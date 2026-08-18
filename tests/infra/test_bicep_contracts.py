from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
AZURE_CLI = shutil.which("az")


@lru_cache(maxsize=None)
def _compile_bicep(relative_path: str) -> dict[str, Any]:
    if AZURE_CLI is None:
        pytest.skip("Azure CLI is required to compile Bicep contract tests")

    result = subprocess.run(
        [AZURE_CLI, "bicep", "build", "--file", relative_path, "--stdout"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _resources(template: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    return [
        resource
        for resource in template["resources"]
        if resource["type"] == resource_type
    ]


def test_cosmos_container_topology_and_partition_keys() -> None:
    template = _compile_bicep("infra/modules/cosmos.bicep")
    containers = _resources(
        template,
        "Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers",
    )
    containers_by_id = {
        container["properties"]["resource"]["id"]: container
        for container in containers
    }

    assert set(containers_by_id) == {
        "ingestion-runs",
        "source-documents",
        "search-chunks",
        "service-audit",
    }
    assert containers_by_id["ingestion-runs"]["properties"]["resource"][
        "partitionKey"
    ]["paths"] == ["/sourceId"]
    assert containers_by_id["source-documents"]["properties"]["resource"][
        "partitionKey"
    ]["paths"] == ["/sourceRunId"]
    assert containers_by_id["search-chunks"]["properties"]["resource"][
        "partitionKey"
    ]["paths"] == ["/documentKey"]
    assert containers_by_id["service-audit"]["properties"]["resource"][
        "partitionKey"
    ]["paths"] == ["/id"]


def test_cosmos_search_policy_and_throughput_ownership() -> None:
    template = _compile_bicep("infra/modules/cosmos.bicep")
    containers = _resources(
        template,
        "Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers",
    )
    containers_by_id = {
        container["properties"]["resource"]["id"]: container
        for container in containers
    }
    search_chunks = containers_by_id["search-chunks"]
    search_resource = search_chunks["properties"]["resource"]

    assert search_resource["vectorEmbeddingPolicy"] == {
        "vectorEmbeddings": [
            {
                "path": "/embedding",
                "dataType": "float32",
                "dimensions": 3072,
                "distanceFunction": "cosine",
            }
        ]
    }
    assert search_resource["fullTextPolicy"] == {
        "defaultLanguage": "en-US",
        "fullTextPaths": [
            {"path": "/content", "language": "en-US"},
            {"path": "/searchableText", "language": "en-US"},
        ],
    }
    assert search_resource["indexingPolicy"]["vectorIndexes"] == [
        {"path": "/embedding", "type": "diskANN"}
    ]
    assert search_resource["indexingPolicy"]["fullTextIndexes"] == [
        {"path": "/content"},
        {"path": "/searchableText"},
    ]
    assert containers_by_id["source-documents"]["properties"]["resource"][
        "indexingPolicy"
    ]["compositeIndexes"] == [
        [
            {"path": "/status", "order": "ascending"},
            {"path": "/discoveryOrdinal", "order": "ascending"},
        ]
    ]

    database = _resources(
        template, "Microsoft.DocumentDB/databaseAccounts/sqlDatabases"
    )[0]
    assert "metadataAutoscaleMaxThroughput" in database["properties"]
    assert "searchChunksAutoscaleMaxThroughput" not in database["properties"]
    assert "searchChunksAutoscaleMaxThroughput" in search_chunks["properties"][
        "options"
    ]
    assert containers_by_id["ingestion-runs"]["properties"]["options"] == {}
    assert containers_by_id["source-documents"]["properties"]["options"] == {}


def test_durable_scheduler_task_hub_and_rbac_contract() -> None:
    template = _compile_bicep("infra/modules/durable-task.bicep")
    scheduler = _resources(template, "Microsoft.DurableTask/schedulers")[0]
    task_hub = _resources(
        template, "Microsoft.DurableTask/schedulers/taskHubs"
    )[0]
    role = _resources(template, "Microsoft.Authorization/roleAssignments")[0]

    assert scheduler["apiVersion"] == "2025-11-01"
    assert scheduler["properties"]["sku"] == {"name": "Consumption"}
    assert task_hub["apiVersion"] == "2025-11-01"
    assert task_hub["properties"] == {}
    assert template["variables"]["durableTaskDataContributorRoleId"] == (
        "0ad04412-c4d5-4796-b79c-f76d14c8d402"
    )
    assert "Microsoft.DurableTask/schedulers" in role["scope"]
    assert "taskHubs" in role["scope"]
    assert role["properties"]["principalId"] == (
        "[parameters('functionAppPrincipalId')]"
    )
    assert role["properties"]["principalType"] == "ServicePrincipal"


def test_function_settings_match_full_sync_contract() -> None:
    template = _compile_bicep("infra/modules/functions.bicep")
    function_app = _resources(template, "Microsoft.Web/sites")[0]
    settings = {
        setting["name"]: setting["value"]
        for setting in function_app["properties"]["siteConfig"]["appSettings"]
    }

    assert settings["COSMOS_INGESTION_RUNS_CONTAINER_NAME"] == (
        "[parameters('cosmosContainerNames').ingestionRuns]"
    )
    assert settings["COSMOS_SOURCE_DOCUMENTS_CONTAINER_NAME"] == (
        "[parameters('cosmosContainerNames').sourceDocuments]"
    )
    assert settings["COSMOS_SEARCH_CHUNKS_CONTAINER_NAME"] == (
        "[parameters('cosmosContainerNames').searchChunks]"
    )
    assert settings["TASKHUB_NAME"] == "[parameters('durableTaskHubName')]"
    assert settings["DURABLE_TASK_SCHEDULER_CONNECTION_STRING"] == (
        "[format('Endpoint={0};Authentication=ManagedIdentity;ClientID={1}', "
        "parameters('durableTaskSchedulerEndpoint'), "
        "parameters('managedIdentityClientId'))]"
    )

    obsolete_settings = {
        "COSMOS_CHUNKS_CONTAINER_NAME",
        "COSMOS_MANIFESTS_CONTAINER_NAME",
        "COSMOS_SOURCE_STATE_CONTAINER_NAME",
        "COSMOS_FAILURES_CONTAINER_NAME",
        "SHAREPOINT_WEBHOOK_CLIENT_STATE",
        "RECONCILIATION_SCHEDULE",
    }
    assert obsolete_settings.isdisjoint(settings)
