"""Tests for the post-ingestion lifecycle repository (ACL resync and delta sync)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from azure.core import MatchConditions

from ingestion.lifecycle_repository import (
    DocumentLifecycleRepository,
    LifecycleConflictError,
    LifecycleRepositoryError,
    ReadyDocumentRef,
)


class FakeCosmosError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("sensitive sdk response")
        self.status_code = status_code


class FakePager:
    def __init__(self, page: list[dict[str, Any]], continuation_token: str | None) -> None:
        self._page = page
        self.continuation_token = continuation_token

    def __next__(self) -> list[dict[str, Any]]:
        return self._page


class FakeQueryIterator:
    def __init__(self, page: list[dict[str, Any]], continuation_token: str | None) -> None:
        self._page = page
        self._continuation_token = continuation_token

    def by_page(self, continuation_token: str | None = None) -> FakePager:
        return FakePager(self._page, self._continuation_token)

    def __iter__(self):
        return iter(self._page)


class FakeContainer:
    """A minimal fake covering read_item/patch_item/upsert_item/query_items/
    execute_item_batch/delete_item, enough to exercise DocumentLifecycleRepository."""

    def __init__(self, partition_field: str, items: dict[tuple[str, str], dict[str, Any]] | None = None) -> None:
        self.partition_field = partition_field
        self.items = items or {}
        self.patch_calls: list[dict[str, Any]] = []
        self.batch_calls: list[tuple[str, list[tuple[Any, ...]]]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self.query_pages: dict[str, list[list[dict[str, Any]]]] = {}

    def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        key = (partition_key, item)
        if key not in self.items:
            raise FakeCosmosError(404)
        return deepcopy(self.items[key])

    def upsert_item(self, *, body: dict[str, Any]) -> dict[str, Any]:
        key = (body[self.partition_field], body["id"])
        self.items[key] = deepcopy(body)
        return deepcopy(body)

    def patch_item(
        self,
        *,
        item: str,
        partition_key: str,
        patch_operations: list[dict[str, Any]],
        filter_predicate: str | None = None,
        etag: str | None = None,
        match_condition: MatchConditions | None = None,
    ) -> dict[str, Any]:
        self.patch_calls.append(
            {"item": item, "partition_key": partition_key, "patch_operations": patch_operations}
        )
        key = (partition_key, item)
        if key not in self.items:
            raise FakeCosmosError(404)
        stored = self.items[key]
        if etag is not None and stored.get("_etag") != etag:
            raise FakeCosmosError(412)
        if filter_predicate is not None and "status = 'ready'" in filter_predicate:
            if stored.get("status") != "ready":
                raise FakeCosmosError(412)
        for operation in patch_operations:
            path = operation["path"].lstrip("/")
            stored[path] = operation["value"]
        return deepcopy(stored)

    def execute_item_batch(
        self, *, batch_operations: list[tuple[Any, ...]], partition_key: str
    ) -> list[dict[str, Any]]:
        self.batch_calls.append((partition_key, deepcopy(batch_operations)))
        for kind, args in batch_operations:
            if kind != "patch":
                raise AssertionError(f"unsupported batch op: {kind}")
            item_id, patch_operations = args
            key = (partition_key, item_id)
            stored = self.items[key]
            for operation in patch_operations:
                stored[operation["path"].lstrip("/")] = operation["value"]
        return [{"statusCode": 200} for _ in batch_operations]

    def delete_item(self, *, item: str, partition_key: str) -> None:
        self.delete_calls.append((item, partition_key))
        key = (partition_key, item)
        if key not in self.items:
            raise FakeCosmosError(404)
        del self.items[key]

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict[str, Any]],
        max_item_count: int,
        partition_key: str | None = None,
        enable_cross_partition_query: bool | None = None,
    ) -> FakeQueryIterator:
        parameter_values = {parameter["name"]: parameter["value"] for parameter in parameters}
        rows = []
        for (pk, item_id), stored in self.items.items():
            if partition_key is not None and pk != partition_key:
                continue
            if "@status" in parameter_values and stored.get("status") != parameter_values["@status"]:
                continue
            if "@documentId" in parameter_values and stored.get("documentId") != parameter_values["@documentId"]:
                continue
            if "@documentKey" in parameter_values and stored.get("documentKey") != parameter_values["@documentKey"]:
                continue
            rows.append(deepcopy(stored))
        return FakeQueryIterator(rows, None)


def _ready_document(document_id: str = "doc-1", **overrides: Any) -> dict[str, Any]:
    base = {
        "id": document_id,
        "documentId": document_id,
        "sourceRunId": "source:run-a",
        "documentKey": f"source:run-a:{document_id}",
        "itemId": "item-1",
        "status": "ready",
        "allowedGroupIds": ["group-a"],
        "aclHash": "hash-a",
        "_etag": "etag-1",
    }
    base.update(overrides)
    return base


def test_retire_document_flips_status_and_requires_ready_precondition() -> None:
    runs = FakeContainer("sourceId")
    documents = FakeContainer("sourceRunId", {("source:run-a", "doc-1"): _ready_document()})
    repo = DocumentLifecycleRepository(runs, documents, FakeContainer("documentKey"))

    repo.retire_document(source_run_id="source:run-a", document_id="doc-1", etag="etag-1", reason="acl_revoked")

    stored = documents.items[("source:run-a", "doc-1")]
    assert stored["status"] == "retired"
    assert stored["retiredReason"] == "acl_revoked"
    assert stored["retiredAt"]


def test_retire_document_rejects_unsupported_reason() -> None:
    documents = FakeContainer("sourceRunId", {("source:run-a", "doc-1"): _ready_document()})
    repo = DocumentLifecycleRepository(FakeContainer("sourceId"), documents, FakeContainer("documentKey"))

    with pytest.raises(ValueError, match="retired_reason"):
        repo.retire_document(source_run_id="source:run-a", document_id="doc-1", etag="etag-1", reason="nope")


def test_retire_document_conflict_when_already_retired_or_stale_etag() -> None:
    documents = FakeContainer("sourceRunId", {("source:run-a", "doc-1"): _ready_document(status="retired")})
    repo = DocumentLifecycleRepository(FakeContainer("sourceId"), documents, FakeContainer("documentKey"))

    with pytest.raises(LifecycleConflictError):
        repo.retire_document(source_run_id="source:run-a", document_id="doc-1", etag="etag-1", reason="superseded")


def test_refresh_document_acl_patches_document_and_every_chunk() -> None:
    documents = FakeContainer("sourceRunId", {("source:run-a", "doc-1"): _ready_document()})
    chunks = FakeContainer(
        "documentKey",
        {
            ("source:run-a:doc-1", "chunk:000000"): {
                "id": "chunk:000000", "documentKey": "source:run-a:doc-1", "allowedGroupIds": ["group-a"],
            },
            ("source:run-a:doc-1", "chunk:000001"): {
                "id": "chunk:000001", "documentKey": "source:run-a:doc-1", "allowedGroupIds": ["group-a"],
            },
        },
    )
    repo = DocumentLifecycleRepository(FakeContainer("sourceId"), documents, chunks)

    repo.refresh_document_acl(
        source_run_id="source:run-a", document_id="doc-1", document_key="source:run-a:doc-1",
        etag="etag-1", allowed_group_ids=("group-a", "group-b"), acl_hash="hash-b",
    )

    assert documents.items[("source:run-a", "doc-1")]["allowedGroupIds"] == ["group-a", "group-b"]
    assert documents.items[("source:run-a", "doc-1")]["aclHash"] == "hash-b"
    assert chunks.items[("source:run-a:doc-1", "chunk:000000")]["allowedGroupIds"] == ["group-a", "group-b"]
    assert chunks.items[("source:run-a:doc-1", "chunk:000001")]["allowedGroupIds"] == ["group-a", "group-b"]
    assert len(chunks.batch_calls) == 1


def test_delete_document_and_chunks_removes_everything() -> None:
    documents = FakeContainer("sourceRunId", {("source:run-a", "doc-1"): _ready_document()})
    chunks = FakeContainer(
        "documentKey",
        {("source:run-a:doc-1", "chunk:000000"): {"id": "chunk:000000", "documentKey": "source:run-a:doc-1"}},
    )
    repo = DocumentLifecycleRepository(FakeContainer("sourceId"), documents, chunks)

    repo.delete_document_and_chunks(
        source_run_id="source:run-a", document_id="doc-1", document_key="source:run-a:doc-1"
    )

    assert ("source:run-a", "doc-1") not in documents.items
    assert ("source:run-a:doc-1", "chunk:000000") not in chunks.items


def test_delete_document_and_chunks_is_idempotent_when_already_gone() -> None:
    repo = DocumentLifecycleRepository(FakeContainer("sourceId"), FakeContainer("sourceRunId"), FakeContainer("documentKey"))

    repo.delete_document_and_chunks(source_run_id="source:run-a", document_id="doc-1", document_key="source:run-a:doc-1")


def test_list_ready_documents_page_and_find_by_document_id() -> None:
    documents = FakeContainer(
        "sourceRunId",
        {
            ("source:run-a", "doc-1"): _ready_document("doc-1"),
            ("source:run-b", "doc-2"): _ready_document("doc-2", sourceRunId="source:run-b", documentKey="source:run-b:doc-2"),
            ("source:run-a", "doc-3"): _ready_document("doc-3", status="failed"),
        },
    )
    repo = DocumentLifecycleRepository(FakeContainer("sourceId"), documents, FakeContainer("documentKey"))

    page = repo.list_ready_documents_page(page_size=10)
    assert {ref.document_id for ref in page.items} == {"doc-1", "doc-2"}

    found = repo.find_ready_document_by_document_id("doc-2")
    assert found is not None
    assert found.source_run_id == "source:run-b"

    assert repo.find_ready_document_by_document_id("doc-missing") is None


def test_delta_cursor_round_trip() -> None:
    runs = FakeContainer("sourceId")
    repo = DocumentLifecycleRepository(runs, FakeContainer("sourceRunId"), FakeContainer("documentKey"))

    assert repo.get_delta_cursor("source") is None

    repo.save_delta_cursor("source", "https://graph.microsoft.com/v1.0/drives/d/root/delta?token=abc")
    assert repo.get_delta_cursor("source") == "https://graph.microsoft.com/v1.0/drives/d/root/delta?token=abc"


def test_save_delta_cursor_rejects_empty_link() -> None:
    repo = DocumentLifecycleRepository(FakeContainer("sourceId"), FakeContainer("sourceRunId"), FakeContainer("documentKey"))

    with pytest.raises(ValueError):
        repo.save_delta_cursor("source", "")
