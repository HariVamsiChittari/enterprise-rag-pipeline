"""Tests for the Goal 1 SourceConnector boundary: SharePointConnector must delegate to
the same graph.py functions unchanged, just with drive_id/client pre-bound."""

from __future__ import annotations

import httpx

from ingestion.graph import DiscoveryState
from ingestion.models import ScaleLimits
from ingestion.source_connector import SharePointConnector


def _connector(handler) -> tuple[httpx.Client, SharePointConnector]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return client, SharePointConnector(client, "drive-1")


def test_discover_next_page_delegates_with_bound_drive_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/drives/drive-1/root/children" in str(request.url)
        return httpx.Response(200, json={"value": []})

    client, connector = _connector(handler)
    with client:
        step = connector.discover_next_page(DiscoveryState.initial(), ScaleLimits())

    assert step.state.complete is True


def test_read_verified_acl_delegates_with_bound_drive_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/permissions" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "roles": ["read"],
                            "grantedToV2": {"group": {"id": "22222222-2222-2222-2222-222222222222"}},
                        }
                    ]
                },
            )
        if "/groups/" in str(request.url):
            return httpx.Response(200, json={"id": "22222222-2222-2222-2222-222222222222", "securityEnabled": True})
        raise AssertionError(f"unexpected request: {request.url}")

    client, connector = _connector(handler)
    with client:
        acl = connector.read_verified_acl("item-1", max_pages=5)

    assert acl.allowed_group_ids == ("22222222-2222-2222-2222-222222222222",)


def test_bootstrap_delta_cursor_delegates_with_bound_drive_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/drives/drive-1/root/delta" in str(request.url)
        assert "token=latest" in str(request.url)
        return httpx.Response(200, json={"@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=abc"})

    client, connector = _connector(handler)
    with client:
        cursor = connector.bootstrap_delta_cursor()

    assert cursor == "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=abc"


def test_read_drive_delta_delegates_with_bound_drive_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/drives/drive-1/root/delta" in str(request.url)
        return httpx.Response(
            200,
            json={"value": [{"id": "item-1"}], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=new"},
        )

    client, connector = _connector(handler)
    with client:
        delta = connector.read_drive_delta(max_pages=10)

    assert delta.delta_link == "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=new"
    assert [item["id"] for item in delta.items] == ["item-1"]
