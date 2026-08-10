"""Azure Functions Durable App — SharePoint RAG Ingestion Pipeline.

Endpoints:
  POST /api/ingestion/full-sync    Start durable full-sync orchestrator (returns 202)
  GET  /api/ingestion/status       Query orchestration instance status
  GET  /api/ingestion/inspect      Read Cosmos data for debugging
  POST /api/query                  RAG query endpoint
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import azure.functions as func
import azure.durable_functions as df

app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)
logger = logging.getLogger("rag-ingestion")

WAVE_SIZE = int(os.getenv("WAVE_SIZE", "4"))


# =============================================================================
# HTTP Endpoints
# =============================================================================

@app.route(route="ingestion/full-sync", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@app.durable_client_input(client_name="client")
async def start_full_sync(req: func.HttpRequest, client) -> func.HttpResponse:
    """Start the durable full-sync orchestrator with singleton instance ID."""
    from ingestion.models import create_orchestration_instance_id

    source_id = os.getenv("INGESTION_SOURCE_ID", "").strip()
    if not source_id:
        return _json_response({"error": "missing_ingestion_source_id"}, 503)

    instance_id = create_orchestration_instance_id(source_id)
    existing = await client.get_status(instance_id)
    if existing and existing.runtime_status in (
        df.OrchestrationRuntimeStatus.Running,
        df.OrchestrationRuntimeStatus.Pending,
    ):
        return _json_response({"status": "already_running", "instanceId": instance_id}, 409)

    await client.start_new("full_sync_orchestrator", instance_id=instance_id)
    return client.create_check_status_response(req, instance_id)


@app.route(route="ingestion/status", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
@app.durable_client_input(client_name="client")
async def get_status(req: func.HttpRequest, client) -> func.HttpResponse:
    """Query orchestration status."""
    from ingestion.models import create_orchestration_instance_id
    source_id = os.getenv("INGESTION_SOURCE_ID", "").strip()
    instance_id = req.params.get("instanceId") or create_orchestration_instance_id(source_id)
    status = await client.get_status(instance_id)
    if not status:
        return _json_response({"error": "not_found"}, 404)
    return _json_response({
        "instanceId": instance_id,
        "runtimeStatus": str(status.runtime_status),
        "output": status.output,
        "createdTime": str(status.created_time) if status.created_time else None,
        "lastUpdatedTime": str(status.last_updated_time) if status.last_updated_time else None,
    }, 200)


@app.route(route="ingestion/terminate", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@app.durable_client_input(client_name="client")
async def terminate_ingestion(req: func.HttpRequest, client) -> func.HttpResponse:
    """Terminate a stuck orchestration and purge its history."""
    from ingestion.models import create_orchestration_instance_id
    source_id = os.getenv("INGESTION_SOURCE_ID", "").strip()
    instance_id = req.params.get("instanceId") or create_orchestration_instance_id(source_id)
    await client.terminate(instance_id, "operator_terminated")
    await client.purge_instance_history(instance_id)
    return _json_response({"terminated": instance_id}, 200)


@app.route(route="ingestion/inspect", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def inspect_data(req: func.HttpRequest) -> func.HttpResponse:
    """Read Cosmos containers via MI for debugging."""
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    container_name = (req.params.get("container") or "").strip()
    allowed = {"ingestion-runs", "source-documents", "search-chunks"}
    if container_name not in allowed:
        return _json_response({"error": "invalid_container", "allowed": sorted(allowed)}, 400)

    limit = min(int(req.params.get("limit", "10")), 50)
    run_id = (req.params.get("runId") or "").strip()

    try:
        credential = DefaultAzureCredential(managed_identity_client_id=os.getenv("AZURE_CLIENT_ID", ""))
        cosmos = CosmosClient(os.getenv("COSMOS_ENDPOINT", ""), credential=credential)
        db = cosmos.get_database_client(os.getenv("COSMOS_DATABASE_NAME", ""))
        container = db.get_container_client(container_name)

        if run_id:
            source_id = os.getenv("INGESTION_SOURCE_ID", "").strip()
            rows = list(container.query_items(
                query="SELECT TOP @limit * FROM c",
                parameters=[{"name": "@limit", "value": limit}],
                partition_key=f"{source_id}:{run_id}",
            ))
        else:
            rows = list(container.query_items(
                query="SELECT TOP @limit * FROM c",
                parameters=[{"name": "@limit", "value": limit}],
                enable_cross_partition_query=True,
            ))
    except Exception as error:
        logger.exception("inspect_data failed")
        return _json_response({"error": "cosmos_query_failed", "detail": str(error)[:200]}, 503)

    sanitized = [{k: v for k, v in row.items() if not k.startswith("_")} for row in rows]
    return _json_response({"container": container_name, "count": len(sanitized), "rows": sanitized}, 200)


@app.route(route="query", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def query_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    """RAG query with ACL-trimmed retrieval."""
    try:
        from retrieval.auth import AuthorizationError
        body = req.get_json()
        question = body.get("question", "")
        if not question:
            return _json_response({"error": "question_required"}, 400)
        # TODO: Wire retrieval service when ready
        return _json_response({"error": "retrieval_not_implemented"}, 501)
    except Exception:
        logger.exception("Query failed")
        return _json_response({"error": "service_unavailable"}, 503)


# =============================================================================
# Durable Orchestrator
# =============================================================================

@app.orchestration_trigger(context_name="context")
def full_sync_orchestrator(context: df.DurableOrchestrationContext):
    """Orchestrate: activate → discover → fan-out process → finalize."""
    retry = df.RetryOptions(first_retry_interval_in_milliseconds=5000, max_number_of_attempts=5)

    activated = yield context.call_activity_with_retry("activate_run_activity", retry, None)
    if activated.get("error"):
        return {"status": "failed", "phase": "activate", "error": activated["error"]}

    run_id = activated["runId"]
    run_etag = activated["runEtag"]

    discovered = yield context.call_activity_with_retry("discover_all_activity", retry, {"runId": run_id})
    if discovered.get("error"):
        return {"status": "failed", "phase": "discover", "runId": run_id, "error": discovered["error"]}

    documents = discovered["documents"]
    items_scanned = discovered["itemsScanned"]

    total_succeeded = 0
    total_failed = 0
    for wave_start in range(0, len(documents), WAVE_SIZE):
        wave = documents[wave_start:wave_start + WAVE_SIZE]
        tasks = [
            context.call_activity_with_retry("process_document_activity", retry, {"runId": run_id, "document": doc})
            for doc in wave
        ]
        results = yield context.task_all(tasks)
        for result in results:
            if result.get("status") == "succeeded":
                total_succeeded += 1
            else:
                total_failed += 1

    finalized = yield context.call_activity("finalize_run_activity", {"runId": run_id, "runEtag": run_etag, "itemsScanned": items_scanned})

    return {
        "status": "completed",
        "runId": run_id,
        "discovered": len(documents),
        "succeeded": total_succeeded,
        "failed": total_failed,
        "runStatus": finalized.get("runStatus", "unknown"),
    }


# =============================================================================
# Activities
# =============================================================================

@app.activity_trigger(input_name="payload")
def activate_run_activity(payload: Any) -> dict:
    from config import load_config
    from ingestion.services import activate
    try:
        config = load_config()
        repository = _build_repository(config)
        activated = activate(config, repository)
        return {"runId": activated.run.record.run_id, "runEtag": activated.run.etag}
    except Exception as error:
        logger.exception("activate_run_activity failed")
        return {"error": str(error)}


@app.activity_trigger(input_name="payload")
def discover_all_activity(payload: dict) -> dict:
    from config import load_config
    from ingestion.services import discover_all
    try:
        config = load_config()
        repository = _build_repository(config)
        graph_client = _build_graph_client(config)
        with graph_client:
            documents, items_scanned = discover_all(config, payload["runId"], repository, graph_client)
        return {
            "documents": [{"documentId": d.document_id, "sourceRunId": d.source_run_id} for d in documents],
            "itemsScanned": items_scanned,
        }
    except Exception as error:
        logger.exception("discover_all_activity failed")
        return {"error": str(error)}


@app.activity_trigger(input_name="payload")
def process_document_activity(payload: dict) -> dict:
    from config import load_config
    from ingestion.services import process_document
    document_ref = payload["document"]
    try:
        config = load_config()
        repository = _build_repository(config)
        graph_client = _build_graph_client(config)
        di_client = _build_di_client(config) if config.extraction_enabled else None
        language_client = _build_language_client(config) if config.enrichment_enabled else None
        openai_client = _build_openai_client(config)

        stored = repository.get_document(document_ref["sourceRunId"], document_ref["documentId"])
        if stored is None:
            return {"documentId": document_ref["documentId"], "status": "skipped"}

        with graph_client:
            outcome = process_document(
                config, stored.record, stored.etag, repository,
                graph_client, di_client, language_client, openai_client,
            )
        return {
            "documentId": outcome.document_id,
            "status": outcome.status.value,
            "chunksWritten": outcome.chunks_written,
            "error": outcome.error.code if outcome.error else None,
        }
    except Exception as error:
        logger.exception("process_document_activity failed")
        return {"documentId": document_ref.get("documentId", ""), "status": "failed", "error": str(error)}


@app.activity_trigger(input_name="payload")
def finalize_run_activity(payload: dict) -> dict:
    from config import load_config
    from ingestion.services import finalize
    try:
        config = load_config()
        repository = _build_repository(config)
        finalized = finalize(config, payload["runEtag"], repository, payload.get("itemsScanned", 0))
        return {"runStatus": finalized.record.status.value}
    except Exception as error:
        logger.exception("finalize_run_activity failed")
        return {"runStatus": "finalization_failed", "error": str(error)}


# =============================================================================
# Client Factories
# =============================================================================

def _build_repository(config):
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential
    from ingestion.repository import IngestionRepository
    credential = DefaultAzureCredential(managed_identity_client_id=config.managed_identity_client_id)
    cosmos = CosmosClient(config.cosmos_endpoint, credential=credential)
    db = cosmos.get_database_client(config.cosmos_database)
    return IngestionRepository(
        db.get_container_client(config.cosmos_ingestion_runs_container),
        db.get_container_client(config.cosmos_source_documents_container),
        db.get_container_client(config.cosmos_search_chunks_container),
    )


def _build_graph_client(config):
    from azure.identity import CertificateCredential, DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient
    from ingestion.graph import GraphCredentialAuth
    import httpx
    import base64

    credential = DefaultAzureCredential(managed_identity_client_id=config.managed_identity_client_id)
    secret_client = SecretClient(config.key_vault_uri, credential)
    try:
        cert_secret = secret_client.get_secret(config.certificate_secret_name)
        cert_data = base64.b64decode(cert_secret.value, validate=True)
    except Exception as error:
        raise RuntimeError(f"Failed to load Graph certificate from Key Vault: {type(error).__name__}") from error
    graph_credential = CertificateCredential(
        tenant_id=config.tenant_id,
        client_id=config.app_client_id,
        certificate_data=cert_data,
    )
    transport = httpx.HTTPTransport(retries=3)
    return httpx.Client(auth=GraphCredentialAuth(graph_credential, "https://graph.microsoft.com/.default"), transport=transport, timeout=120)


def _build_di_client(config):
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.identity import DefaultAzureCredential
    credential = DefaultAzureCredential(managed_identity_client_id=config.managed_identity_client_id)
    return DocumentIntelligenceClient(endpoint=config.document_intelligence_endpoint, credential=credential)


def _build_language_client(config):
    from azure.ai.textanalytics import TextAnalyticsClient
    from azure.identity import DefaultAzureCredential
    credential = DefaultAzureCredential(managed_identity_client_id=config.managed_identity_client_id)
    return TextAnalyticsClient(endpoint=config.language_endpoint, credential=credential, api_version="2023-04-01")


def _build_openai_client(config):
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI
    credential = DefaultAzureCredential(managed_identity_client_id=config.managed_identity_client_id)
    return AzureOpenAI(
        azure_endpoint=config.openai_endpoint,
        azure_ad_token_provider=get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default"),
        api_version="2024-10-21",
    )


def _json_response(payload: dict, status_code: int) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(payload, default=str), status_code=status_code, mimetype="application/json")
