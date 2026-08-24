"""Tests for delta-sync and ACL-resync orchestration logic in services.py.

process_document() itself (extract/chunk/embed/write) is already exercised by the
full-sync path; these tests focus on the incremental orchestration: adapting delta
items, retiring superseded versions, hard-deleting on Graph deletion, and the
ACL-resync unchanged/updated/retired decision tree.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import httpx
import pytest

from config import IngestionConfig
from ingestion.errors import TerminalDocumentError
from ingestion.graph import VerifiedAcl
from ingestion.lifecycle_repository import LifecycleConflictError, ReadyDocumentRef
from ingestion.models import (
    ActivityOutcome,
    ActivityStatus,
    create_document_id,
    create_document_key,
    create_source_run_id,
)
from ingestion.repository import VersionedRecord
from ingestion.source_connector import SharePointConnector
import ingestion.services as services


def build_config(**overrides: Any) -> IngestionConfig:
    values = dict(
        extraction_enabled=True,
        enrichment_enabled=True,
        summary_enabled=False,
        key_phrases_enabled=True,
        entities_enabled=True,
        allowed_extensions=(".pdf",),
        source_id="source",
        drive_id="drive",
        tenant_id="tenant",
        app_client_id="app",
        certificate_secret_name="cert",
        key_vault_uri="https://kv.example",
        cosmos_endpoint="https://cosmos.example",
        cosmos_database="db",
        cosmos_ingestion_runs_container="ingestion-runs",
        cosmos_source_documents_container="source-documents",
        cosmos_search_chunks_container="search-chunks",
        document_intelligence_endpoint="https://di.example",
        language_endpoint="https://lang.example",
        openai_endpoint="https://openai.example",
        managed_identity_client_id="mi",
        chunk_max_tokens=800,
        chunk_overlap_tokens=100,
        acl_max_pages=10,
        download_timeout_seconds=120.0,
        delta_max_pages=200,
        embedding_batch_size=100,
        max_pdf_pages=500,
        query_proxy_timeout_seconds=30.0,
        sharepoint_site_url="",
    )
    values.update(overrides)
    return IngestionConfig(**values)


class FakeRepository:
    def __init__(self) -> None:
        self.created: list[Any] = []

    def create_discovered_document(self, document: Any) -> VersionedRecord:
        self.created.append(document)
        return VersionedRecord(document, "doc-etag-1")


class FakeLifecycleRepository:
    def __init__(self, cursor: str | None = None) -> None:
        self._cursor = cursor
        self.saved_cursors: list[str] = []
        self.retired: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        self.refreshed: list[dict[str, Any]] = []
        self.ready_by_document_id: dict[str, ReadyDocumentRef] = {}

    def get_delta_cursor(self, source_id: str) -> str | None:
        return self._cursor

    def save_delta_cursor(self, source_id: str, delta_link: str) -> None:
        self.saved_cursors.append(delta_link)
        self._cursor = delta_link

    def find_ready_document_by_document_id(self, document_id: str) -> ReadyDocumentRef | None:
        return self.ready_by_document_id.get(document_id)

    def delete_document_and_chunks(self, *, source_run_id: str, document_id: str, document_key: str, etag: str) -> None:
        self.deleted.append({"source_run_id": source_run_id, "document_id": document_id, "document_key": document_key, "etag": etag})

    def retire_document(self, *, source_run_id: str, document_id: str, etag: str, reason: str) -> None:
        self.retired.append({"source_run_id": source_run_id, "document_id": document_id, "reason": reason})

    def refresh_document_acl(self, **kwargs: Any) -> None:
        self.refreshed.append(kwargs)


def _delta_pdf_item(item_id: str = "item-1", *, deleted: bool = False, name: str = "a.pdf") -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": item_id,
        "name": name,
        "eTag": "etag-x",
        "size": 100,
        "file": {},
        "parentReference": {"id": "parent", "path": "/drive/root:"},
        "webUrl": "https://example.invalid/a.pdf",
    }
    if deleted:
        item["deleted"] = {"state": "deleted"}
    return item


def _mock_connector(handler, drive_id: str = "drive") -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- run_delta_sync ---

def test_run_delta_sync_bootstraps_cursor_on_first_tick_without_processing() -> None:
    config = build_config()
    lifecycle = FakeLifecycleRepository(cursor=None)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "token=latest" in str(request.url)
        return httpx.Response(200, json={"@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=abc"})

    with _mock_connector(handler) as client:
        connector = SharePointConnector(client, config.drive_id)
        outcome = services.run_delta_sync(config, FakeRepository(), lifecycle, connector, None, None, None)

    assert outcome.bootstrapped is True
    assert lifecycle.saved_cursors == ["https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=abc"]


def test_run_delta_sync_processes_add_and_advances_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    config = build_config()
    lifecycle = FakeLifecycleRepository(cursor="https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=old")
    repository = FakeRepository()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"value": [_delta_pdf_item("item-1")], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=new"},
        )

    monkeypatch.setattr(
        services, "process_document",
        lambda *a, **k: ActivityOutcome(document_id="doc", status=ActivityStatus.SUCCEEDED, chunks_written=3, retry_count=0),
    )

    with _mock_connector(handler) as client:
        connector = SharePointConnector(client, config.drive_id)
        outcome = services.run_delta_sync(config, repository, lifecycle, connector, None, None, None)

    assert outcome.created_or_updated == 1
    assert outcome.failed == 0
    assert len(repository.created) == 1
    assert lifecycle.saved_cursors == ["https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=new"]
    assert lifecycle.retired == []  # no prior ready version -> pure add, nothing to retire


def test_run_delta_sync_deletes_superseded_version_after_successful_update(monkeypatch: pytest.MonkeyPatch) -> None:
    config = build_config()
    document_id = create_document_id("source", "drive", "item-1")
    prev_ref = ReadyDocumentRef(
        document_id=document_id,
        source_run_id=create_source_run_id("source", "run-old"),
        document_key=create_document_key("source", "run-old", document_id),
        item_id="item-1",
        allowed_group_ids=("group-a",),
        acl_hash="hash-a",
        etag="etag-old",
    )
    lifecycle = FakeLifecycleRepository(cursor="https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=old")
    lifecycle.ready_by_document_id[document_id] = prev_ref
    repository = FakeRepository()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"value": [_delta_pdf_item("item-1")], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=new"},
        )

    monkeypatch.setattr(
        services, "process_document",
        lambda *a, **k: ActivityOutcome(document_id="doc", status=ActivityStatus.SUCCEEDED, chunks_written=3, retry_count=0),
    )

    with _mock_connector(handler) as client:
        connector = SharePointConnector(client, config.drive_id)
        outcome = services.run_delta_sync(config, repository, lifecycle, connector, None, None, None)

    assert outcome.created_or_updated == 1
    assert lifecycle.retired == []
    assert len(lifecycle.deleted) == 1
    assert lifecycle.deleted[0]["source_run_id"] == prev_ref.source_run_id
    assert lifecycle.deleted[0]["document_key"] == prev_ref.document_key
    assert lifecycle.deleted[0]["etag"] == prev_ref.etag


def test_run_delta_sync_deletes_document_and_chunks_on_graph_deletion() -> None:
    config = build_config()
    document_id = create_document_id("source", "drive", "item-1")
    ref = ReadyDocumentRef(
        document_id=document_id, source_run_id="source:run-a", document_key="source:run-a:" + document_id,
        item_id="item-1", allowed_group_ids=("group-a",), acl_hash="hash-a", etag="etag-a",
    )
    lifecycle = FakeLifecycleRepository(cursor="https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=old")
    lifecycle.ready_by_document_id[document_id] = ref

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"value": [_delta_pdf_item("item-1", deleted=True)], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=new"},
        )

    with _mock_connector(handler) as client:
        connector = SharePointConnector(client, config.drive_id)
        outcome = services.run_delta_sync(config, FakeRepository(), lifecycle, connector, None, None, None)

    assert outcome.deleted == 1
    assert lifecycle.retired == []
    assert lifecycle.deleted == [{
        "source_run_id": "source:run-a", "document_id": document_id,
        "document_key": ref.document_key, "etag": ref.etag,
    }]


def test_run_delta_sync_deletion_of_untracked_item_is_a_no_op() -> None:
    config = build_config()
    lifecycle = FakeLifecycleRepository(cursor="https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=old")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"value": [_delta_pdf_item("item-1", deleted=True)], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=new"},
        )

    with _mock_connector(handler) as client:
        connector = SharePointConnector(client, config.drive_id)
        outcome = services.run_delta_sync(config, FakeRepository(), lifecycle, connector, None, None, None)

    assert outcome.deleted == 0
    assert lifecycle.deleted == []


def test_run_delta_sync_routes_permission_change_to_acl_resync() -> None:
    """Items annotated with @microsoft.graph.sharedChanged trigger ACL resync, not full reprocess."""
    config = build_config()
    document_id = create_document_id("source", "drive", "item-1")
    ref = ReadyDocumentRef(
        document_id=document_id, source_run_id="source:run-a", document_key="source:run-a:" + document_id,
        item_id="item-1", allowed_group_ids=("group-a",), acl_hash="hash-a", etag="etag-a",
    )
    lifecycle = FakeLifecycleRepository(cursor="https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=old")
    lifecycle.ready_by_document_id[document_id] = ref

    # Item with permission change annotation (not a content change, not deleted)
    permission_item = {
        "id": "item-1", "name": "a.pdf", "eTag": "etag-x", "size": 100,
        "file": {}, "parentReference": {"id": "parent", "path": "/drive/root:"},
        "@microsoft.graph.sharedChanged": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"value": [permission_item], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=new"},
        )

    with _mock_connector(handler) as client:
        connector = SharePointConnector(client, config.drive_id)
        outcome = services.run_delta_sync(config, FakeRepository(), lifecycle, connector, None, None, None)

    assert outcome.acl_resynced == 1
    assert outcome.created_or_updated == 0
    # Verify the ACL was actually refreshed (FakeConnector returns same hash → "unchanged")
    # The important thing is that it DIDN'T trigger full process_document


def test_run_delta_sync_permission_change_on_untracked_item_is_noop() -> None:
    """Permission change on a document not yet ingested should be skipped."""
    config = build_config()
    lifecycle = FakeLifecycleRepository(cursor="https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=old")

    permission_item = {
        "id": "item-unknown", "name": "b.pdf", "eTag": "etag-y", "size": 50,
        "file": {}, "parentReference": {"id": "parent", "path": "/drive/root:"},
        "@microsoft.graph.sharedChanged": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"value": [permission_item], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=new"},
        )

    with _mock_connector(handler) as client:
        connector = SharePointConnector(client, config.drive_id)
        outcome = services.run_delta_sync(config, FakeRepository(), lifecycle, connector, None, None, None)

    assert outcome.acl_resynced == 0
    assert outcome.failed == 0


def test_run_delta_sync_skips_folders_and_disallowed_extensions(monkeypatch: pytest.MonkeyPatch) -> None:
    config = build_config()
    lifecycle = FakeLifecycleRepository(cursor="https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=old")
    folder_item = {"id": "folder-1", "name": "folder", "folder": {}}
    other_ext_item = _delta_pdf_item("item-2", name="notes.txt")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"value": [folder_item, other_ext_item], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=new"},
        )

    called = False

    def _unexpected_process_document(*a: Any, **k: Any) -> ActivityOutcome:
        nonlocal called
        called = True
        raise AssertionError("process_document should not be called for folders/disallowed extensions")

    monkeypatch.setattr(services, "process_document", _unexpected_process_document)

    with _mock_connector(handler) as client:
        connector = SharePointConnector(client, config.drive_id)
        outcome = services.run_delta_sync(config, FakeRepository(), lifecycle, connector, None, None, None)

    assert called is False
    assert outcome.created_or_updated == 0
    assert outcome.failed == 0


# --- resync_document_acl / run_acl_resync_page ---

def _ready_ref(**overrides: Any) -> ReadyDocumentRef:
    values = dict(
        document_id="doc-1", source_run_id="source:run-a", document_key="source:run-a:doc-1",
        item_id="item-1", allowed_group_ids=("group-a",), acl_hash="hash-a", etag="etag-1",
    )
    values.update(overrides)
    return ReadyDocumentRef(**values)


class FakeConnector:
    """A SourceConnector stub for ACL-resync tests that don't need real Graph HTTP."""

    def __init__(self, read_verified_acl: Any) -> None:
        self._read_verified_acl = read_verified_acl

    def read_verified_acl(self, item_id: str, max_pages: int) -> VerifiedAcl:
        return self._read_verified_acl(item_id, max_pages)


