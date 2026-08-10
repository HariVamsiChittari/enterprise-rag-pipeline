from __future__ import annotations

from dataclasses import replace
import json

import httpx
import pytest

from ingestion.errors import TerminalDocumentError
from ingestion.graph import (
    CHILDREN_SELECT,
    ChildrenPage,
    DiscoveryState,
    FolderCursor,
    GraphDiscoveryLimitExceeded,
    advance_discovery,
    discover_next_page,
    read_verified_acl,
)
from ingestion.models import ScaleLimits


def pdf_item(item_id: str, name: str | None = None, size: int = 100) -> dict:
    pdf_name = name or f"{item_id}.pdf"
    return {
        "id": item_id,
        "name": pdf_name,
        "eTag": f"etag-{item_id}",
        "size": size,
        "webUrl": f"https://contoso.sharepoint.com/{pdf_name}",
        "file": {"mimeType": "application/pdf"},
        "parentReference": {"id": "parent", "path": "/drives/drive/root:/Docs"},
    }


def test_discovery_pages_root_then_nested_folders_and_preserves_next_link() -> None:
    opaque_next = (
        "https://graph.microsoft.com/v1.0/drives/drive/root/children"
        "?$skiptoken=a%2Fb%2Bc%3D&custom=%24opaque"
    )
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if len(requested_urls) == 1:
            assert request.url.params["$select"] == CHILDREN_SELECT
            assert request.url.params["$orderby"] == "name asc"
            return httpx.Response(
                200,
                json={
                    "value": [
                        pdf_item("root-1"),
                        {"id": "folder", "name": "Nested", "folder": {}},
                        {"id": "ignored", "name": "notes.txt", "file": {"mimeType": "text/plain"}},
                    ],
                    "@odata.nextLink": opaque_next,
                },
            )
        if len(requested_urls) == 2:
            return httpx.Response(200, json={"value": [pdf_item("root-2")]})
        assert request.url.path.endswith("/items/folder/children")
        return httpx.Response(200, json={"value": [pdf_item("nested")]})

    state = DiscoveryState.initial()
    discovered = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        while not state.complete:
            step = discover_next_page(client, "drive", state, ScaleLimits())
            state = step.state
            discovered.extend(step.pdfs)

    assert requested_urls[1] == opaque_next
    assert [item.item_id for item in discovered] == ["root-1", "root-2", "nested"]
    assert [item.discovery_ordinal for item in discovered] == [0, 1, 2]
    assert discovered[0].source_path == "/drives/drive/root:/Docs/root-1.pdf"
    assert state == DiscoveryState((), 5, 1, 3, 3)


def test_discovery_state_and_step_are_json_serializable() -> None:
    state = DiscoveryState(
        (FolderCursor("folder", 2, "https://graph.microsoft.com/v1.0/next"),),
        items_scanned=10,
        folders_discovered=3,
        pdfs_discovered=4,
        graph_pages=2,
    )
    step = advance_discovery(
        DiscoveryState.initial(),
        ChildrenPage((pdf_item("pdf"),), None),
        ScaleLimits(),
    )

    assert DiscoveryState.from_dict(json.loads(json.dumps(state.to_dict()))) == state
    assert json.loads(json.dumps(step.to_dict()))["pdfs"][0]["itemId"] == "pdf"


def test_discovery_traverses_package_facets_as_containers() -> None:
    step = advance_discovery(
        DiscoveryState.initial(),
        ChildrenPage(({"id": "package", "name": "Bundle", "package": {}},), None),
        ScaleLimits(),
    )
    assert step.state.pending_folders == (FolderCursor("package", 1),)


