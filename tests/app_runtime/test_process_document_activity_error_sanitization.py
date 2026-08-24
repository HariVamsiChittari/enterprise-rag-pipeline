"""Regression test for process_document_activity's retryable-error re-raise: must not
propagate the original exception's raw message, which could embed a signed
SharePoint/Graph download URL and trip a known Azure Functions host bug that corrupts
Durable's replay state for exceptions containing credential-like tokens
(https://github.com/Azure/azure-functions-durable-python/issues/600).

Monkeypatches only the dependency-construction points (_build_repository,
_build_graph_client, etc.) and ingestion.services.process_document — the same
established pattern as tests/app_runtime/test_retire_prior_version.py. Production code
is never modified by these tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import function_app


class FakeConfig:
    extraction_enabled = False
    enrichment_enabled = False
    sharepoint_site_url = ""
    drive_id = "drive-1"
    source_id = "source-1"


class FakeRepository:
    def get_document(self, source_run_id: str, document_id: str) -> SimpleNamespace:
        return SimpleNamespace(record=object(), etag="etag-1")


@pytest.fixture(autouse=True)
def patch_activity_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    import config

    monkeypatch.setattr(config, "load_config", lambda: FakeConfig())
    monkeypatch.setattr(function_app, "_build_repository", lambda config: FakeRepository())
    monkeypatch.setattr(function_app, "_build_graph_client", lambda config: object())
    monkeypatch.setattr(function_app, "_build_sharepoint_client", lambda config: None)
    monkeypatch.setattr(function_app, "_build_openai_client", lambda config: object())
    monkeypatch.setattr(function_app, "_build_audit_container", lambda config: object())


SIGNED_URL_MESSAGE = (
    "download failed: https://contoso.sharepoint.com/_api/download"
    "?tempauth=eyJhbGciOiJIUzI1NiJ9.fake&sig=aB3dE%2FfG5hI%2BjK7lM9nO%3D"
)


def test_retryable_error_reraises_sanitized_exception_without_raw_url(monkeypatch: pytest.MonkeyPatch) -> None:
    import ingestion.services as services

    def _raise_timeout(*args: Any, **kwargs: Any) -> None:
        raise TimeoutError(SIGNED_URL_MESSAGE)

    monkeypatch.setattr(services, "process_document", _raise_timeout)

    with pytest.raises(Exception) as exc_info:
        function_app.process_document_activity({"document": {"sourceRunId": "run-1", "documentId": "doc-1"}})

    assert "sig=" not in str(exc_info.value)
    assert "tempauth" not in str(exc_info.value)
    assert SIGNED_URL_MESSAGE not in str(exc_info.value)


def test_nonretryable_error_returns_safe_code_without_raw_url(monkeypatch: pytest.MonkeyPatch) -> None:
    import ingestion.services as services

    def _raise_value_error(*args: Any, **kwargs: Any) -> None:
        raise ValueError(SIGNED_URL_MESSAGE)

    monkeypatch.setattr(services, "process_document", _raise_value_error)

    result = function_app.process_document_activity({"document": {"sourceRunId": "run-1", "documentId": "doc-1"}})

    assert result["status"] == "failed"
    assert "sig=" not in result["error"]
    assert "tempauth" not in result["error"]
    assert SIGNED_URL_MESSAGE not in result["error"]