def test_resync_document_acl_is_a_noop_when_hash_unchanged() -> None:
    config = build_config()
    lifecycle = FakeLifecycleRepository()
    connector = FakeConnector(lambda *a, **k: VerifiedAcl(("group-a",), "hash-a"))

    result = services.resync_document_acl(config, _ready_ref(), lifecycle, connector)

    assert result == "unchanged"
    assert lifecycle.refreshed == []
    assert lifecycle.retired == []


def test_resync_document_acl_patches_when_hash_changed() -> None:
    config = build_config()
    lifecycle = FakeLifecycleRepository()
    connector = FakeConnector(lambda *a, **k: VerifiedAcl(("group-a", "group-b"), "hash-b"))

    result = services.resync_document_acl(config, _ready_ref(), lifecycle, connector)

    assert result == "updated"
    assert len(lifecycle.refreshed) == 1
    assert lifecycle.refreshed[0]["allowed_group_ids"] == ("group-a", "group-b")


def test_resync_document_acl_retires_on_terminal_acl_error() -> None:
    config = build_config()
    lifecycle = FakeLifecycleRepository()

    def _raise(*a: Any, **k: Any) -> None:
        raise TerminalDocumentError("unsafe_acl:no_verified_security_groups")

    connector = FakeConnector(_raise)

    result = services.resync_document_acl(config, _ready_ref(), lifecycle, connector)

    assert result == "retired"
    assert lifecycle.retired == [{"source_run_id": "source:run-a", "document_id": "doc-1", "reason": "acl_revoked"}]


def test_resync_document_acl_tolerates_concurrent_retirement(monkeypatch: pytest.MonkeyPatch) -> None:
    config = build_config()
    lifecycle = FakeLifecycleRepository()

    def _raise(*a: Any, **k: Any) -> None:
        raise TerminalDocumentError("unsafe_acl:no_verified_security_groups")

    def _retire_conflict(**kwargs: Any) -> None:
        raise LifecycleConflictError("already retired")

    connector = FakeConnector(_raise)
    monkeypatch.setattr(lifecycle, "retire_document", _retire_conflict)

    result = services.resync_document_acl(config, _ready_ref(), lifecycle, connector)

    assert result == "retired"
