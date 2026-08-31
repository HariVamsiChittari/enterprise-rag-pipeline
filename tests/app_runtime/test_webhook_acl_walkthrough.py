from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "tools" / "script_webhook_acl_walkthrough.py"
SPEC = importlib.util.spec_from_file_location("script_webhook_acl_walkthrough", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
walkthrough = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = walkthrough
SPEC.loader.exec_module(walkthrough)


class FakeApi:
    def __init__(self, *, instance_id: str | None, status: dict | None) -> None:
        self.instance_id = instance_id
        self.status = status
        self.optional_paths: list[str] = []

    def get(self, path: str) -> dict:
        if "container=ingestion-runs" in path:
            rows = []
            if self.instance_id is not None:
                rows.append({"id": "delta-sync-trigger", "currentInstanceId": self.instance_id})
            return {"rows": rows}
        if "container=service-audit" in path:
            return {"rows": [{"operation": "webhook_received"}]}
        raise AssertionError(f"unexpected GET {path}")

    def get_optional(self, path: str) -> dict | None:
        self.optional_paths.append(path)
        return self.status


@pytest.fixture
def config():
    return walkthrough.DemoConfig(
        function_app="func",
        client_id="client",
        file_name="test.pdf",
        editors_group_id="group",
        question="question",
        timeout_seconds=30,
        poll_seconds=1,
        max_ready_documents=50,
        preflight_only=False,
        query_timeout_seconds=30,
        resume_after_upload=False,
    )


def test_delta_status_requires_trigger_instance_id(config) -> None:
    with pytest.raises(walkthrough.DemoError, match="no current orchestration instance ID"):
        walkthrough._show_delta_and_audit(
            FakeApi(instance_id=None, status=None), config, walkthrough.DemoReporter(), "doc-1"
        )


def test_delta_status_rejects_stale_trigger_instance(config) -> None:
    api = FakeApi(instance_id="delta-sync-trigger-stale", status=None)

    with pytest.raises(walkthrough.DemoError, match="was not found"):
        walkthrough._show_delta_and_audit(api, config, walkthrough.DemoReporter(), "doc-1")

    assert api.optional_paths == [
        "/api/ingestion/status?instanceId=delta-sync-trigger-stale"
    ]


def test_delta_status_accepts_current_completed_success(config) -> None:
    api = FakeApi(
        instance_id="delta-sync-trigger-current",
        status={
            "runtimeStatus": "OrchestrationRuntimeStatus.Completed",
            "output": {
                "createdOrUpdated": 1,
                "deleted": 0,
                "aclResynced": 0,
                "failed": 0,
            },
        },
    )
    reporter = walkthrough.DemoReporter()

    walkthrough._show_delta_and_audit(api, config, reporter, "doc-1")

    assert any(
        event["level"] == "INFO" and "instanceId=delta-sync-trigger-current" in event["message"]
        for event in reporter.events
    )