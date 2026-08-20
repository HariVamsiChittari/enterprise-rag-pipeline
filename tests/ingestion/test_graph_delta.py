"""Tests for Graph delta query's 410/DeltaResetRequired handling (Scenario 12 gap:
previously untested, though the mechanism already existed in graph.py)."""

from __future__ import annotations

import httpx

from ingestion.graph import read_drive_delta


def test_read_drive_delta_first_call_establishes_baseline() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/drives/drive-1/root/delta" in str(request.url)
        return httpx.Response(
            200,
            json={
                "value": [{"id": "item-1"}, {"id": "item-2"}],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=first",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        delta = read_drive_delta(client, "drive-1", max_pages=10)

    assert delta.is_full_baseline is True
    assert delta.delta_link == "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=first"
    assert {item["id"] for item in delta.items} == {"item-1", "item-2"}


def test_read_drive_delta_resumes_normally_with_a_stored_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "token=old" in str(request.url)
        return httpx.Response(
            200,
            json={
                "value": [{"id": "item-3"}],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=new",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        delta = read_drive_delta(
            client, "drive-1", max_pages=10,
            delta_link="https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=old",
        )

    assert delta.is_full_baseline is False
    assert delta.delta_link == "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=new"
    assert [item["id"] for item in delta.items] == ["item-3"]


def test_read_drive_delta_handles_410_reset_with_a_full_rebaseline() -> None:
    reset_location = "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta"
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            assert "token=expired" in str(request.url)
            return httpx.Response(410, headers={"location": reset_location})
        # The reset re-request must go to the location the server provided, without a token.
        assert str(request.url) == reset_location
        return httpx.Response(
            200,
            json={
                "value": [{"id": "item-1"}, {"id": "item-2"}],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=rebaselined",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        delta = read_drive_delta(
            client, "drive-1", max_pages=10,
            delta_link="https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=expired",
        )

    assert call_count == 2
    assert delta.is_full_baseline is True  # caller must treat this as a full re-sync, not an incremental diff
    assert delta.delta_link == "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=rebaselined"
    assert {item["id"] for item in delta.items} == {"item-1", "item-2"}


def test_read_drive_delta_deduplicates_repeated_item_ids_keeping_latest_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    {"id": "item-1", "name": "old-name.pdf"},
                    {"id": "item-1", "name": "renamed.pdf"},
                ],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=x",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        delta = read_drive_delta(client, "drive-1", max_pages=10)

    assert len(delta.items) == 1
    assert delta.items[0]["name"] == "renamed.pdf"


def test_read_drive_delta_handles_double_410_reset() -> None:
    """Graph can return 410 on the reset URL itself during server-side transitions."""
    first_reset = "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?reset=1"
    second_reset = "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?reset=2"
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(410, headers={"location": first_reset})
        if call_count == 2:
            return httpx.Response(410, headers={"location": second_reset})
        return httpx.Response(
            200,
            json={
                "value": [{"id": "item-1"}],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=fresh",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        delta = read_drive_delta(
            client, "drive-1", max_pages=10,
            delta_link="https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=stale",
        )

    assert call_count == 3
    assert delta.is_full_baseline is True
    assert delta.delta_link == "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=fresh"
    assert [item["id"] for item in delta.items] == ["item-1"]
