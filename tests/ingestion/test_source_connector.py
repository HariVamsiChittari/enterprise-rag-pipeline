"""Tests for the SourceConnector boundary: SharePointConnector must delegate to the
same graph.py functions unchanged, just with drive_id/client pre-bound."""

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


def test_read_item_delegates_with_bound_drive_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/drives/drive-1/items/item-1" in str(request.url)
        return httpx.Response(200, json={
            "id": "item-1",
            "name": "document.pdf",
            "eTag": "source-etag",
            "file": {"mimeType": "application/pdf"},
        })

    client, connector = _connector(handler)
    with client:
        item = connector.read_item("item-1")

    assert item is not None
    assert item["eTag"] == "source-etag"


def test_read_verified_acl_delegates_site_group_context() -> None:
    nested_group_id = "33333333-3333-4333-8333-333333333333"

    def graph_handler(request: httpx.Request) -> httpx.Response:
        if "/permissions" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "roles": ["read"],
                            "grantedToV2": {"siteGroup": {"id": "4"}},
                        }
                    ]
                },
            )
        if "/groups/" in str(request.url):
            return httpx.Response(
                200, json={"id": nested_group_id, "securityEnabled": True}
            )
        raise AssertionError(f"unexpected Graph request: {request.url}")

    def sharepoint_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/_api/web/sitegroups(4)/users")
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "PrincipalType": 4,
                        "LoginName": f"c:0t.c|tenant|{nested_group_id}",
                    }
                ]
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(graph_handler)) as graph_client,
        httpx.Client(transport=httpx.MockTransport(sharepoint_handler)) as sp_client,
    ):
        connector = SharePointConnector(
            graph_client,
            "drive-1",
            sp_client=sp_client,
            site_url="https://contoso.sharepoint.com/sites/docs",
        )
        acl = connector.read_verified_acl("item-1", max_pages=5)

    assert acl.allowed_group_ids == (nested_group_id,)


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
