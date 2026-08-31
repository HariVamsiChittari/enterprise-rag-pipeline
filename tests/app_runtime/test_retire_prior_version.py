"""Tests for function_app._retire_prior_version (full-sync hard-delete path).

Monkeypatches only the two dependency-construction points _retire_prior_version
actually uses (_build_lifecycle_repository, ingestion.telemetry.write_audit_record).
Production code is never modified by these tests.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

import function_app
import ingestion.telemetry as telemetry
from ingestion.lifecycle_repository import LifecycleConflictError, ReadyDocumentRef


class FakeConfig:
    def __init__(self, source_id: str = "source") -> None:
        self.source_id = source_id


class FakeLifecycleRepo:
    def __init__(self, ref: ReadyDocumentRef | None = None, raise_error: Exception | None = None) -> None:
        self._ref = ref
        self._raise_error = raise_error
        self.delete_calls: list[dict[str, Any]] = []

    def list_ready_document_versions(self, document_id: str) -> tuple[ReadyDocumentRef, ...]:
        return (self._ref,) if self._ref is not None else ()

    def delete_document_and_chunks(
        self, *, source_run_id: str, document_id: str, document_key: str, etag: str
    ) -> None:
        if self._raise_error is not None:
            raise self._raise_error
        self.delete_calls.append({
            "source_run_id": source_run_id, "document_id": document_id,
            "document_key": document_key, "etag": etag,
        })


def _prior_ref(source_run_id: str = "source:run-old") -> ReadyDocumentRef:
    return ReadyDocumentRef(
        document_id="doc-1", source_run_id=source_run_id, document_key=f"{source_run_id}:doc-1",
        item_id="item-1", allowed_group_ids=("group-a",), acl_hash="hash-a", etag="etag-old",
    )


def _patch_repo(monkeypatch: pytest.MonkeyPatch, repo: FakeLifecycleRepo) -> None:
    monkeypatch.setattr(function_app, "_build_lifecycle_repository", lambda config: repo)


def _patch_audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def _fake_write(container: Any, source_id: str, run_id: str, record: dict[str, Any]) -> None:
        records.append({"container": container, "source_id": source_id, "run_id": run_id, **record})

    monkeypatch.setattr(telemetry, "write_audit_record", _fake_write)
    return records


def test_retire_prior_version_hard_deletes_and_audits_when_prior_ready_version_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = _prior_ref()
    repo = FakeLifecycleRepo(ref=ref)
    _patch_repo(monkeypatch, repo)
    records = _patch_audit(monkeypatch)

    function_app._retire_prior_version(FakeConfig(), "doc-1", "source:run-new", "sentinel-container")

    assert repo.delete_calls == [{
        "source_run_id": ref.source_run_id, "document_id": "doc-1",
        "document_key": ref.document_key, "etag": ref.etag,
    }]
    assert len(records) == 1
    assert records[0]["operation"] == "document_deleted"
    assert records[0]["reason"] == "superseded"
    assert records[0]["method"] == "full_sync"
    assert records[0]["replacedDocumentKey"] == ref.document_key
    assert records[0]["container"] == "sentinel-container"
    assert records[0]["run_id"] == "source:run-new"


def test_retire_prior_version_is_a_no_op_when_no_prior_version_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeLifecycleRepo(ref=None)
    _patch_repo(monkeypatch, repo)
    records = _patch_audit(monkeypatch)

    function_app._retire_prior_version(FakeConfig(), "doc-1", "source:run-new", "sentinel-container")

    assert repo.delete_calls == []
    assert records == []


def test_retire_prior_version_is_a_no_op_when_ref_is_the_current_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = _prior_ref(source_run_id="source:run-new")
    repo = FakeLifecycleRepo(ref=ref)
    _patch_repo(monkeypatch, repo)
    records = _patch_audit(monkeypatch)

    function_app._retire_prior_version(FakeConfig(), "doc-1", "source:run-new", "sentinel-container")

    assert repo.delete_calls == []
    assert records == []


def test_retire_prior_version_swallows_conflict_error_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = _prior_ref()
    repo = FakeLifecycleRepo(ref=ref, raise_error=LifecycleConflictError("already changed"))
    _patch_repo(monkeypatch, repo)
    records = _patch_audit(monkeypatch)

    function_app._retire_prior_version(FakeConfig(), "doc-1", "source:run-new", "sentinel-container")

    assert records == []  # delete failed before the audit write line was reached


def test_retire_prior_version_swallows_unexpected_errors_and_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    ref = _prior_ref()
    repo = FakeLifecycleRepo(ref=ref, raise_error=RuntimeError("boom"))
    _patch_repo(monkeypatch, repo)
    _patch_audit(monkeypatch)
    caplog.set_level(logging.WARNING)

    function_app._retire_prior_version(FakeConfig(), "doc-1", "source:run-new", "sentinel-container")

    assert any("retire_prior_version failed" in record.message for record in caplog.records)


def test_retire_prior_version_omits_audit_when_container_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = _prior_ref()
    repo = FakeLifecycleRepo(ref=ref)
    _patch_repo(monkeypatch, repo)
    records = _patch_audit(monkeypatch)

    function_app._retire_prior_version(FakeConfig(), "doc-1", "source:run-new")

    assert len(repo.delete_calls) == 1
    assert records == []
