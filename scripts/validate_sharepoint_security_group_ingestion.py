#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from azure.identity import CertificateCredential, DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from ingestion.graph import GRAPH_ROOT, GraphCredentialAuth  # noqa: E402
from ingestion.run import GRAPH_SCOPE, decode_pfx  # noqa: E402


@dataclass(frozen=True)
class ValidationConfig:
    tenant_id: str
    client_id: str
    certificate_secret_name: str
    key_vault_uri: str
    drive_id: str
    owner_email: str
    member_emails: tuple[str, ...]
    target_item_id: str
    group_display_name: str
    group_mail_nickname: str
    timeout_seconds: float
    membership_wait_seconds: float
    membership_poll_interval_seconds: float
    graph_access_token: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a security group, add members, grant it access to one SharePoint item, "
            "and validate effective Graph-based access."
        )
    )
    parser.add_argument("--owner-email", default=os.getenv("VALIDATION_GROUP_OWNER_EMAIL"))
    parser.add_argument(
        "--member-email",
        action="append",
        dest="member_emails",
        default=None,
        help="Repeat for each user email to add to the new security group.",
    )
    parser.add_argument("--target-item-id", default=os.getenv("VALIDATION_TARGET_ITEM_ID"))
    parser.add_argument(
        "--group-display-name",
        default=os.getenv("VALIDATION_GROUP_DISPLAY_NAME", "Rag Project Ingestion Validation"),
    )
    parser.add_argument(
        "--group-mail-nickname",
        default=os.getenv("VALIDATION_GROUP_MAIL_NICKNAME", "rag-project-ingestion-validation"),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.getenv("VALIDATION_TIMEOUT_SECONDS", "30")),
    )
    parser.add_argument(
        "--membership-wait-seconds",
        type=float,
        default=float(os.getenv("VALIDATION_MEMBERSHIP_WAIT_SECONDS", "90")),
    )
    parser.add_argument(
        "--membership-poll-interval-seconds",
        type=float,
        default=float(os.getenv("VALIDATION_MEMBERSHIP_POLL_INTERVAL_SECONDS", "5")),
    )
    parser.add_argument(
        "--graph-access-token",
        default=os.getenv("VALIDATION_GRAPH_ACCESS_TOKEN"),
        help="Optional delegated Microsoft Graph bearer token for local validation runs.",
    )
    return parser.parse_args()


