"""Azure Functions Durable App — SharePoint RAG Ingestion Pipeline.

Endpoints:
  POST /api/ingestion/full-sync    Start durable full-sync orchestrator (returns 202)
  GET  /api/ingestion/status       Query orchestration instance status
  GET  /api/ingestion/inspect      Read Cosmos data for debugging
  POST /api/query                  RAG query endpoint
  POST /api/webhook/sharepoint     Microsoft Graph change notification receiver
  POST /api/webhook/lifecycle      Microsoft Graph lifecycle notification receiver

Timers:
  reconciliation_timer     Daily safety-net delta query (replaces 15-min polling)
  acl_resync_timer         Re-verify ACLs on already-ingested documents
  subscription_renew_timer Renew Microsoft Graph webhook subscription daily
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
WAVE_TIMEOUT_MINUTES = int(os.getenv("WAVE_TIMEOUT_MINUTES", "20"))
RECONCILIATION_SCHEDULE = os.getenv("DELTA_SYNC_SCHEDULE", "0 0 4 * * *")  # daily 04:00 UTC
ACL_RESYNC_SCHEDULE = os.getenv("ACL_RESYNC_SCHEDULE", "0 0 3 * * 0")  # weekly Sunday 03:00 UTC (safety net)
ACL_RESYNC_PAGE_SIZE = int(os.getenv("ACL_RESYNC_PAGE_SIZE", "50"))
SUBSCRIPTION_RENEW_SCHEDULE = os.getenv("SUBSCRIPTION_RENEW_SCHEDULE", "0 0 2 * * *")
WEBHOOK_CLIENT_STATE = os.getenv("WEBHOOK_CLIENT_STATE", "")



@app.route(route="ingestion/full-sync", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@app.durable_client_input(client_name="client")
async def start_full_sync(req: func.HttpRequest, client) -> func.HttpResponse:
    """Start the durable full-sync orchestrator with a fresh, never-reused instance ID.

    Durable instance-ID reuse is documented by Microsoft as best-effort and racy at the
    storage layer (Azure/azure-functions-durable-python#410), so "is a full-sync already
    running" is tracked via the app's own Cosmos source-control/run records instead of a
    fixed, guessable Durable instance ID.
    """
    from config import load_config

    source_id = os.getenv("INGESTION_SOURCE_ID", "").strip()
    if not source_id:
        return _json_response({"error": "missing_ingestion_source_id"}, 503)

    config = load_config()
    repository = _build_repository(config)
    if _full_sync_is_running(repository, source_id):
        instance_id = _current_full_sync_instance_id(repository, source_id)
        return _json_response({"status": "already_running", "instanceId": instance_id}, 409)

    instance_id = await client.start_new("full_sync_orchestrator")
    return client.create_check_status_response(req, instance_id)


@app.route(route="ingestion/status", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
@app.durable_client_input(client_name="client")
async def get_status(req: func.HttpRequest, client) -> func.HttpResponse:
    """Query orchestration status. ?showHistory=true adds the replay history (diagnostic)."""
    from config import load_config
    source_id = os.getenv("INGESTION_SOURCE_ID", "").strip()
    instance_id = req.params.get("instanceId")
    if not instance_id:
        config = load_config()
        repository = _build_repository(config)
        instance_id = _current_full_sync_instance_id(repository, source_id)
        if not instance_id:
            return _json_response({"error": "not_found"}, 404)
    show_history = (req.params.get("showHistory") or "").lower() == "true"
    status = await client.get_status(instance_id, show_history=show_history, show_history_output=show_history)
    if not status:
        return _json_response({"error": "not_found"}, 404)
    payload = {
        "instanceId": instance_id,
        "runtimeStatus": str(status.runtime_status),
        "output": status.output,
        "createdTime": str(status.created_time) if status.created_time else None,
        "lastUpdatedTime": str(status.last_updated_time) if status.last_updated_time else None,
    }
    if show_history:
        payload["history"] = status.history
    return _json_response(payload, 200)


@app.route(route="ingestion/terminate", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@app.durable_client_input(client_name="client")
async def terminate_ingestion(req: func.HttpRequest, client) -> func.HttpResponse:
    """Terminate orchestration, fail non-terminal docs, and finalize run as TERMINATED."""
    from config import load_config
    from ingestion.services import terminate_run
    source_id = os.getenv("INGESTION_SOURCE_ID", "").strip()
    config = load_config()
    repository = _build_repository(config)
    instance_id = req.params.get("instanceId") or _current_full_sync_instance_id(repository, source_id)
    if instance_id:
        await client.terminate(instance_id, "operator_terminated")
        # purge requires the instance to already be in a terminal state.
        if await _wait_for_terminal_status(client, instance_id):
            await client.purge_instance_history(instance_id)
        else:
            logger.warning("terminate_ingestion: %s did not reach terminal status in time, skipping purge", instance_id)
    result = terminate_run(config, repository)
    result["orchestrationId"] = instance_id
    return _json_response(result, 200)


async def _wait_for_terminal_status(client, instance_id: str, timeout_seconds: int = 30) -> bool:
    """Poll until the instance reaches a status purge_instance_history requires, or timeout.

    terminate()/purge_instance_history() are client-function calls, not orchestrator code,
    so ordinary async/await and asyncio.sleep are safe here (Durable's determinism
    constraints only apply inside orchestrator functions).
    """
    import asyncio
    terminal = (
        df.OrchestrationRuntimeStatus.Completed,
        df.OrchestrationRuntimeStatus.Failed,
        df.OrchestrationRuntimeStatus.Terminated,
    )
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        status = await client.get_status(instance_id)
        if status is None or status.runtime_status in terminal:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(1)


@app.route(route="ingestion/retry-failed", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@app.durable_client_input(client_name="client")
async def retry_failed(req: func.HttpRequest, client) -> func.HttpResponse:
    """Retry only the failed documents from the current run."""
    import uuid
    from config import load_config
    from ingestion.services import get_retry_candidates
    source_id = os.getenv("INGESTION_SOURCE_ID", "").strip()
    config = load_config()
    repository = _build_repository(config)
    if _full_sync_is_running(repository, source_id):
        return _json_response({"error": "sync_in_progress"}, 409)
    candidates = get_retry_candidates(config, repository)
    if not candidates:
        return _json_response({"status": "nothing_to_retry", "failed": 0}, 200)
    reset_docs = []
    for doc in candidates:
        reset = repository.reset_failed_to_discovered(doc)
        if reset:
            reset_docs.append({"documentId": reset["id"], "sourceRunId": reset["sourceRunId"]})
    if not reset_docs:
        return _json_response({"status": "reset_failed", "failed": len(candidates)}, 500)
    retry_id = f"retry-failed-{uuid.uuid4().hex}"
    await client.start_new("retry_failed_orchestrator", instance_id=retry_id, client_input=reset_docs)
    return _json_response({"status": "retrying", "count": len(reset_docs), "orchestrationId": retry_id}, 202)


@app.route(route="ingestion/inspect", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def inspect_data(req: func.HttpRequest) -> func.HttpResponse:
    """Read Cosmos containers via MI for debugging."""
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    container_name = (req.params.get("container") or "").strip()
    allowed = {"ingestion-runs", "source-documents", "search-chunks", "service-audit"}
    if container_name not in allowed:
        return _json_response({"error": "invalid_container", "allowed": sorted(allowed)}, 400)

    limit = min(int(req.params.get("limit", "10")), 200)
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
    except Exception:
        logger.exception("inspect_data failed")
        return _json_response({"error": "cosmos_query_failed"}, 503)

    sanitized = [{k: v for k, v in row.items() if not k.startswith("_")} for row in rows]
    return _json_response({"container": container_name, "count": len(sanitized), "rows": sanitized}, 200)


_PARTITION_KEY_FIELD = {
    "ingestion-runs": "sourceId",
    "source-documents": "sourceRunId",
    "search-chunks": "documentKey",
    "service-audit": "id",
}


@app.route(route="ingestion/purge", methods=["DELETE"], auth_level=func.AuthLevel.ANONYMOUS)
def purge_data(req: func.HttpRequest) -> func.HttpResponse:
    """Delete items from a Cosmos container. Writes an audit record for every purge."""
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential
    import uuid

    try:
        body = req.get_json()
    except ValueError:
        return _json_response({"error": "invalid_json"}, 400)

    container_name = (body.get("container") or "").strip()
    purgeable = {"ingestion-runs", "source-documents", "search-chunks"}
    if container_name not in purgeable:
        return _json_response({"error": "invalid_container", "allowed": sorted(purgeable)}, 400)

    ids = body.get("ids")
    purge_all = body.get("purgeAll", False)
    confirm = body.get("confirm", "")

    if not ids and not purge_all:
        return _json_response({"error": "provide 'ids' (list) or 'purgeAll':true"}, 400)
    if purge_all and confirm != "yes":
        return _json_response({"error": "purgeAll requires 'confirm':'yes'"}, 400)
    if ids and (not isinstance(ids, list) or len(ids) > 100):
        return _json_response({"error": "ids must be a list with max 100 items"}, 400)

    operator = req.headers.get("X-MS-CLIENT-PRINCIPAL-NAME", "unknown")
    pk_field = _PARTITION_KEY_FIELD[container_name]

    try:
        credential = DefaultAzureCredential(managed_identity_client_id=os.getenv("AZURE_CLIENT_ID", ""))
        cosmos = CosmosClient(os.getenv("COSMOS_ENDPOINT", ""), credential=credential)
        db = cosmos.get_database_client(os.getenv("COSMOS_DATABASE_NAME", ""))
        container = db.get_container_client(container_name)
        audit_container = db.get_container_client("service-audit")

        deleted_ids = []
        failed = 0

        if ids:
            # Read each item to get its partition key, then delete
            for item_id in ids:
                try:
                    items = list(container.query_items(
                        query="SELECT c.id, c[@pk] as pk FROM c WHERE c.id = @id",
                        parameters=[{"name": "@id", "value": item_id}, {"name": "@pk", "value": pk_field}],
                        enable_cross_partition_query=True,
                    ))
                    if not items:
                        failed += 1
                        continue
                    pk_value = items[0].get("pk", items[0].get(pk_field, ""))
                    container.delete_item(item=item_id, partition_key=pk_value)
                    deleted_ids.append(item_id)
                except Exception:
                    failed += 1
        else:
            while True:
                batch = list(container.query_items(
                    query=f"SELECT TOP 100 c.id, c.{pk_field} FROM c",
                    enable_cross_partition_query=True,
                ))
                if not batch:
                    break
                for item in batch:
                    try:
                        container.delete_item(item=item["id"], partition_key=item[pk_field])
                        deleted_ids.append(item["id"])
                    except Exception:
                        failed += 1

        # write_audit_record never raises, so it can't mask a completed purge as a failure.
        from ingestion.telemetry import write_audit_record
        audit_id = str(uuid.uuid4())
        source_id = os.getenv("INGESTION_SOURCE_ID", "").strip()
        write_audit_record(audit_container, source_id, "purge", {
            "id": audit_id,
            "operation": "purge",
            "container": container_name,
            "operator": operator,
            "deletedIds": deleted_ids[:100],
            "deletedCount": len(deleted_ids),
            "failedCount": failed,
            "purgeAll": purge_all,
        })
        logger.warning("purge_executed container=%s deleted=%d failed=%d operator=%s", container_name, len(deleted_ids), failed, operator)

        return _json_response({"deleted": len(deleted_ids), "failed": failed, "auditId": audit_id}, 200)
    except Exception:
        logger.exception("purge_data failed")
        return _json_response({"error": "purge_failed"}, 503)


@app.route(route="query", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def query_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    """Proxy RAG queries to the internal ACA retrieval service."""
    import httpx
    try:
        body = req.get_json()
        question = body.get("question", "")
        if not question:
            return _json_response({"error": "question_required"}, 400)
        retrieval_url = os.getenv("RETRIEVAL_SERVICE_URL", "").strip()
        if not retrieval_url:
            return _json_response({"error": "RETRIEVAL_SERVICE_URL not configured"}, 501)
        if not retrieval_url.startswith("https://"):
            return _json_response({"error": "RETRIEVAL_SERVICE_URL must use HTTPS"}, 500)
        auth_header = req.headers.get("X-MS-CLIENT-PRINCIPAL", "")
        proxy_timeout = float(os.getenv("QUERY_PROXY_TIMEOUT_SECONDS", "30.0"))
        with httpx.Client(timeout=proxy_timeout) as client:
            resp = client.post(
                f"{retrieval_url}/api/query",
                json=body,
                headers={"X-MS-CLIENT-PRINCIPAL": auth_header},
            )
        return func.HttpResponse(resp.text, status_code=resp.status_code, mimetype="application/json")
    except Exception:
        logger.exception("Query proxy failed")
        return _json_response({"error": "service_unavailable"}, 503)



def _current_full_sync_instance_id(repository, source_id: str) -> str | None:
    """The Durable instance ID of the most recent full-sync run, per Cosmos, or None."""
    control = repository.get_source_control(source_id)
    return control.record.current_orchestration_instance_id if control else None


def _full_sync_is_running(repository, source_id: str) -> bool:
    """True if full_sync_orchestrator is currently active for this source.

    delta-sync/acl-resync skip their tick while this is true: full-sync's
    discover_all() has no knowledge of delta-sync's cursor (and vice versa), so
    running them concurrently on the same changed file could create two
    simultaneously-"ready" versions with neither retiring the other.

    Checked via Cosmos rather than Durable's per-instance-ID status: this app never
    reuses instance IDs, so there is no fixed ID to poll (see start_full_sync).
    """
    from ingestion.models import RunStatus
    control = repository.get_source_control(source_id)
    if control is None:
        return False
    run = repository.get_run(source_id, control.record.current_run_id)
    return run is not None and run.record.status == RunStatus.RUNNING


async def _start_if_not_running(client, lifecycle_repository, source_id: str, control_id: str, orchestrator_name: str) -> str | None:
    """Start `orchestrator_name` with a fresh, never-reused instance ID, unless the tick
    tracked under `control_id` is still Running/Pending. Returns the new instance ID, or
    None if a previous tick is still active.

    Mirrors start_full_sync's fix: Durable instance-ID reuse is best-effort/racy at the
    storage layer (Azure/azure-functions-durable-python#410), so we never poll or start a
    fixed ID — the last-dispatched ID is tracked in Cosmos purely so we know what to poll.
    """
    import uuid
    existing_id = lifecycle_repository.get_trigger_instance_id(source_id, control_id)
    if existing_id:
        existing = await client.get_status(existing_id)
        if existing and existing.runtime_status in (
            df.OrchestrationRuntimeStatus.Running,
            df.OrchestrationRuntimeStatus.Pending,
        ):
            return None
    instance_id = f"{control_id}-{uuid.uuid4().hex}"
    lifecycle_repository.save_trigger_instance_id(source_id, control_id, instance_id)
    await client.start_new(orchestrator_name, instance_id=instance_id)
    return instance_id


@app.timer_trigger(schedule=RECONCILIATION_SCHEDULE, arg_name="timer", run_on_startup=False, use_monitor=True)
@app.durable_client_input(client_name="client")
async def reconciliation_timer(timer: func.TimerRequest, client) -> None:
    """Daily safety-net delta-sync — catches changes missed by webhooks."""
    from config import load_config
    from ingestion.lifecycle_repository import DELTA_SYNC_TRIGGER_ID
    source_id = os.getenv("INGESTION_SOURCE_ID", "").strip()
    if not source_id:
        logger.warning("reconciliation_timer skipped: missing INGESTION_SOURCE_ID")
        return
    config = load_config()
    repository = _build_repository(config)
    if _full_sync_is_running(repository, source_id):
        logger.info("reconciliation_timer skipped: full-sync is running")
        return
    lifecycle_repository = _build_lifecycle_repository(config)
    started = await _start_if_not_running(client, lifecycle_repository, source_id, DELTA_SYNC_TRIGGER_ID, "delta_sync_orchestrator")
    if not started:
        logger.info("reconciliation_timer skipped: previous tick still running")


@app.timer_trigger(schedule=ACL_RESYNC_SCHEDULE, arg_name="timer", run_on_startup=False, use_monitor=True)
@app.durable_client_input(client_name="client")
async def acl_resync_timer(timer: func.TimerRequest, client) -> None:
    """Kick off one ACL-resync pass unless the previous pass, or a full-sync,
    is still running."""
    from config import load_config
    from ingestion.lifecycle_repository import ACL_RESYNC_TRIGGER_ID
    source_id = os.getenv("INGESTION_SOURCE_ID", "").strip()
    if not source_id:
        logger.warning("acl_resync_timer skipped: missing INGESTION_SOURCE_ID")
        return
    config = load_config()
    repository = _build_repository(config)
    if _full_sync_is_running(repository, source_id):
        logger.info("acl_resync_timer skipped: full-sync is running")
        return
    lifecycle_repository = _build_lifecycle_repository(config)
    started = await _start_if_not_running(client, lifecycle_repository, source_id, ACL_RESYNC_TRIGGER_ID, "acl_resync_orchestrator")
    if not started:
        logger.info("acl_resync_timer skipped: previous pass still running")



@app.route(route="webhook/sharepoint", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@app.durable_client_input(client_name="client")
async def webhook_sharepoint(req: func.HttpRequest, client) -> func.HttpResponse:
    """Receive Microsoft Graph change notifications for the subscribed drive."""
    validation_token = req.params.get("validationToken")
    if validation_token:
        return func.HttpResponse(validation_token, status_code=200, mimetype="text/plain")

    if not WEBHOOK_CLIENT_STATE:
        logger.error("webhook_sharepoint: WEBHOOK_CLIENT_STATE not configured")
        return func.HttpResponse(status_code=500)

    try:
        payload = req.get_json()
    except ValueError:
        return func.HttpResponse(status_code=400)

    from ingestion.subscription import validate_webhook_notification
    if not validate_webhook_notification(payload, WEBHOOK_CLIENT_STATE):
        return func.HttpResponse(status_code=403)

    source_id = os.getenv("INGESTION_SOURCE_ID", "").strip()
    if not source_id:
        return func.HttpResponse(status_code=200)

    from config import load_config
    from ingestion.lifecycle_repository import DELTA_SYNC_TRIGGER_ID
    config = load_config()
    repository = _build_repository(config)
    if _full_sync_is_running(repository, source_id):
        logger.info("webhook_sharepoint: full-sync running, skipping delta trigger")
        return func.HttpResponse(status_code=200)

    lifecycle_repository = _build_lifecycle_repository(config)
    started = await _start_if_not_running(client, lifecycle_repository, source_id, DELTA_SYNC_TRIGGER_ID, "delta_sync_orchestrator")
    if not started:
        logger.info("webhook_sharepoint: delta-sync already running")
        return func.HttpResponse(status_code=200)

    logger.info("webhook_sharepoint: started delta_sync_orchestrator")
    _write_webhook_audit(source_id, "webhook_received", {"action": "delta_sync_triggered"})
    return func.HttpResponse(status_code=200)


@app.route(route="webhook/lifecycle", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
async def webhook_lifecycle(req: func.HttpRequest) -> func.HttpResponse:
    """Handle Graph subscription lifecycle events (missed, removed, reauthorizationRequired)."""
    validation_token = req.params.get("validationToken")
    if validation_token:
        return func.HttpResponse(validation_token, status_code=200, mimetype="text/plain")

    try:
        payload = req.get_json()
    except ValueError:
        return func.HttpResponse(status_code=400)

    notifications = payload.get("value", [])
    for notification in notifications:
        event = notification.get("lifecycleEvent", "")
        logger.warning("webhook_lifecycle: %s for subscription %s", event, notification.get("subscriptionId"))
    return func.HttpResponse(status_code=200)


@app.timer_trigger(schedule=SUBSCRIPTION_RENEW_SCHEDULE, arg_name="timer", run_on_startup=False, use_monitor=True)
async def subscription_renew_timer(timer: func.TimerRequest) -> None:
    """Create or renew the Microsoft Graph webhook subscription for drive changes."""
    from config import load_config
    from ingestion.subscription import create_subscription, renew_subscription, SubscriptionNotFoundError

    source_id = os.getenv("INGESTION_SOURCE_ID", "").strip()
    if not source_id or not WEBHOOK_CLIENT_STATE:
        logger.warning("subscription_renew_timer skipped: missing config")
        return

    config = load_config()
    graph_client = _build_graph_client(config)
    base_url = os.getenv("FUNCTION_PUBLIC_BASE_URL", "").strip()
    if not base_url:
        logger.warning("subscription_renew_timer skipped: FUNCTION_PUBLIC_BASE_URL not set")
        return

    notification_url = f"{base_url}/api/webhook/sharepoint"
    lifecycle_url = f"{base_url}/api/webhook/lifecycle"

    lifecycle_repository = _build_lifecycle_repository(config)
    existing_id = lifecycle_repository.get_webhook_subscription_id(source_id)

    if existing_id:
        try:
            info = renew_subscription(graph_client, existing_id)
            logger.info("subscription_renewed: %s expires %s", info.subscription_id, info.expiration)
            return
        except SubscriptionNotFoundError:
            logger.info("subscription_expired, creating new one")

    info = create_subscription(
        graph_client, config.drive_id, notification_url, lifecycle_url, WEBHOOK_CLIENT_STATE,
    )
    lifecycle_repository.save_webhook_subscription_id(source_id, info.subscription_id)
    logger.info("subscription_created: %s expires %s", info.subscription_id, info.expiration)


@app.orchestration_trigger(context_name="context")
def full_sync_orchestrator(context: df.DurableOrchestrationContext):
    """Orchestrate: activate → discover → fan-out process → finalize."""
    from datetime import timedelta
    retry = df.RetryOptions(first_retry_interval_in_milliseconds=5000, max_number_of_attempts=5)

    activated = yield context.call_activity_with_retry(
        "activate_run_activity", retry, {"instanceId": context.instance_id}
    )
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
        deadline = context.current_utc_datetime + timedelta(minutes=WAVE_TIMEOUT_MINUTES)
        timer = context.create_timer(deadline)
        wave_task = context.task_all(tasks)
        try:
            winner = yield context.task_any([wave_task, timer])
        except Exception:
            # One or more activities exhausted all retries (e.g. sustained 429s).
            # task_all fails fast in Python, so sweep this run for any docs still
            # stuck non-terminal rather than guessing which ones completed.
            timer.cancel()
            swept = yield context.call_activity(
                "fail_wave_documents_activity", {"runId": run_id, "reason": "wave_retry_exhausted"},
            )
            total_failed += swept.get("failedCount", len(wave))
            continue
        if winner == timer:
            total_failed += len(wave)
        else:
            timer.cancel()
            results = wave_task.result
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


@app.orchestration_trigger(context_name="context")
def delta_sync_orchestrator(context: df.DurableOrchestrationContext):
    """One bounded delta-sync tick: adds/updates/deletes since the last cursor."""
    retry = df.RetryOptions(first_retry_interval_in_milliseconds=5000, max_number_of_attempts=3)
    result = yield context.call_activity_with_retry("delta_sync_activity", retry, None)
    if result.get("error"):
        return {"status": "failed", "error": result["error"]}
    # Webhook fired but delta saw no content changes — check for permission drift
    if result.get("itemsSeen", -1) == 0:
        acl = yield context.call_activity_with_retry(
            "acl_resync_page_activity", retry, {"continuationToken": None}
        )
        if not acl.get("error"):
            result["aclResynced"] = acl.get("updated", 0)
    return {"status": "completed", **result}


@app.orchestration_trigger(context_name="context")
def acl_resync_orchestrator(context: df.DurableOrchestrationContext):
    """Page through all ready documents re-verifying ACLs."""
    retry = df.RetryOptions(first_retry_interval_in_milliseconds=5000, max_number_of_attempts=3)
    continuation_token: str | None = None
    total_checked = total_updated = total_retired = 0
    while True:
        result = yield context.call_activity_with_retry(
            "acl_resync_page_activity", retry, {"continuationToken": continuation_token}
        )
        if result.get("error"):
            return {"status": "failed", "error": result["error"]}
        total_checked += result["checked"]
        total_updated += result["updated"]
        total_retired += result["retired"]
        continuation_token = result.get("continuationToken")
        if continuation_token is None:
            break
    return {
        "status": "completed",
        "checked": total_checked,
        "updated": total_updated,
        "retired": total_retired,
    }


@app.orchestration_trigger(context_name="context")
def retry_failed_orchestrator(context: df.DurableOrchestrationContext):
    """Reprocess only the failed documents that were reset to discovered."""
    from datetime import timedelta
    retry = df.RetryOptions(first_retry_interval_in_milliseconds=5000, max_number_of_attempts=5)
    documents = context.get_input()
    if not documents:
        return {"status": "nothing_to_retry"}
    run_id = documents[0].get("sourceRunId", "unknown")
    succeeded = 0
    failed = 0
    for doc_ref in documents:
        task = context.call_activity_with_retry(
            "process_document_activity", retry, {"runId": run_id, "document": doc_ref},
        )
        deadline = context.current_utc_datetime + timedelta(minutes=WAVE_TIMEOUT_MINUTES)
        timer = context.create_timer(deadline)
        try:
            winner = yield context.task_any([task, timer])
        except Exception:
            timer.cancel()
            swept = yield context.call_activity(
                "fail_wave_documents_activity", {"runId": run_id, "reason": "retry_exhausted"},
            )
            failed += max(swept.get("failedCount", 0), 1)
            continue
        if winner == timer:
            failed += 1
        else:
            timer.cancel()
            result = task.result
            if result.get("status") == "succeeded":
                succeeded += 1
            else:
                failed += 1
    return {"status": "completed", "retried": len(documents), "succeeded": succeeded, "failed": failed}


@app.activity_trigger(input_name="payload")
def activate_run_activity(payload: dict) -> dict:
    from config import load_config
    from ingestion.services import activate
    try:
        config = load_config()
        repository = _build_repository(config)
        activated = activate(config, repository, payload["instanceId"])
        return {"runId": activated.run.record.run_id, "runEtag": activated.run.etag}
    except Exception as error:
        logger.exception("activate_run_activity failed")
        return {"error": str(error)}


@app.activity_trigger(input_name="payload")
def discover_all_activity(payload: dict) -> dict:
    from config import load_config
    from ingestion.services import discover_all
    from ingestion.source_connector import SharePointConnector
    try:
        config = load_config()
        repository = _build_repository(config)
        graph_client = _build_graph_client(config)
        connector = SharePointConnector(graph_client, config.drive_id)
        documents, items_scanned = discover_all(config, payload["runId"], repository, connector)
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
    from ingestion.source_connector import SharePointConnector
    document_ref = payload["document"]
    try:
        config = load_config()
        repository = _build_repository(config)
        graph_client = _build_graph_client(config)
        di_client = _build_di_client(config) if config.extraction_enabled else None
        language_client = _build_language_client(config) if config.enrichment_enabled else None
        openai_client = _build_openai_client(config)
        audit_container = _build_audit_container(config)

        stored = repository.get_document(document_ref["sourceRunId"], document_ref["documentId"])
        if stored is None:
            return {"documentId": document_ref["documentId"], "status": "skipped"}

        sp_client = _build_sharepoint_client(config)
        connector = SharePointConnector(graph_client, config.drive_id, sp_client=sp_client, site_url=config.sharepoint_site_url)
        outcome = process_document(
            config, stored.record, stored.etag, repository,
            connector, di_client, language_client, openai_client,
            audit_container=audit_container,
        )
        if outcome.status.value == "succeeded":
            _retire_prior_version(config, document_ref["documentId"], document_ref["sourceRunId"], audit_container)
        return {
            "documentId": outcome.document_id,
            "status": outcome.status.value,
            "chunksWritten": outcome.chunks_written,
            "error": outcome.error.code if outcome.error else None,
        }
    except Exception as error:
        from ingestion.models import safe_error_from_exception
        logger.exception("process_document_activity failed")
        safe = safe_error_from_exception(error, "activity")
        # Retryable errors must propagate so call_activity_with_retry actually retries, but
        # re-raise a sanitized message: a raw exception can embed a signed download URL,
        # tripping a host bug that corrupts Durable's replay state for exceptions containing
        # credential-like tokens (Azure/azure-functions-durable-python#600).
        if safe.retryable:
            raise TimeoutError(safe.code) from None
        return {"documentId": document_ref.get("documentId", ""), "status": "failed", "error": safe.code}


@app.activity_trigger(input_name="payload")
def fail_wave_documents_activity(payload: dict) -> dict:
    """Force-fail any discovered/processing docs for this run after retry exhaustion."""
    from config import load_config
    try:
        config = load_config()
        repository = _build_repository(config)
        failed_count = repository.fail_nonterminal_documents(
            config.source_id, payload["runId"], payload.get("reason", "wave_retry_exhausted"),
        )
        return {"failedCount": failed_count}
    except Exception as error:
        logger.exception("fail_wave_documents_activity failed")
        return {"error": str(error), "failedCount": 0}


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


@app.activity_trigger(input_name="payload")
def delta_sync_activity(payload: Any) -> dict:
    from config import load_config
    from ingestion.services import run_delta_sync
    from ingestion.source_connector import SharePointConnector
    try:
        config = load_config()
        repository = _build_repository(config)
        lifecycle_repository = _build_lifecycle_repository(config)
        graph_client = _build_graph_client(config)
        di_client = _build_di_client(config) if config.extraction_enabled else None
        language_client = _build_language_client(config) if config.enrichment_enabled else None
        openai_client = _build_openai_client(config)
        audit_container = _build_audit_container(config)
        sp_client = _build_sharepoint_client(config)
        connector = SharePointConnector(graph_client, config.drive_id, sp_client=sp_client, site_url=config.sharepoint_site_url)
        outcome = run_delta_sync(
            config, repository, lifecycle_repository, connector,
            di_client, language_client, openai_client,
            audit_container=audit_container,
        )
        logger.info(
            "delta_sync_completed bootstrapped=%s created_or_updated=%d deleted=%d acl_resynced=%d failed=%d items_seen=%d",
            outcome.bootstrapped, outcome.created_or_updated, outcome.deleted, outcome.acl_resynced, outcome.failed, outcome.items_seen,
        )
        return {
            "bootstrapped": outcome.bootstrapped,
            "createdOrUpdated": outcome.created_or_updated,
            "deleted": outcome.deleted,
            "aclResynced": outcome.acl_resynced,
            "failed": outcome.failed,
            "itemsSeen": outcome.items_seen,
        }
    except Exception as error:
        logger.exception("delta_sync_activity failed")
        return {"error": str(error)}


@app.activity_trigger(input_name="payload")
def acl_resync_page_activity(payload: dict) -> dict:
    from config import load_config
    from ingestion.services import run_acl_resync_page
    from ingestion.source_connector import SharePointConnector
    try:
        config = load_config()
        lifecycle_repository = _build_lifecycle_repository(config)
        graph_client = _build_graph_client(config)
        sp_client = _build_sharepoint_client(config)
        audit_container = _build_audit_container(config)
        connector = SharePointConnector(graph_client, config.drive_id, sp_client=sp_client, site_url=config.sharepoint_site_url)
        outcome, token = run_acl_resync_page(
            config, lifecycle_repository, connector,
            page_size=ACL_RESYNC_PAGE_SIZE,
            continuation_token=payload.get("continuationToken"),
            audit_container=audit_container,
        )
        logger.info(
            "acl_resync_page_completed checked=%d unchanged=%d updated=%d retired=%d has_more=%s",
            outcome.checked, outcome.unchanged, outcome.updated, outcome.retired, token is not None,
        )
        return {
            "checked": outcome.checked,
            "unchanged": outcome.unchanged,
            "updated": outcome.updated,
            "retired": outcome.retired,
            "continuationToken": token,
        }
    except Exception as error:
        logger.exception("acl_resync_page_activity failed")
        return {"error": str(error)}



def _retire_prior_version(config, document_id: str, current_source_run_id: str, audit_container=None) -> None:
    """After full-sync re-processes a file, hard-delete any old ready version from a prior run."""
    from ingestion.lifecycle_repository import LifecycleConflictError
    from ingestion.telemetry import write_audit_record
    try:
        lifecycle_repo = _build_lifecycle_repository(config)
        ref = lifecycle_repo.find_ready_document_by_document_id(document_id)
        if ref is not None and ref.source_run_id != current_source_run_id:
            lifecycle_repo.delete_document_and_chunks(
                source_run_id=ref.source_run_id,
                document_id=document_id,
                document_key=ref.document_key,
                etag=ref.etag,
            )
            if audit_container is not None:
                write_audit_record(audit_container, config.source_id, current_source_run_id, {
                    "operation": "document_deleted", "documentId": document_id,
                    "reason": "superseded", "method": "full_sync",
                    "replacedDocumentKey": ref.document_key,
                })
    except LifecycleConflictError:
        pass
    except Exception:
        logger.warning("retire_prior_version failed for %s", document_id, exc_info=True)


# Built once per worker and reused across invocations (Azure Functions Python guidance).
_client_cache: dict[str, Any] = {}


def _build_repository(config):
    if "repository" not in _client_cache:
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential
        from ingestion.repository import IngestionRepository
        credential = DefaultAzureCredential(managed_identity_client_id=config.managed_identity_client_id)
        cosmos = CosmosClient(config.cosmos_endpoint, credential=credential)
        db = cosmos.get_database_client(config.cosmos_database)
        _client_cache["repository"] = IngestionRepository(
            db.get_container_client(config.cosmos_ingestion_runs_container),
            db.get_container_client(config.cosmos_source_documents_container),
            db.get_container_client(config.cosmos_search_chunks_container),
        )
    return _client_cache["repository"]


def _build_lifecycle_repository(config):
    if "lifecycle_repository" not in _client_cache:
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential
        from ingestion.lifecycle_repository import DocumentLifecycleRepository
        credential = DefaultAzureCredential(managed_identity_client_id=config.managed_identity_client_id)
        cosmos = CosmosClient(config.cosmos_endpoint, credential=credential)
        db = cosmos.get_database_client(config.cosmos_database)
        _client_cache["lifecycle_repository"] = DocumentLifecycleRepository(
            db.get_container_client(config.cosmos_ingestion_runs_container),
            db.get_container_client(config.cosmos_source_documents_container),
            db.get_container_client(config.cosmos_search_chunks_container),
        )
    return _client_cache["lifecycle_repository"]


def _write_webhook_audit(source_id: str, operation: str, extra: dict) -> None:
    """Best-effort audit record for webhook events."""
    try:
        from config import load_config
        config = load_config()
        container = _build_audit_container(config)
        from ingestion.telemetry import write_audit_record
        write_audit_record(container, source_id, "webhook", {"operation": operation, **extra})
    except Exception:
        logger.warning("webhook_audit_write_failed", exc_info=True)


def _build_graph_client(config):
    """Cached across invocations — do not wrap call sites in `with graph_client:`, that
    would close this shared client after first use."""
    if "graph_client" not in _client_cache:
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
        _client_cache["graph_client"] = httpx.Client(
            auth=GraphCredentialAuth(graph_credential, "https://graph.microsoft.com/.default"), transport=transport, timeout=120,
        )
    return _client_cache["graph_client"]


def _build_sharepoint_client(config):
    """Build an httpx.Client for SharePoint REST API using the same cert but SP scope. Cached (see _build_graph_client)."""
    if "sharepoint_client" not in _client_cache:
        from azure.identity import CertificateCredential, DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        from ingestion.graph import GraphCredentialAuth
        import httpx
        import base64

        if not config.sharepoint_site_url:
            _client_cache["sharepoint_client"] = None
        else:
            credential = DefaultAzureCredential(managed_identity_client_id=config.managed_identity_client_id)
            secret_client = SecretClient(config.key_vault_uri, credential)
            cert_secret = secret_client.get_secret(config.certificate_secret_name)
            cert_data = base64.b64decode(cert_secret.value, validate=True)
            from urllib.parse import urlparse
            host = urlparse(config.sharepoint_site_url).hostname or ""
            sp_scope = f"https://{host}/.default"
            sp_credential = CertificateCredential(
                tenant_id=config.tenant_id,
                client_id=config.app_client_id,
                certificate_data=cert_data,
            )
            transport = httpx.HTTPTransport(retries=3)
            _client_cache["sharepoint_client"] = httpx.Client(auth=GraphCredentialAuth(sp_credential, sp_scope), transport=transport, timeout=60)
    return _client_cache["sharepoint_client"]


def _build_di_client(config):
    if "di_client" not in _client_cache:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.identity import DefaultAzureCredential
        credential = DefaultAzureCredential(managed_identity_client_id=config.managed_identity_client_id)
        _client_cache["di_client"] = DocumentIntelligenceClient(endpoint=config.document_intelligence_endpoint, credential=credential)
    return _client_cache["di_client"]


def _build_language_client(config):
    if "language_client" not in _client_cache:
        from azure.ai.textanalytics import TextAnalyticsClient
        from azure.identity import DefaultAzureCredential
        credential = DefaultAzureCredential(managed_identity_client_id=config.managed_identity_client_id)
        _client_cache["language_client"] = TextAnalyticsClient(endpoint=config.language_endpoint, credential=credential, api_version="2023-04-01")
    return _client_cache["language_client"]


def _build_openai_client(config):
    if "openai_client" not in _client_cache:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import AzureOpenAI
        credential = DefaultAzureCredential(managed_identity_client_id=config.managed_identity_client_id)
        _client_cache["openai_client"] = AzureOpenAI(
            azure_endpoint=config.openai_endpoint,
            azure_ad_token_provider=get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default"),
            api_version="2024-10-21",
        )
    return _client_cache["openai_client"]


def _build_audit_container(config):
    if "audit_container" not in _client_cache:
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential
        from ingestion.telemetry import COSMOS_AUDIT_CONTAINER_NAME
        credential = DefaultAzureCredential(managed_identity_client_id=config.managed_identity_client_id)
        cosmos = CosmosClient(config.cosmos_endpoint, credential=credential)
        db = cosmos.get_database_client(config.cosmos_database)
        _client_cache["audit_container"] = db.get_container_client(COSMOS_AUDIT_CONTAINER_NAME)
    return _client_cache["audit_container"]


def _json_response(payload: dict, status_code: int) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(payload, default=str), status_code=status_code, mimetype="application/json")
