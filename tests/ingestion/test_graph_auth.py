from __future__ import annotations

from unittest.mock import Mock

import httpx

from ingestion.graph import GraphCredentialAuth


def test_graph_auth_gets_a_token_for_each_request() -> None:
    credential = Mock()
    credential.get_token.side_effect = [Mock(token="token-1"), Mock(token="token-2")]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200)

    with httpx.Client(
        auth=GraphCredentialAuth(credential, "https://graph.microsoft.com/.default"),
        transport=httpx.MockTransport(handler),
    ) as client:
        client.get("https://graph.microsoft.com/v1.0/drives")
        client.get("https://graph.microsoft.com/v1.0/groups")

    assert seen == ["Bearer token-1", "Bearer token-2"]
    assert credential.get_token.call_count == 2