from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from azure.cosmos.exceptions import CosmosResourceExistsError, CosmosResourceNotFoundError

from retrieval.catalog import (
    CatalogConflictError,
    CatalogError,
    CosmosCatalogLoader,
    activate_catalog,
    build_catalog_item,
    load_catalog_item,
    publish_catalog,
)


def _source() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "config": {
            "retrieval": {
                "overFetchFactor": 3,
                "hybridWeights": {"vector": 2.0, "text": 1.0},
                "fullTextScoreScope": "Global",
            },
            "defaultProfile": "hr",
            "synonymsEnabled": True,
            "profiles": [
                {
                    "name": "hr",
                    "synonymMap": "hr-en",
                    "textWeights": {"sourceName": 1.5, "content": 1.0},
                    "functionAggregation": "sum",
                    "functions": [
                        {
                            "type": "freshness",
                            "fieldName": "sourceModifiedAt",
                            "boost": 0.15,
                            "interpolation": "linear",
                            "freshness": {"boostingDuration": "P180D"},
                        },
                    ],
                }
            ],
            "synonymMaps": [
                {
                    "name": "hr-en",
                    "format": "solr",
                    "rules": ["annual leave, vacation, paid time off"],
                }
            ],
        },
    }


class FakeContainer:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.etag = "etag-1"

    def create_item(self, *, body: dict[str, Any]) -> dict[str, Any]:
        key = (body["deploymentInstanceId"], body["id"])
        if key in self.items:
            raise CosmosResourceExistsError(status_code=409, message="exists")
        stored = dict(body)
        stored["_etag"] = self.etag
        self.items[key] = stored
        return stored

    def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        try:
            return dict(self.items[(partition_key, item)])
        except KeyError:
            raise CosmosResourceNotFoundError(status_code=404, message="missing") from None

    def replace_item(
        self, *, item: str, body: dict[str, Any], etag: str, match_condition: Any,
    ) -> dict[str, Any]:
        key = (body["deploymentInstanceId"], item)
        if key not in self.items:
            raise CosmosResourceNotFoundError(status_code=404, message="missing")
        if etag != self.items[key]["_etag"]:
            error = RuntimeError("conflict")
            error.status_code = 412  # type: ignore[attr-defined]
            raise error
        stored = dict(body)
        stored["_etag"] = "etag-2"
        self.items[key] = stored
        return stored


def _item() -> dict[str, Any]:
    return build_catalog_item(
        _source(), "dev", created_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
    )


def test_build_catalog_is_deterministic_and_loads_runtime_values() -> None:
    first = _item()
    second = _item()
    assert first["id"] == second["id"]
    catalog = load_catalog_item(first)
    assert catalog.over_fetch_factor == 3
    assert catalog.hybrid_weights == (2.0, 1.0)
    assert catalog.default_profile == "hr"
    assert set(catalog.profiles) == {"hr"}
    assert set(catalog.synonym_maps) == {"hr-en"}


def test_build_catalog_normalizes_integral_floats_for_cosmos_persistence() -> None:
    source = _source()

    item = build_catalog_item(source, "dev")

    assert source["config"]["retrieval"]["hybridWeights"] == {
        "vector": 2.0,
        "text": 1.0,
    }
    assert item["config"]["retrieval"]["hybridWeights"] == {
        "vector": 2,
        "text": 1,
    }
    assert item["config"]["profiles"][0]["textWeights"]["content"] == 1
    assert item["config"]["profiles"][0]["textWeights"]["sourceName"] == 1.5
    assert load_catalog_item(item).hybrid_weights == (2.0, 1.0)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("config", "profiles", 0, "textWeights", "unknown"), 1.0, "invalid"),
        (("config", "profiles", 0, "functions", 0, "fieldName"), "createdAt", "invalid"),
    ],
)
def test_catalog_rejects_unsupported_signals(path, value, message) -> None:
    source = _source()
    target: Any = source
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(CatalogError, match=message):
        build_catalog_item(source, "dev")


def test_catalog_rejects_nonfinite_numbers() -> None:
    source = _source()
    source["config"]["retrieval"]["hybridWeights"]["vector"] = float("nan")
    with pytest.raises(CatalogError, match="finite"):
        build_catalog_item(source, "dev")


