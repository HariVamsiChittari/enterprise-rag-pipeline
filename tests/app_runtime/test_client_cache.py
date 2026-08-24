"""Tests for function_app._client_cache: _build_* factories must construct their
underlying SDK client once per process and reuse it across calls (Azure Functions
Python guidance: create clients at module level, don't rebuild per invocation).

Monkeypatches the SDK classes each factory imports locally (azure.cosmos.CosmosClient,
azure.identity.DefaultAzureCredential, openai.AzureOpenAI), matching the pattern used in
test_retire_prior_version.py: a local `from x import Y` re-resolves the module attribute
on every call, so patching the module attribute is sufficient. Production code is never
modified by these tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import azure.cosmos
import azure.identity
import openai
import pytest

import function_app


@pytest.fixture(autouse=True)
def clear_client_cache():
    """_client_cache is module-level shared state; isolate each test from it."""
    function_app._client_cache.clear()
    yield
    function_app._client_cache.clear()


class FakeContainer:
    pass


class FakeDatabase:
    def get_container_client(self, name: str) -> FakeContainer:
        return FakeContainer()


class FakeCosmosClient:
    instances: list["FakeCosmosClient"] = []

    def __init__(self, endpoint: str, credential: Any) -> None:
        FakeCosmosClient.instances.append(self)

    def get_database_client(self, name: str) -> FakeDatabase:
        return FakeDatabase()


class FakeAzureOpenAI:
    instances: list["FakeAzureOpenAI"] = []

    def __init__(self, **kwargs: Any) -> None:
        FakeAzureOpenAI.instances.append(self)


def _fake_config(**overrides: Any) -> SimpleNamespace:
    base = dict(
        managed_identity_client_id="mi-client-id",
        cosmos_endpoint="https://cosmos.example",
        cosmos_database="db",
        cosmos_ingestion_runs_container="ingestion-runs",
        cosmos_source_documents_container="source-documents",
        cosmos_search_chunks_container="search-chunks",
        openai_endpoint="https://openai.example",
        sharepoint_site_url="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_repository_constructs_cosmos_client_once(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeCosmosClient.instances.clear()
    monkeypatch.setattr(azure.cosmos, "CosmosClient", FakeCosmosClient)
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda **kwargs: object())
    config = _fake_config()

    first = function_app._build_repository(config)
    second = function_app._build_repository(config)

    assert first is second
    assert len(FakeCosmosClient.instances) == 1


def test_build_openai_client_constructs_sdk_client_once(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAzureOpenAI.instances.clear()
    monkeypatch.setattr(openai, "AzureOpenAI", FakeAzureOpenAI)
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda **kwargs: object())
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", lambda credential, scope: lambda: "token")
    config = _fake_config()

    first = function_app._build_openai_client(config)
    second = function_app._build_openai_client(config)

    assert first is second
    assert len(FakeAzureOpenAI.instances) == 1


def test_build_sharepoint_client_caches_none_without_rebuilding(monkeypatch: pytest.MonkeyPatch) -> None:
    """When sharepoint_site_url is unset, the factory must cache None, not retry Key Vault on every call."""

    def _fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("SecretClient should not be constructed when sharepoint_site_url is empty")

    monkeypatch.setattr("azure.keyvault.secrets.SecretClient", _fail_if_called)
    config = _fake_config(sharepoint_site_url="")

    first = function_app._build_sharepoint_client(config)
    second = function_app._build_sharepoint_client(config)

    assert first is None
    assert second is None
    assert "sharepoint_client" in function_app._client_cache
