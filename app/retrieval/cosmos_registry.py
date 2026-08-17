"""Goal 2: a registry of Cosmos DB instances (one per source) for retrieval fan-out.

Defaults to exactly one instance built from RetrievalConfig's existing single-instance
fields, so behavior is unchanged for the current single-source deployment. A second
instance is added purely by setting COSMOS_REGISTRY_JSON -- no code change required.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from azure.cosmos import CosmosClient
from azure.identity import ManagedIdentityCredential

from retrieval.cosmos import SecureCosmosRetriever


@dataclass(frozen=True)
class CosmosInstanceConfig:
    source_id: str
    endpoint: str
    database: str
    chunks_container: str
    manifests_container: str


class CosmosRegistry:
    """source_id -> SecureCosmosRetriever, for RagService's per-instance fan-out."""

    def __init__(self, retrievers: dict[str, SecureCosmosRetriever]) -> None:
        if not retrievers:
            raise ValueError("registry must contain at least one Cosmos instance")
        self._retrievers = dict(retrievers)

    def items(self) -> list[tuple[str, SecureCosmosRetriever]]:
        return list(self._retrievers.items())

    def __len__(self) -> int:
        return len(self._retrievers)


def load_cosmos_instance_configs(
    *,
    default_source_id: str,
    default_endpoint: str,
    default_database: str,
    default_chunks_container: str,
    default_manifests_container: str,
) -> tuple[CosmosInstanceConfig, ...]:
    """Parse COSMOS_REGISTRY_JSON if set (a JSON array of instance entries), else fall
    back to a single instance built from the caller's default (legacy) values."""
    raw = os.getenv("COSMOS_REGISTRY_JSON", "").strip()
    if not raw:
        return (
            CosmosInstanceConfig(
                source_id=default_source_id,
                endpoint=default_endpoint,
                database=default_database,
                chunks_container=default_chunks_container,
                manifests_container=default_manifests_container,
            ),
        )

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as error:
        raise EnvironmentError("COSMOS_REGISTRY_JSON is not valid JSON") from error
    if not isinstance(entries, list) or not entries:
        raise EnvironmentError("COSMOS_REGISTRY_JSON must be a non-empty JSON array")

    configs: list[CosmosInstanceConfig] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise EnvironmentError("COSMOS_REGISTRY_JSON entries must be objects")
        configs.append(
            CosmosInstanceConfig(
                source_id=_require_str(entry, "sourceId"),
                endpoint=_require_str(entry, "endpoint"),
                database=_require_str(entry, "database"),
                chunks_container=entry.get("chunksContainer", default_chunks_container),
                manifests_container=entry.get("manifestsContainer", default_manifests_container),
            )
        )
    return tuple(configs)


def build_cosmos_registry(
    instance_configs: tuple[CosmosInstanceConfig, ...],
    credential: ManagedIdentityCredential,
    *,
    acl_enabled: bool = True,
) -> CosmosRegistry:
    retrievers: dict[str, SecureCosmosRetriever] = {}
    for instance in instance_configs:
        cosmos = CosmosClient(url=instance.endpoint, credential=credential)
        db = cosmos.get_database_client(instance.database)
        retrievers[instance.source_id] = SecureCosmosRetriever(
            db.get_container_client(instance.chunks_container),
            db.get_container_client(instance.manifests_container),
            acl_enabled=acl_enabled,
        )
    return CosmosRegistry(retrievers)


def _require_str(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EnvironmentError(f"COSMOS_REGISTRY_JSON entry is missing required field '{key}'")
    return value
