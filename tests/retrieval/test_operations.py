from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from azure.cosmos.exceptions import CosmosResourceExistsError, CosmosResourceNotFoundError

import retrieval.operations as operations
from retrieval.catalog import build_catalog_item


class FakeContainer:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def create_item(self, *, body: dict[str, Any]) -> dict[str, Any]:
        key = (body["deploymentInstanceId"], body["id"])
        if key in self.items:
            raise CosmosResourceExistsError(status_code=409, message="exists")
        stored = {**body, "_etag": f"etag-{len(self.items) + 1}"}
        self.items[key] = stored
        return dict(stored)

    def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        try:
            return dict(self.items[(partition_key, item)])
        except KeyError:
            raise CosmosResourceNotFoundError(
                status_code=404, message="missing"
            ) from None


class FakeCosmos:
    def __init__(self, container: FakeContainer) -> None:
        self.container = container
        self.closed = False

    def get_database_client(self, name: str) -> "FakeCosmos":
        return self

    def get_container_client(self, name: str) -> FakeContainer:
        return self.container

    def close(self) -> None:
        self.closed = True


class FakeCredential:
    def __init__(self, *, client_id: str) -> None:
        self.client_id = client_id
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _source() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "config": {
            "retrieval": {
                "overFetchFactor": 3,
                "hybridWeights": {"vector": 2, "text": 1},
                "fullTextScoreScope": "Global",
            },
            "defaultProfile": "default",
            "synonymsEnabled": False,
            "profiles": [{
                "name": "default",
                "textWeights": {"content": 1},
                "functionAggregation": "sum",
                "functions": [],
            }],
            "synonymMaps": [],
        },
    }


def _configure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, container: FakeContainer,
) -> tuple[FakeCosmos, list[FakeCredential]]:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(_source()), encoding="utf-8")
    digest = build_catalog_item(_source(), "instance-a")["version"]
    values = {
        "COSMOS_ENDPOINT": "https://cosmos.example",
        "COSMOS_DATABASE": "rag-db",
        "RETRIEVAL_CONFIG_CONTAINER": "retrieval-config",
        "DEPLOYMENT_INSTANCE_ID": "instance-a",
        "EXPECTED_CATALOG_DIGEST": digest,
        "MANAGED_IDENTITY_CLIENT_ID": "identity-client",
        "CATALOG_PATH": str(path),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    cosmos = FakeCosmos(container)
    credentials: list[FakeCredential] = []
    monkeypatch.setattr(operations, "CosmosClient", lambda *_a, **_k: cosmos)

    def _credential(**kwargs: Any) -> FakeCredential:
        credential = FakeCredential(**kwargs)
        credentials.append(credential)
        return credential

    monkeypatch.setattr(operations, "ManagedIdentityCredential", _credential)
    return cosmos, credentials


def test_private_runner_publishes_and_replays_same_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    container = FakeContainer()
    cosmos, credentials = _configure(monkeypatch, tmp_path, container)

    first = operations.publish_bootstrap_catalog()
    second = operations.publish_bootstrap_catalog()

    assert first == second
    assert first["catalogDigest"].startswith("sha256:")
    assert first["activationEtag"]
    assert cosmos.closed is True
    assert all(credential.closed for credential in credentials)


def test_private_runner_rejects_reviewed_digest_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    container = FakeContainer()
    _configure(monkeypatch, tmp_path, container)
    monkeypatch.setenv("EXPECTED_CATALOG_DIGEST", "sha256:" + "a" * 64)

    with pytest.raises(
        operations.OperationsError, match="reviewed artifact"
    ):
        operations.publish_bootstrap_catalog()

    assert container.items == {}


def test_private_runner_refuses_different_active_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    container = FakeContainer()
    _configure(monkeypatch, tmp_path, container)
    container.items[("instance-a", "active")] = {
        "id": "active",
        "deploymentInstanceId": "instance-a",
        "catalogId": "catalog:" + "b" * 64,
        "version": "sha256:" + "b" * 64,
        "_etag": "etag-existing",
    }

    with pytest.raises(operations.OperationsError, match="different active catalog"):
        operations.publish_bootstrap_catalog()
