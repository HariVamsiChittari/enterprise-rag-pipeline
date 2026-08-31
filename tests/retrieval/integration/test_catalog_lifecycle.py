"""Env-gated live-Cosmos validation for retrieval catalog publication and rollback.

Requires a disposable `retrieval-config` container and a principal with container-scoped
Data Contributor. The test uses a unique partition and cleans up every created item.

  $env:RAG_INTEGRATION_COSMOS_ENDPOINT="https://<account>.documents.azure.com:443/"
  $env:RAG_INTEGRATION_COSMOS_DATABASE="rag-db"
  $env:RAG_INTEGRATION_COSMOS_CONFIG_CONTAINER="retrieval-config"
  python -m pytest tests/retrieval/integration/test_catalog_lifecycle.py -q
"""

from __future__ import annotations

import os
import uuid

import pytest

from retrieval.catalog import (
    CatalogConflictError,
    CosmosCatalogLoader,
    activate_catalog,
    build_catalog_item,
    load_catalog_item,
    publish_catalog,
)

_REQUIRED_ENV = (
    "RAG_INTEGRATION_COSMOS_ENDPOINT",
    "RAG_INTEGRATION_COSMOS_DATABASE",
    "RAG_INTEGRATION_COSMOS_CONFIG_CONTAINER",
)

pytestmark = pytest.mark.skipif(
    any(not os.getenv(name) for name in _REQUIRED_ENV),
    reason="Live catalog integration test; set RAG_INTEGRATION_COSMOS_* env vars to run",
)


def _source(over_fetch_factor: int) -> dict:
    return {
        "schemaVersion": 1,
        "config": {
            "retrieval": {
                "overFetchFactor": over_fetch_factor,
                "hybridWeights": {"vector": 2.0, "text": 1.0},
                "fullTextScoreScope": "Global",
            },
            "defaultProfile": None,
            "synonymsEnabled": False,
            "profiles": [],
            "synonymMaps": [],
        },
    }


@pytest.fixture
def live_catalog():
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    client = CosmosClient(
        os.environ["RAG_INTEGRATION_COSMOS_ENDPOINT"],
        credential=DefaultAzureCredential(),
    )
    container = (
        client.get_database_client(os.environ["RAG_INTEGRATION_COSMOS_DATABASE"])
        .get_container_client(os.environ["RAG_INTEGRATION_COSMOS_CONFIG_CONTAINER"])
    )
    environment = f"integration-{uuid.uuid4()}"
    created_ids: list[str] = []
    try:
        yield container, environment, created_ids
    finally:
        for item_id in reversed(created_ids):
            try:
                container.delete_item(item=item_id, partition_key=environment)
            except Exception:
                pass


def test_publish_activate_conflict_load_and_rollback(live_catalog) -> None:
    container, environment, created_ids = live_catalog
    first_item = build_catalog_item(_source(3), environment)
    second_item = build_catalog_item(_source(4), environment)
    publish_catalog(container, first_item)
    publish_catalog(container, first_item)
    publish_catalog(container, second_item)
    created_ids.extend([first_item["id"], second_item["id"]])

    first = load_catalog_item(first_item)
    second = load_catalog_item(second_item)
    pointer = activate_catalog(
        container, first, activated_by="integration-test", expected_etag=None,
    )
    created_ids.append("active")
    assert CosmosCatalogLoader(container, environment).load().over_fetch_factor == 3

    with pytest.raises(CatalogConflictError):
        activate_catalog(
            container, second, activated_by="integration-test", expected_etag="stale",
        )

    pointer = activate_catalog(
        container,
        second,
        activated_by="integration-test",
        expected_etag=pointer["_etag"],
    )
    assert CosmosCatalogLoader(container, environment).load().over_fetch_factor == 4

    activate_catalog(
        container,
        first,
        activated_by="integration-test",
        expected_etag=pointer["_etag"],
    )
    assert CosmosCatalogLoader(container, environment).load().version == first.version
