"""Tests for the Cosmos-based "is full-sync running" check that replaced the Durable
per-instance-ID check. Durable instance-ID reuse is documented as best-effort/racy at
the storage layer (Azure/azure-functions-durable-python#410), so this app never reuses
instance IDs and instead tracks "is a full-sync active" via the same Cosmos
source-control/run records the app already writes during activation.

Uses a minimal fake repository (only the two methods these helpers call). Production
code is never modified by these tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import function_app
from ingestion.models import RunStatus


class FakeRepository:
    def __init__(
        self,
        control: Any | None = None,
        run: Any | None = None,
    ) -> None:
        self._control = control
        self._run = run

    def get_source_control(self, source_id: str) -> Any | None:
        return self._control

    def get_run(self, source_id: str, run_id: str) -> Any | None:
        return self._run


def _control(instance_id: str = "full-sync-abc", run_id: str = "run-1") -> SimpleNamespace:
    return SimpleNamespace(record=SimpleNamespace(current_orchestration_instance_id=instance_id, current_run_id=run_id))


def _run(status: RunStatus) -> SimpleNamespace:
    return SimpleNamespace(record=SimpleNamespace(status=status))


def test_full_sync_is_running_false_when_no_control_record() -> None:
    repo = FakeRepository(control=None)
    assert function_app._full_sync_is_running(repo, "source-1") is False


def test_full_sync_is_running_false_when_run_completed() -> None:
    repo = FakeRepository(control=_control(), run=_run(RunStatus.COMPLETED))
    assert function_app._full_sync_is_running(repo, "source-1") is False


def test_full_sync_is_running_true_when_run_running() -> None:
    repo = FakeRepository(control=_control(), run=_run(RunStatus.RUNNING))
    assert function_app._full_sync_is_running(repo, "source-1") is True


def test_current_full_sync_instance_id_returns_none_without_control_record() -> None:
    repo = FakeRepository(control=None)
    assert function_app._current_full_sync_instance_id(repo, "source-1") is None


def test_current_full_sync_instance_id_returns_stored_value() -> None:
    repo = FakeRepository(control=_control(instance_id="full-sync-xyz"))
    assert function_app._current_full_sync_instance_id(repo, "source-1") == "full-sync-xyz"
