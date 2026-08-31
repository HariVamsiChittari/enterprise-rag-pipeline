"""Validate, publish, activate, or roll back a Cosmos retrieval catalog.

Publish and rollback require the operator's reviewed active pointer identity
(``catalogId``, ``version``, ``ETag``) so a stale reviewer cannot overwrite a
newer pointer even when the ETag has since changed. The resulting pointer
``_etag`` is printed so a subsequent operation (rollback drill, compensation)
can supply it as an expected value.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "app"))

from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential

from retrieval.catalog import (
    CatalogError,
    activate_catalog,
    build_catalog_item,
    load_catalog_item,
    publish_catalog,
)


class StalePointerError(CatalogError):
    """Operator-reviewed active pointer identity does not match live state."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "publish", "rollback"))
    parser.add_argument("--file", type=Path)
    parser.add_argument("--deployment-instance-id", required=True)
    parser.add_argument("--endpoint")
    parser.add_argument("--database")
    parser.add_argument("--container", default="retrieval-config")
    parser.add_argument("--catalog-id")
    parser.add_argument("--activated-by", default="deployment-pipeline")
    parser.add_argument(
        "--expected-pointer-id",
        default="active",
        help="Expected active pointer document ID (default 'active').",
    )
    parser.add_argument(
        "--expected-pointer-version",
        help="Operator-reviewed active pointer 'version' field.",
    )
    parser.add_argument(
        "--expected-pointer-etag",
        help="Operator-reviewed active pointer ETag captured immediately before review.",
    )
    parser.add_argument(
        "--expect-no-pointer",
        action="store_true",
        help="Assert there is no active pointer (first-time bootstrap only).",
    )
    return parser


def _read_source(path: Path | None) -> dict:
    if path is None:
        raise CatalogError("--file is required for validate and publish")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CatalogError("catalog source file cannot be read") from error
    if not isinstance(value, dict):
        raise CatalogError("catalog source must be a JSON object")
    return value


def _container(args: argparse.Namespace):
    if not args.endpoint or not args.database:
        raise CatalogError("--endpoint and --database are required for publish and rollback")
    client = CosmosClient(args.endpoint, credential=DefaultAzureCredential())
    return client.get_database_client(args.database).get_container_client(args.container)


def _read_active_pointer(container, deployment_instance_id: str) -> dict | None:
    try:
        return container.read_item(
            item="active", partition_key=deployment_instance_id,
        )
    except Exception as error:
        if getattr(error, "status_code", None) == 404:
            return None
        raise CatalogError("active catalog pointer read failed") from None


def _verify_pointer_identity(pointer: dict | None, args: argparse.Namespace) -> str | None:
    """Compare live pointer to operator-reviewed identity. Returns the ETag on success."""
    if args.expect_no_pointer:
        if pointer is not None:
            raise StalePointerError(
                "expected no active pointer but one exists; refuse to overwrite"
            )
        return None
    if pointer is None:
        raise StalePointerError(
            "no active pointer found; pass --expect-no-pointer for first-time publish"
        )
    if not args.expected_pointer_version or not args.expected_pointer_etag:
        raise StalePointerError(
            "--expected-pointer-version and --expected-pointer-etag are required "
            "for publish and rollback against an existing active pointer"
        )
    if pointer.get("id") != args.expected_pointer_id:
        raise StalePointerError("active pointer id does not match --expected-pointer-id")
    if pointer.get("version") != args.expected_pointer_version:
        raise StalePointerError("active pointer version does not match reviewer state")
    live_etag = pointer.get("_etag")
    if not isinstance(live_etag, str) or not live_etag:
        raise CatalogError("active catalog pointer has no ETag")
    if live_etag != args.expected_pointer_etag:
        raise StalePointerError(
            "active pointer ETag has changed since review; refuse compare-and-swap"
        )
    return live_etag


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "validate":
            item = build_catalog_item(
                _read_source(args.file), args.deployment_instance_id,
            )
            print(json.dumps({
                "catalogId": item["id"],
                "catalogDigest": item["version"],
            }))
            return 0

        container = _container(args)
        pointer = _read_active_pointer(container, args.deployment_instance_id)
        expected_etag = _verify_pointer_identity(pointer, args)

        if args.action == "publish":
            item = build_catalog_item(
                _read_source(args.file), args.deployment_instance_id,
            )
            publish_catalog(container, item)
            catalog = load_catalog_item(item)
        else:
            if not args.catalog_id:
                raise CatalogError("--catalog-id is required for rollback")
            try:
                persisted = container.read_item(
                    item=args.catalog_id,
                    partition_key=args.deployment_instance_id,
                )
            except Exception:
                raise CatalogError("rollback catalog cannot be read") from None
            catalog = load_catalog_item(persisted)

        activated = activate_catalog(
            container,
            catalog,
            activated_by=args.activated_by,
            expected_etag=expected_etag,
        )
        activation_etag = activated.get("_etag") if isinstance(activated, dict) else None
        print(json.dumps({
            "catalogId": catalog.catalog_id,
            "catalogDigest": catalog.version,
            "activationEtag": activation_etag,
        }))
        return 0
    except CatalogError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