@pytest.mark.parametrize(
    ("limits", "state", "page", "code"),
    [
        (ScaleLimits(max_drive_items=1), DiscoveryState.initial(), ChildrenPage(({"id": "one"}, {"id": "two"}), None), "max_drive_items_exceeded"),
        (ScaleLimits(max_folders=1), DiscoveryState.initial(), ChildrenPage(({"id": "one", "folder": {}}, {"id": "two", "folder": {}}), None), "max_folders_exceeded"),
        (ScaleLimits(max_folder_depth=1), DiscoveryState((FolderCursor("parent", 1),)), ChildrenPage(({"id": "child", "folder": {}},), None), "max_folder_depth_exceeded"),
        (ScaleLimits(max_eligible_pdfs=1), DiscoveryState.initial(), ChildrenPage((pdf_item("one"), pdf_item("two")), None), "max_eligible_pdfs_exceeded"),
        (ScaleLimits(max_pdf_bytes=99), DiscoveryState.initial(), ChildrenPage((pdf_item("large", size=100),), None), "max_pdf_bytes_exceeded"),
    ],
)
def test_discovery_enforces_guards(limits, state, page, code: str) -> None:
    with pytest.raises(GraphDiscoveryLimitExceeded, match=code):
        advance_discovery(state, page, limits)


def test_discovery_enforces_page_guard_before_request() -> None:
    requests: list[httpx.Request] = []
    state = replace(DiscoveryState.initial(), graph_pages=1)
    with httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={}))) as client:
        with pytest.raises(GraphDiscoveryLimitExceeded, match="max_graph_pages_exceeded"):
            discover_next_page(client, "drive", state, ScaleLimits(max_graph_pages=1))
    assert requests == []


@pytest.mark.parametrize("next_link", ["http://graph.microsoft.com/v1.0/next", "https://evil.example/v1.0/next", "https://user@graph.microsoft.com/v1.0/next", "https://graph.microsoft.com:444/v1.0/next"])
def test_discovery_rejects_unsafe_next_link(next_link: str) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"value": [], "@odata.nextLink": next_link}))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(ValueError, match="Graph paging URL is not allowed"):
            discover_next_page(client, "drive", DiscoveryState.initial(), ScaleLimits())


def test_read_verified_acl_returns_sorted_verified_security_groups() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/permissions"):
            return httpx.Response(200, json={"value": [{"roles": ["read"], "grantedToIdentitiesV2": [{"group": {"id": "group-b"}}, {"group": {"id": "group-a"}}]}]})
        group_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"id": group_id, "securityEnabled": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        acl = read_verified_acl(client, "drive", "item", 2)
    assert acl.allowed_group_ids == ("group-a", "group-b")
    assert len(acl.acl_hash) == 64
    assert requests[0].endswith("/permissions")
    assert all("/content" not in path for path in requests)


@pytest.mark.parametrize("permission", [
    {"roles": ["read"], "grantedToV2": {"user": {"id": "user"}}},
    {"roles": ["read"], "grantedToV2": {"sharePointGroup": {"id": "4"}}},
    {"roles": ["read"], "link": {"type": "view"}, "grantedToV2": {"group": {"id": "group"}}},
    {"roles": ["unknown"], "grantedToV2": {"group": {"id": "group"}}},
    {"roles": ["read"], "grantedToV2": {"group": {}}},
])
def test_read_verified_acl_rejects_unsupported_or_malformed_permissions(permission: dict) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"value": [permission]}))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(TerminalDocumentError, match="unsafe_acl"):
            read_verified_acl(client, "drive", "item", 1)


@pytest.mark.parametrize("security_enabled", [False])
def test_read_verified_acl_rejects_unverified_groups(security_enabled: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/permissions"):
            return httpx.Response(200, json={"value": [{"roles": ["read"], "grantedToV2": {"group": {"id": "group"}}}]})
        return httpx.Response(200, json={"id": "group", "securityEnabled": security_enabled})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TerminalDocumentError, match="unsafe_acl:no_verified_security_groups"):
            read_verified_acl(client, "drive", "item", 1)


def test_read_verified_acl_accepts_group_not_found_in_graph() -> None:
    """Groups returning 404 are accepted (app may lack Group.Read.All)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/permissions"):
            return httpx.Response(200, json={"value": [{"roles": ["read"], "grantedToV2": {"group": {"id": "group"}}}]})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        acl = read_verified_acl(client, "drive", "item", 1)
    assert acl.allowed_group_ids == ("group",)