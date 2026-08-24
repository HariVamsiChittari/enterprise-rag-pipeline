"""Tests for _wait_for_terminal_status, the shared helper behind the fix to
start_full_sync/terminate_ingestion's stale-Durable-Task-history handling.

Note: start_full_sync/terminate_ingestion are decorated with
@app.durable_client_input, whose middleware reconstructs a real
DurableOrchestrationClient from a JSON binding payload before calling the user
code — they can't be invoked directly with a plain fake client object without
faking that binding-resolution machinery, which isn't worth the complexity for
what is otherwise thin route-handler glue. Those two functions are covered by
live end-to-end verification instead. Production code is never modified here.
"""

from __future__ import annotations

import asyncio
from typing import Any

import azure.durable_functions as df

import function_app


class FakeStatus:
    def __init__(self, runtime_status: Any) -> None:
        self.runtime_status = runtime_status


class FakeClient:
    def __init__(self, statuses: list[Any] | None = None) -> None:
        self._statuses = list(statuses or [])

    async def get_status(self, instance_id: str) -> FakeStatus | None:
        if not self._statuses:
            return None
        status = self._statuses.pop(0)
        return None if status is None else FakeStatus(status)


def test_wait_for_terminal_status_true_when_already_terminal() -> None:
    client = FakeClient(statuses=[df.OrchestrationRuntimeStatus.Terminated])
    assert asyncio.run(function_app._wait_for_terminal_status(client, "id-1")) is True


def test_wait_for_terminal_status_true_when_instance_missing() -> None:
    client = FakeClient(statuses=[None])
    assert asyncio.run(function_app._wait_for_terminal_status(client, "id-1")) is True


def test_wait_for_terminal_status_times_out_when_stuck_running() -> None:
    client = FakeClient(statuses=[df.OrchestrationRuntimeStatus.Running])
    assert asyncio.run(function_app._wait_for_terminal_status(client, "id-1", timeout_seconds=0)) is False

