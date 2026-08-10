from __future__ import annotations

import base64
import json
from unittest.mock import Mock

import pytest

from retrieval.auth import AuthorizationError, principal_from_easy_auth, require_easy_auth_role


def encode(claims: list[dict[str, str]]) -> str:
    payload = json.dumps({"claims": claims}).encode()
    return base64.b64encode(payload).decode()


def test_principal_reverifies_token_groups_with_graph() -> None:
    resolver = Mock()
    resolver.resolve_transitive_security_groups.return_value = {"security-group"}
    principal = principal_from_easy_auth(
        encode(
            [
                {"typ": "oid", "val": "user"},
                {"typ": "tid", "val": "tenant"},
                {"typ": "groups", "val": "group-2"},
                {"typ": "groups", "val": "group-1"},
            ]
        ),
        "tenant",
        resolver,
    )

    assert principal.acl_ids == ["security-group"]
    resolver.resolve_transitive_security_groups.assert_called_once_with("user")


def test_principal_uses_fallback_for_group_overage() -> None:
    resolver = Mock()
    resolver.resolve_transitive_security_groups.return_value = {"security-group"}
    principal = principal_from_easy_auth(
        encode(
            [
                {"typ": "oid", "val": "user"},
                {"typ": "tid", "val": "tenant"},
                {"typ": "hasgroups", "val": "true"},
            ]
        ),
        "tenant",
        resolver,
    )

    assert principal.acl_ids == ["security-group"]
    resolver.resolve_transitive_security_groups.assert_called_once_with("user")


def test_easy_auth_uri_claim_names_are_normalized() -> None:
    resolver = Mock()
    resolver.resolve_transitive_security_groups.return_value = {"security-group"}
    encoded = encode(
        [
            {
                "typ": "http://schemas.microsoft.com/identity/claims/objectidentifier",
                "val": "user",
            },
            {
                "typ": "http://schemas.microsoft.com/identity/claims/tenantid",
                "val": "tenant",
            },
            {
                "typ": "http://schemas.microsoft.com/ws/2008/06/identity/claims/role",
                "val": "Rag.Reconcile",
            },
        ]
    )

    principal = principal_from_easy_auth(encoded, "tenant", resolver)
    require_easy_auth_role(encoded, "tenant", "Rag.Reconcile")

    assert principal.user_id == "user"


@pytest.mark.parametrize(
    "encoded,tenant",
    [
        (None, "tenant"),
        ("not-base64", "tenant"),
        (encode([{"typ": "oid", "val": "user"}]), "tenant"),
        (
            encode(
                [
                    {"typ": "oid", "val": "user"},
                    {"typ": "tid", "val": "other"},
                ]
            ),
            "tenant",
        ),
    ],
)
def test_principal_fails_closed(encoded: str | None, tenant: str) -> None:
    with pytest.raises(AuthorizationError):
        principal_from_easy_auth(encoded, tenant)