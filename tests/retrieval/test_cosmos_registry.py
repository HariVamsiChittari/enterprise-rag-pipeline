"""Tests for the Goal 2 CosmosRegistry: config parsing and default single-instance behavior."""

from __future__ import annotations

import pytest

from retrieval.cosmos_registry import CosmosRegistry, load_cosmos_instance_configs


def test_defaults_to_single_instance_from_legacy_values_when_registry_json_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COSMOS_REGISTRY_JSON", raising=False)

    configs = load_cosmos_instance_configs(
        default_source_id="source-a",
        default_endpoint="https://cosmos-a.example",
        default_database="db",
        default_chunks_container="search-chunks",
        default_manifests_container="source-documents",
    )

    assert len(configs) == 1
    assert configs[0].source_id == "source-a"
    assert configs[0].endpoint == "https://cosmos-a.example"


def test_parses_multiple_instances_from_registry_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "COSMOS_REGISTRY_JSON",
        '[{"sourceId": "source-a", "endpoint": "https://a.example", "database": "db-a"},'
        ' {"sourceId": "source-b", "endpoint": "https://b.example", "database": "db-b", "chunksContainer": "chunks-b"}]',
    )

    configs = load_cosmos_instance_configs(
        default_source_id="ignored",
        default_endpoint="ignored",
        default_database="ignored",
        default_chunks_container="search-chunks",
        default_manifests_container="source-documents",
    )

    assert [c.source_id for c in configs] == ["source-a", "source-b"]
    assert configs[0].chunks_container == "search-chunks"  # falls back to default
    assert configs[1].chunks_container == "chunks-b"


def test_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSMOS_REGISTRY_JSON", "not json")

    with pytest.raises(EnvironmentError, match="not valid JSON"):
        load_cosmos_instance_configs(
            default_source_id="s", default_endpoint="e", default_database="d",
            default_chunks_container="c", default_manifests_container="m",
        )


def test_rejects_empty_array(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSMOS_REGISTRY_JSON", "[]")

    with pytest.raises(EnvironmentError, match="non-empty"):
        load_cosmos_instance_configs(
            default_source_id="s", default_endpoint="e", default_database="d",
            default_chunks_container="c", default_manifests_container="m",
        )


def test_rejects_entry_missing_required_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSMOS_REGISTRY_JSON", '[{"sourceId": "source-a"}]')

    with pytest.raises(EnvironmentError, match="endpoint"):
        load_cosmos_instance_configs(
            default_source_id="s", default_endpoint="e", default_database="d",
            default_chunks_container="c", default_manifests_container="m",
        )


def test_registry_requires_at_least_one_entry() -> None:
    with pytest.raises(ValueError, match="at least one"):
        CosmosRegistry({})


def test_registry_items_and_len() -> None:
    registry = CosmosRegistry({"source-a": object(), "source-b": object()})

    assert len(registry) == 2
    assert {source_id for source_id, _ in registry.items()} == {"source-a", "source-b"}