def _required(name: str) -> str:
    value = os.getenv(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing_environment:{name}")
    return value.strip()


def config_from(args: argparse.Namespace) -> ValidationConfig:
    raw_members = args.member_emails or []
    if not raw_members:
        env_members = os.getenv("VALIDATION_GROUP_MEMBER_EMAILS", "")
        raw_members = [member.strip() for member in env_members.split(",") if member.strip()]

    owner_email = (args.owner_email or "").strip()
    target_item_id = (args.target_item_id or "").strip()
    group_display_name = (args.group_display_name or "").strip()
    group_mail_nickname = (args.group_mail_nickname or "").strip()
    unique_members = tuple(_dedupe_emails(raw_members))

    if not owner_email:
        raise ValueError("owner_email_required")
    if not unique_members:
        raise ValueError("member_emails_required")
    if not target_item_id:
        raise ValueError("target_item_id_required")
    if not group_display_name:
        raise ValueError("group_display_name_required")
    if not group_mail_nickname:
        raise ValueError("group_mail_nickname_required")
    if args.timeout_seconds <= 0:
        raise ValueError("timeout_seconds_invalid")
    if args.membership_wait_seconds <= 0:
        raise ValueError("membership_wait_seconds_invalid")
    if args.membership_poll_interval_seconds <= 0:
        raise ValueError("membership_poll_interval_seconds_invalid")

    return ValidationConfig(
        tenant_id=_required("SHAREPOINT_TENANT_ID"),
        client_id=_required("SHAREPOINT_APP_CLIENT_ID"),
        certificate_secret_name=_required("SHAREPOINT_CERTIFICATE_SECRET_NAME"),
        key_vault_uri=_required("KEY_VAULT_URI"),
        drive_id=_required("SHAREPOINT_ASSIGNED_DRIVE_ID"),
        owner_email=owner_email,
        member_emails=unique_members,
        target_item_id=target_item_id,
        group_display_name=group_display_name,
        group_mail_nickname=group_mail_nickname,
        timeout_seconds=args.timeout_seconds,
        membership_wait_seconds=args.membership_wait_seconds,
        membership_poll_interval_seconds=args.membership_poll_interval_seconds,
        graph_access_token=(
            args.graph_access_token.strip()
            if isinstance(args.graph_access_token, str) and args.graph_access_token.strip()
            else None
        ),
    )


def _dedupe_emails(values: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _graph_json(client: httpx.Client, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    response = client.request(method, url, **kwargs)
    response.raise_for_status()
    if not response.content:
        return {}
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("graph_response_invalid")
    return payload


def _resolve_user(client: httpx.Client, email: str) -> dict[str, str]:
    payload = _graph_json(
        client,
        "GET",
        f"{GRAPH_ROOT}/users/{email}",
        params={"$select": "id,displayName,userPrincipalName"},
    )
    user_id = payload.get("id")
    user_principal_name = payload.get("userPrincipalName")
    display_name = payload.get("displayName")
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_lookup_invalid")
    if not isinstance(user_principal_name, str) or not user_principal_name:
        raise ValueError("user_lookup_invalid")
    return {
        "id": user_id,
        "userPrincipalName": user_principal_name,
        "displayName": display_name if isinstance(display_name, str) and display_name else user_principal_name,
    }


def _group_create_payload(config: ValidationConfig, owner_id: str, member_ids: list[str]) -> dict[str, Any]:
    if not member_ids:
        raise ValueError("member_ids_required")
    return {
        "displayName": config.group_display_name,
        "mailEnabled": False,
        "mailNickname": config.group_mail_nickname,
        "securityEnabled": True,
        "groupTypes": [],
        "owners@odata.bind": [f"{GRAPH_ROOT}/users/{owner_id}"],
        "members@odata.bind": [f"{GRAPH_ROOT}/directoryObjects/{member_id}" for member_id in member_ids],
    }


def _create_group(client: httpx.Client, config: ValidationConfig, owner_id: str, member_ids: list[str]) -> dict[str, str]:
    payload = _graph_json(client, "POST", f"{GRAPH_ROOT}/groups", json=_group_create_payload(config, owner_id, member_ids))
    group_id = payload.get("id")
    display_name = payload.get("displayName")
    if not isinstance(group_id, str) or not group_id:
        raise ValueError("group_create_invalid")
    return {
        "id": group_id,
        "displayName": display_name if isinstance(display_name, str) and display_name else config.group_display_name,
    }


def _transitive_member_upns(payload: dict[str, Any]) -> set[str]:
    values = payload.get("value")
    if not isinstance(values, list):
        raise ValueError("transitive_members_invalid")
    members: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        upn = item.get("userPrincipalName")
        if isinstance(upn, str) and upn:
            members.add(upn.lower())
    return members


def _wait_for_membership(client: httpx.Client, group_id: str, expected_upns: set[str], config: ValidationConfig) -> None:
    deadline = time.monotonic() + config.membership_wait_seconds
    while True:
        try:
            payload = _graph_json(
                client,
                "GET",
                f"{GRAPH_ROOT}/groups/{group_id}/transitiveMembers/microsoft.graph.user",
                params={"$select": "id,userPrincipalName"},
                headers={"ConsistencyLevel": "eventual"},
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
            payload = {"value": []}
        visible = _transitive_member_upns(payload)
        if expected_upns.issubset(visible):
            return
        if time.monotonic() >= deadline:
            missing = sorted(expected_upns - visible)
            raise RuntimeError(f"membership_validation_failed:{','.join(missing)}")
        time.sleep(config.membership_poll_interval_seconds)


def _grant_item_permission(
    client: httpx.Client,
    drive_id: str,
    item_id: str,
    group_alias: str,
) -> list[dict[str, Any]]:
    payload = _graph_json(
        client,
        "POST",
        f"{GRAPH_ROOT}/drives/{drive_id}/items/{item_id}/invite",
        json={
            "recipients": [{"alias": group_alias}],
            "requireSignIn": True,
            "sendInvitation": False,
            "roles": ["read"],
        },
    )
    permissions = payload.get("value")
    if not isinstance(permissions, list):
        raise ValueError("invite_response_invalid")
    return [permission for permission in permissions if isinstance(permission, dict)]


def _permission_contains_group(permission: dict[str, Any], group_id: str) -> bool:
    identities: list[dict[str, Any]] = []
    granted_to = permission.get("grantedToV2")
    granted_many = permission.get("grantedToIdentitiesV2")
    if isinstance(granted_to, dict):
        identities.append(granted_to)
    if isinstance(granted_many, list):
        identities.extend(item for item in granted_many if isinstance(item, dict))
    for identity in identities:
        group = identity.get("group")
        if isinstance(group, dict) and group.get("id") == group_id:
            return True
    return False


def _validate_item_permissions(client: httpx.Client, drive_id: str, item_id: str, group_id: str) -> int:
    payload = _graph_json(client, "GET", f"{GRAPH_ROOT}/drives/{drive_id}/items/{item_id}/permissions")
    permissions = payload.get("value")
    if not isinstance(permissions, list):
        raise ValueError("permissions_response_invalid")
    matching = [permission for permission in permissions if isinstance(permission, dict) and _permission_contains_group(permission, group_id)]
    if not matching:
        raise RuntimeError("permission_validation_failed")
    return len(matching)


def _build_graph_client(config: ValidationConfig, stack: ExitStack) -> httpx.Client:
    if config.graph_access_token is not None:
        return stack.enter_context(
            httpx.Client(
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {config.graph_access_token}",
                },
                timeout=config.timeout_seconds,
                follow_redirects=False,
            )
        )
    azure_credential = stack.enter_context(
        DefaultAzureCredential(managed_identity_client_id=os.getenv("AZURE_CLIENT_ID"))
    )
    secret_client = stack.enter_context(SecretClient(config.key_vault_uri, azure_credential))
    secret = secret_client.get_secret(config.certificate_secret_name)
    if secret.value is None:
        raise ValueError("certificate_secret_empty")
    graph_credential = stack.enter_context(
        CertificateCredential(
            tenant_id=config.tenant_id,
            client_id=config.client_id,
            certificate_data=decode_pfx(secret.value),
        )
    )
    graph_auth = GraphCredentialAuth(graph_credential, GRAPH_SCOPE)
    return stack.enter_context(
        httpx.Client(
            auth=graph_auth,
            headers={"Accept": "application/json"},
            timeout=config.timeout_seconds,
            follow_redirects=False,
        )
    )


def run_validation(config: ValidationConfig) -> dict[str, Any]:
    with ExitStack() as stack:
        client = _build_graph_client(config, stack)
        owner = _resolve_user(client, config.owner_email)
        members = [_resolve_user(client, email) for email in config.member_emails]
        group = _create_group(client, config, owner["id"], [member["id"] for member in members])
        expected_upns = {member["userPrincipalName"].lower() for member in members}
        _wait_for_membership(client, group["id"], expected_upns, config)
        _grant_item_permission(client, config.drive_id, config.target_item_id, config.group_mail_nickname)
        permission_count = _validate_item_permissions(client, config.drive_id, config.target_item_id, group["id"])
        return {
            "status": "validated",
            "group": group,
            "owner": owner,
            "members": members,
            "targetItemId": config.target_item_id,
            "matchingPermissionCount": permission_count,
        }


def main() -> int:
    config = config_from(parse_args())
    result = run_validation(config)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())