"""Identity contracts for requests authenticated by App Service Authentication."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx


CLAIM_TYPE_ALIASES = {
    "http://schemas.microsoft.com/identity/claims/objectidentifier": "oid",
    "http://schemas.microsoft.com/identity/claims/tenantid": "tid",
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups": "groups",
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/role": "roles",
}


class AuthorizationError(ValueError):
    pass


class GroupResolver(Protocol):
    def resolve_transitive_security_groups(self, user_id: str) -> set[str]: ...


class GraphGroupResolver:
    def __init__(self, client: httpx.Client, max_pages: int = 20) -> None:
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self._client = client
        self._max_pages = max_pages

    def resolve_transitive_security_groups(self, user_id: str) -> set[str]:
        if not user_id:
            raise AuthorizationError("user_id_required")
        url: str | None = (
            "https://graph.microsoft.com/v1.0/users/"
            f"{quote(user_id, safe='')}/transitiveMemberOf/microsoft.graph.group"
            "?$select=id,securityEnabled"
        )
        group_ids: set[str] = set()
        pages = 0
        while url:
            if pages >= self._max_pages:
                raise AuthorizationError("group_page_limit_exceeded")
            parsed = httpx.URL(url)
            if parsed.scheme != "https" or parsed.host != "graph.microsoft.com":
                raise AuthorizationError("invalid_group_paging_url")
            response = self._client.get(url)
            response.raise_for_status()
            payload = response.json()
            values = payload.get("value") if isinstance(payload, dict) else None
            if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
                raise AuthorizationError("invalid_group_response")
            for item in values:
                group_id = item.get("id")
                security_enabled = item.get("securityEnabled")
                if not isinstance(group_id, str) or not isinstance(security_enabled, bool):
                    raise AuthorizationError("invalid_group_response")
                if security_enabled:
                    group_ids.add(group_id)
            next_link = payload.get("@odata.nextLink")
            if next_link is not None and not isinstance(next_link, str):
                raise AuthorizationError("invalid_group_paging_url")
            url = next_link
            pages += 1
        return group_ids


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    security_group_ids: frozenset[str]

    @property
    def acl_ids(self) -> list[str]:
        return sorted(self.security_group_ids)


def principal_from_easy_auth(
    encoded_principal: str | None,
    expected_tenant_id: str,
    group_resolver: GroupResolver | None = None,
    *,
    acl_enabled: bool = True,
) -> Principal:
    by_type = _claims_by_type(encoded_principal)
    user_id = _single_claim(by_type, "oid")
    tenant_id = _single_claim(by_type, "tid")
    if tenant_id != expected_tenant_id:
        raise AuthorizationError("unexpected_tenant")

    if not acl_enabled:
        return Principal(user_id, tenant_id, frozenset())

    if group_resolver is None:
        raise AuthorizationError("security_groups_unresolved")
    try:
        group_ids = group_resolver.resolve_transitive_security_groups(user_id)
    except Exception as error:
        raise AuthorizationError("security_groups_unresolved") from error
    if not group_ids or not all(isinstance(group_id, str) and group_id for group_id in group_ids):
        raise AuthorizationError("security_groups_unresolved")
    return Principal(user_id, tenant_id, frozenset(group_ids))


def require_easy_auth_role(
    encoded_principal: str | None,
    expected_tenant_id: str,
    required_role: str,
) -> None:
    if not required_role:
        raise AuthorizationError("required_role_missing")
    claims = _claims_by_type(encoded_principal)
    if _single_claim(claims, "tid") != expected_tenant_id:
        raise AuthorizationError("unexpected_tenant")
    if required_role not in claims.get("roles", []):
        raise AuthorizationError("required_role_missing")


def _claims_by_type(encoded_principal: str | None) -> dict[str, list[str]]:
    if not encoded_principal:
        raise AuthorizationError("missing_authenticated_principal")
    try:
        padding = "=" * (-len(encoded_principal) % 4)
        payload = json.loads(base64.b64decode(encoded_principal + padding, validate=True))
    except (ValueError, json.JSONDecodeError) as error:
        raise AuthorizationError("invalid_authenticated_principal") from error
    if not isinstance(payload, dict):
        raise AuthorizationError("invalid_authenticated_principal")
    claims = payload.get("claims")
    if not isinstance(claims, list) or not all(isinstance(claim, dict) for claim in claims):
        raise AuthorizationError("invalid_authenticated_claims")
    by_type: dict[str, list[str]] = {}
    for claim in claims:
        claim_type = claim.get("typ")
        value = claim.get("val")
        if isinstance(claim_type, str) and isinstance(value, str) and value:
            normalized_type = CLAIM_TYPE_ALIASES.get(claim_type, claim_type)
            by_type.setdefault(normalized_type, []).append(value)
    return by_type


def _single_claim(claims: dict[str, list[str]], claim_type: str) -> str:
    values = claims.get(claim_type, [])
    if len(values) != 1:
        raise AuthorizationError(f"invalid_{claim_type}_claim")
    return values[0]