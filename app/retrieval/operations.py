"""Private one-shot deployment operations executed inside the ACA environment."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity import ManagedIdentityCredential

from retrieval.catalog import (
    CatalogConflictError,
    CatalogError,
    activate_catalog,
    build_catalog_item,
    load_catalog_item,
    publish_catalog,
)


class OperationsError(RuntimeError):
    """A private deployment operation failed without exposing sensitive details."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise OperationsError(f"{name} is required")
    return value


def publish_bootstrap_catalog() -> dict[str, str]:
    endpoint = _required("COSMOS_ENDPOINT")
    database_name = _required("COSMOS_DATABASE")
    container_name = _required("RETRIEVAL_CONFIG_CONTAINER")
    deployment_instance_id = _required("DEPLOYMENT_INSTANCE_ID")
    expected_digest = _required("EXPECTED_CATALOG_DIGEST")
    managed_identity_client_id = _required("MANAGED_IDENTITY_CLIENT_ID")
    source_path = Path(
        os.getenv("CATALOG_PATH", "/app/retrieval/catalog.example.json")
    )
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperationsError("catalog source cannot be read") from error
    if not isinstance(source, dict):
        raise OperationsError("catalog source must be an object")

    item = build_catalog_item(source, deployment_instance_id)
    catalog = load_catalog_item(item)
    if catalog.version != expected_digest:
        raise OperationsError("catalog digest does not match the reviewed artifact")

    credential = ManagedIdentityCredential(client_id=managed_identity_client_id)
    cosmos = CosmosClient(endpoint, credential=credential)
    try:
        container = cosmos.get_database_client(database_name).get_container_client(
            container_name
        )
        publish_catalog(container, item)
        try:
            pointer = container.read_item(
                item="active", partition_key=deployment_instance_id
            )
        except CosmosResourceNotFoundError:
            try:
                pointer = activate_catalog(
                    container,
                    catalog,
                    activated_by="stage5-private-operations",
                    expected_etag=None,
                )
            except CatalogConflictError:
                pointer = container.read_item(
                    item="active", partition_key=deployment_instance_id
                )
        if (
            pointer.get("catalogId") != catalog.catalog_id
            or pointer.get("version") != catalog.version
            or pointer.get("deploymentInstanceId") != deployment_instance_id
        ):
            raise OperationsError(
                "a different active catalog already exists for this deployment instance"
            )
        activation_etag = pointer.get("_etag")
        if not isinstance(activation_etag, str) or not activation_etag:
            raise OperationsError("active catalog pointer has no ETag")
        return {
            "catalogId": catalog.catalog_id,
            "catalogDigest": catalog.version,
            "activationEtag": activation_etag,
        }
    except CatalogError:
        raise
    except OperationsError:
        raise
    except Exception as error:
        raise OperationsError("private catalog publication failed") from error
    finally:
        cosmos.close()
        credential.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("publish-catalog",))
    args = parser.parse_args(argv)
    try:
        if args.operation == "publish-catalog":
            result = publish_bootstrap_catalog()
        else:
            raise OperationsError("unsupported operation")
    except (CatalogError, OperationsError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}))
        return 2
    print(json.dumps({"status": "succeeded", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