def test_catalog_rejects_unknown_authoring_top_level_field() -> None:
    source = _source()
    source["guardrails"] = {"rejectUnknownFields": False}
    with pytest.raises(CatalogError, match="unknown top-level"):
        build_catalog_item(source, "dev")


def test_catalog_rejects_item_over_conservative_size_limit() -> None:
    source = _source()
    source["config"]["synonymMaps"][0]["rules"] = [
        "a, " + ("x" * 1_573_000)
    ]
    with pytest.raises(CatalogError, match="exceeds"):
        build_catalog_item(source, "dev")


def test_catalog_loader_rejects_oversized_persisted_item() -> None:
    item = _item()
    item["config"]["synonymMaps"][0]["rules"] = ["a, " + ("x" * 1_573_000)]
    with pytest.raises(CatalogError, match="exceeds"):
        load_catalog_item(item)


def test_catalog_loader_rejects_unknown_underscore_property() -> None:
    item = _item()
    item["_custom"] = "must not be silently discarded"
    with pytest.raises(CatalogError, match="Additional properties"):
        load_catalog_item(item)


def test_catalog_rejects_unknown_default_profile_and_map_reference() -> None:
    source = _source()
    source["config"]["defaultProfile"] = "missing"
    with pytest.raises(CatalogError, match="default profile"):
        build_catalog_item(source, "dev")

    source = _source()
    source["config"]["profiles"][0]["synonymMap"] = "missing"
    with pytest.raises(CatalogError, match="unknown synonym map"):
        build_catalog_item(source, "dev")


def test_catalog_detects_content_tampering() -> None:
    item = _item()
    item["config"]["retrieval"]["overFetchFactor"] = 4
    with pytest.raises(CatalogError, match="does not match"):
        load_catalog_item(item)


def test_publish_is_idempotent_and_loader_reads_exact_pinned_item() -> None:
    container = FakeContainer()
    item = _item()
    publish_catalog(container, item)
    publish_catalog(container, item)
    catalog = load_catalog_item(item)
    activate_catalog(
        container,
        catalog,
        activated_by="deployment-pipeline",
        expected_etag=None,
        activated_at=datetime(2026, 8, 26, 1, tzinfo=timezone.utc),
    )
    loaded = CosmosCatalogLoader(container, "dev", catalog.version).load()
    assert loaded.version == catalog.version


def test_activation_requires_current_etag_and_supports_rollback() -> None:
    container = FakeContainer()
    first_item = _item()
    second_source = _source()
    second_source["config"]["retrieval"]["overFetchFactor"] = 4
    second_item = build_catalog_item(second_source, "dev")
    publish_catalog(container, first_item)
    publish_catalog(container, second_item)
    first = load_catalog_item(first_item)
    second = load_catalog_item(second_item)
    pointer = activate_catalog(
        container, first, activated_by="pipeline", expected_etag=None,
    )
    with pytest.raises(CatalogConflictError):
        activate_catalog(
            container, second, activated_by="pipeline", expected_etag="stale",
        )
    updated = activate_catalog(
        container, second, activated_by="pipeline", expected_etag=pointer["_etag"],
    )
    activate_catalog(
        container, first, activated_by="pipeline", expected_etag=updated["_etag"],
    )
    assert CosmosCatalogLoader(container, "dev", first.version).load().version == first.version
    assert CosmosCatalogLoader(container, "dev", second.version).load().version == second.version


def test_loader_fails_closed_for_missing_or_inconsistent_items() -> None:
    container = FakeContainer()
    missing_version = "sha256:" + "a" * 64
    with pytest.raises(CatalogError, match="pinned retrieval catalog item is missing"):
        CosmosCatalogLoader(container, "dev", missing_version).load()

    item = _item()
    publish_catalog(container, item)
    catalog = load_catalog_item(item)
    del container.items[("dev", item["id"])]
    with pytest.raises(CatalogError, match="missing"):
        CosmosCatalogLoader(container, "dev", catalog.version).load()


@pytest.mark.parametrize(
    "version",
    ["", "sha256:ABC", "a" * 64, "sha256:" + "A" * 64, "sha512:" + "a" * 64],
)
def test_loader_rejects_invalid_external_digest(version: str) -> None:
    with pytest.raises(CatalogError, match="sha256"):
        CosmosCatalogLoader(FakeContainer(), "instance-a", version)
