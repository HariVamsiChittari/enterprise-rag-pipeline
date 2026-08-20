# API Reference

Complete HTTP API for the Enterprise RAG Pipeline. All routes exposed by the Function App and the retrieval service, with headers, request bodies, responses, and error codes.

## Contents

- [Authentication](#authentication)
- [Common Headers](#common-headers)
- [Ingestion Endpoints](#ingestion-endpoints) (Function App)
- [Query Endpoint](#query-endpoint) (Function App proxy → retrieval)
- [Webhook Endpoints](#webhook-endpoints) (Function App, unauthenticated)
- [Retrieval Service Endpoints](#retrieval-service-endpoints) (ACA/AKS, internal)
- [Error Responses](#error-responses)

---

## Authentication

The Function App uses **App Service EasyAuth** with Microsoft Entra ID. Every operator endpoint requires either a Bearer token or a Function App master key.

### Option 1: Bearer Token (recommended for interactive use)

```powershell
$clientId = "<ADMIN_API_CLIENT_ID>"   # from AZURE_SETUP.md §1.4
$token = az account get-access-token --resource "api://$clientId" --query accessToken -o tsv
$headers = @{ Authorization = "Bearer $token" }
```

### Option 2: Function App Master Key (recommended for automation)

```powershell
$masterKey = az functionapp keys list --name <func-app> --resource-group <rg> --query masterKey -o tsv
$headers = @{ "x-functions-key" = $masterKey }
```

### Local Development (bypass EasyAuth)

When running the Function App locally (`func start`), authentication is bypassed. To simulate a caller identity for the retrieval service, set `X-MS-CLIENT-PRINCIPAL` manually:

```powershell
# Base64-encoded JSON of {claims: [{typ, val}, ...]} — must include oid, tid, and groups
$claims = @{
  claims = @(
    @{ typ = "oid"; val = "<your-user-oid>" }
    @{ typ = "tid"; val = "<your-tenant-id>" }
  )
} | ConvertTo-Json -Compress
$principal = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($claims))
$headers["X-MS-CLIENT-PRINCIPAL"] = $principal
```

---

## Common Headers

| Header | Required | Value | Notes |
|---|---|---|---|
| `Authorization` | Yes (operator endpoints) | `Bearer <token>` | EasyAuth-validated Entra token |
| `x-functions-key` | Alternative to Bearer | Function App master key | Bypasses EasyAuth |
| `Content-Type` | Yes (POST/DELETE with body) | `application/json` | UTF-8 |
| `X-MS-CLIENT-PRINCIPAL` | Auto-set by EasyAuth | Base64 JSON | Forwarded by `/api/query` proxy |
| `X-MS-CLIENT-PRINCIPAL-NAME` | Auto-set by EasyAuth | Caller's UPN/email | Used by purge audit trail |

---

## Ingestion Endpoints

Base URL: `https://<function-app-name>.azurewebsites.net`

### `POST /api/ingestion/full-sync`

Start a full-sync orchestration. Discovers all files in the SharePoint drive, processes each in parallel waves, and writes chunks to Cosmos.

**Request**

```http
POST /api/ingestion/full-sync HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json

(empty body)
```

**Response — 202 Accepted**

```json
{
  "id": "sharepoint-drive-sync",
  "statusQueryGetUri": "https://<func>.azurewebsites.net/runtime/webhooks/durabletask/instances/sharepoint-drive-sync?...",
  "sendEventPostUri": "...",
  "terminatePostUri": "..."
}
```

**Error responses**

| Status | Body | Reason |
|---|---|---|
| 409 | `{"status":"already_running","instanceId":"..."}` | Full-sync already in progress |
| 503 | `{"error":"missing_ingestion_source_id"}` | `INGESTION_SOURCE_ID` env var not set |

---

### `GET /api/ingestion/status`

Query the runtime status of an orchestration instance (full-sync, delta-sync, or ACL-resync).

**Request**

```http
GET /api/ingestion/status?instanceId=<id> HTTP/1.1
Authorization: Bearer <token>
```

**Query parameters**

| Name | Required | Default | Description |
|---|---|---|---|
| `instanceId` | No | Full-sync singleton for the current `INGESTION_SOURCE_ID` | Any orchestration instance ID |

Common instance IDs:

- Full-sync: `<source_id>-sync` (e.g. `sharepoint-drive-sync`)
- Delta-sync: `delta-sync-<source_id>`
- ACL-resync: `acl-resync-<source_id>`

**Response — 200 OK**

```json
{
  "instanceId": "sharepoint-drive-sync",
  "runtimeStatus": "OrchestrationRuntimeStatus.Completed",
  "output": {
    "status": "completed",
    "runId": "run:sharepoint-drive:2026-08-19T...",
    "discovered": 22,
    "succeeded": 22,
    "failed": 0,
    "runStatus": "completed"
  },
  "createdTime": "2026-08-19T17:25:05+00:00",
  "lastUpdatedTime": "2026-08-19T17:32:11+00:00"
}
```

**Error responses**

| Status | Body | Reason |
|---|---|---|
| 404 | `{"error":"not_found"}` | No orchestration with that instance ID |

---

### `POST /api/ingestion/terminate`

Terminate a running orchestration, force-fail any stuck documents, and finalize the run as `TERMINATED`.

**Request**

```http
POST /api/ingestion/terminate?instanceId=<id> HTTP/1.1
Authorization: Bearer <token>
```

**Query parameters**

| Name | Required | Default | Description |
|---|---|---|---|
| `instanceId` | No | Full-sync singleton | Instance to terminate |

**Response — 200 OK**

```json
{
  "status": "terminated",
  "runId": "run:sharepoint-drive:...",
  "docsForceFailed": 3,
  "counters": {"discovered": 22, "ready": 19, "failed": 3},
  "orchestrationId": "sharepoint-drive-sync"
}
```

**Alternative responses (all 200 OK)**

| `status` value | Meaning |
|---|---|
| `terminated` | Orchestration was running; force-failed non-terminal docs and finalized as TERMINATED |
| `no_active_run` | No source-control record or current run — nothing to terminate |
| `already_terminal` | Run has already reached a terminal state (completed/failed/terminated); returns the existing `runStatus` |

---

### `POST /api/ingestion/retry-failed`

Reprocess only the failed documents from the current run. Skips re-scanning the entire corpus.

**Request**

```http
POST /api/ingestion/retry-failed HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json

(empty body)
```

**Response — 202 Accepted**

```json
{
  "status": "retrying",
  "count": 3,
  "orchestrationId": "retry-failed-sharepoint-drive-sync"
}
```

**Alternative responses**

| Status | Body | Reason |
|---|---|---|
| 200 | `{"status":"nothing_to_retry","failed":0}` | No failed documents in current run |
| 409 | `{"error":"sync_in_progress"}` | Full-sync currently running (must complete first) |
| 500 | `{"status":"reset_failed","failed":3}` | Could not reset document status to discovered |

---

### `GET /api/ingestion/inspect`

Read rows from any Cosmos container for debugging. Sanitizes system fields (`_ts`, `_etag`, etc).

**Request**

```http
GET /api/ingestion/inspect?container=<name>&limit=<n>&runId=<id> HTTP/1.1
Authorization: Bearer <token>
```

**Query parameters**

| Name | Required | Default | Description |
|---|---|---|---|
| `container` | Yes | — | One of: `ingestion-runs`, `source-documents`, `search-chunks`, `service-audit` |
| `limit` | No | `10` | Number of rows (max `200`) |
| `runId` | No | — | If set, filter by partition `<source_id>:<runId>` (avoids cross-partition query) |

**Response — 200 OK**

```json
{
  "container": "source-documents",
  "count": 3,
  "rows": [
    {
      "id": "doc:sharepoint-drive:...",
      "sourceRunId": "sharepoint-drive:run:...",
      "sourceName": "Policy.pdf",
      "status": "ready",
      "stage": "terminal",
      "allowedGroupIds": ["4eac97d2-...", "ba671fcb-..."],
      "aclHash": "sha256:...",
      "eTag": "..."
    }
  ]
}
```

**Error responses**

| Status | Body | Reason |
|---|---|---|
| 400 | `{"error":"invalid_container","allowed":["..."]}` | Container name not in allowlist |
| 503 | `{"error":"cosmos_query_failed"}` | Cosmos SDK exception |

---

### `DELETE /api/ingestion/purge`

Delete items from a Cosmos container with an audit record. Refuses to purge `service-audit`.

**Request**

```http
DELETE /api/ingestion/purge HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json

{
  "container": "search-chunks",
  "ids": ["chunk1", "chunk2"]
}
```

**Body schema**

| Field | Type | Required | Description |
|---|---|---|---|
| `container` | string | Yes | One of: `ingestion-runs`, `source-documents`, `search-chunks` |
| `ids` | array<string> | Conditional | List of item IDs (max 100). Required if `purgeAll` is not set. |
| `purgeAll` | boolean | Conditional | If `true`, delete all items in the container |
| `confirm` | string | Required if `purgeAll` | Must equal `"yes"` — safety guard |

**Response — 200 OK**

```json
{
  "deleted": 2,
  "failed": 0,
  "auditId": "5ff5c1a0-...-uuid"
}
```

**Error responses**

| Status | Body | Reason |
|---|---|---|
| 400 | `{"error":"invalid_json"}` | Body is not valid JSON |
| 400 | `{"error":"invalid_container","allowed":["..."]}` | Container name not allowed |
| 400 | `{"error":"provide 'ids' (list) or 'purgeAll':true"}` | Missing target |
| 400 | `{"error":"purgeAll requires 'confirm':'yes'"}` | Safety guard failed |
| 400 | `{"error":"ids must be a list with max 100 items"}` | Bulk limit exceeded |
| 503 | `{"error":"purge_failed"}` | Cosmos error during delete |

**Audit trail**: every purge writes a record to the `service-audit` container with the operator's UPN (`X-MS-CLIENT-PRINCIPAL-NAME` header), `deletedIds` (first 100), `deletedCount`, `failedCount`, `purgeAll`, and `timestamp`.

---

## Query Endpoint

### `POST /api/query`

RAG query. The Function App validates the caller's Entra token, then proxies to the retrieval service (ACA/AKS) with the `X-MS-CLIENT-PRINCIPAL` header forwarded.

**Request**

```http
POST /api/query HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json

{
  "question": "What is the password policy?",
  "mode": "hybrid",
  "history": [],
  "top_k": 5
}
```

**Body schema**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `question` | string | Yes | — | 1–4000 characters |
| `mode` | string | No | `"hybrid"` | One of: `hybrid`, `vector`, `full_text` |
| `history` | array<object> | No | `[]` | Conversation history for multi-turn queries. Each item: `{role: "user"\|"assistant", content: "..."}`. Bounded to last 10 messages. |
| `top_k` | integer | No | `MAX_EVIDENCE_CHUNKS` env var (default 5) | Chunks to retrieve (1–20) |

**Path selection is automatic** based on LLM query planner output:

- 1 planned query → **Standard RAG path** (~5s)
- 2–3 planned queries → **Agentic RAG path** (~8–10s, with automatic fallback to standard on timeout)

**Response — 200 OK**

```json
{
  "answer": "The policy requires MFA [S1] and periodic password reviews [S2] every 90 days.",
  "citations": [
    {
      "ref": "[S1]",
      "source_name": "04_Password_and_Authentication_Policy.pdf",
      "url": "https://<tenant>.sharepoint.com/.../04_Password_and_Authentication_Policy.pdf#page=3"
    },
    {
      "ref": "[S2]",
      "source_name": "04_Password_and_Authentication_Policy.pdf",
      "url": "https://<tenant>.sharepoint.com/.../04_Password_and_Authentication_Policy.pdf#page=8"
    }
  ],
  "request_id": "3f8c9e5a-...-uuid"
}
```

**Response schema**

| Field | Type | Description |
|---|---|---|
| `answer` | string | Grounded answer with `[S#]` citation markers |
| `citations` | array<object> | One per unique source chunk. Empty when `INCLUDE_CITATIONS=false`. |
| `citations[].ref` | string | Matches `[S#]` markers in `answer` |
| `citations[].source_name` | string | Original file name |
| `citations[].url` | string | SharePoint URL with `#page=N` fragment |
| `request_id` | string | UUID for log correlation |

**Error responses**

| Status | Body | Reason |
|---|---|---|
| 400 | `{"error":"question_required"}` | Missing or empty `question` |
| 429 | `{"detail":"rate_limit_exceeded"}` | Per-user **per-replica** sliding window exceeded (default 30 RPM; effective ceiling ≈ `RATE_LIMIT_RPM × replicaCount`, see ARCHITECTURE L8) |
| 500 | `{"error":"RETRIEVAL_SERVICE_URL must use HTTPS"}` | Misconfigured proxy target |
| 501 | `{"error":"RETRIEVAL_SERVICE_URL not configured"}` | Env var missing |
| 503 | `{"error":"service_unavailable"}` | Retrieval service unreachable (timeout, network error) |

**Notes**

- The `X-MS-CLIENT-PRINCIPAL` header is auto-forwarded from EasyAuth to the retrieval service for ACL evaluation.
- Answer generation timeout is controlled by `GENERATION_TIMEOUT_SECONDS` (retrieval service).
- Proxy timeout is controlled by `QUERY_PROXY_TIMEOUT_SECONDS` (Function App, default 30s).

---

## Webhook Endpoints

Webhook endpoints are **excluded from EasyAuth** — they must be publicly reachable so Microsoft Graph can deliver notifications. Security is enforced by matching the `clientState` value against `WEBHOOK_CLIENT_STATE`.

### `POST /api/webhook/sharepoint`

Receive Microsoft Graph change notifications for the subscribed drive. Triggers a delta-sync orchestration when content changes are detected.

**Two modes**:

**A. Subscription validation handshake** (Graph sends this when creating/renewing the subscription):

```http
POST /api/webhook/sharepoint?validationToken=<opaque-token> HTTP/1.1
```

Response — echo the token as `text/plain`:

```
200 OK
Content-Type: text/plain

<opaque-token>
```

**B. Change notification** (Graph sends this when a drive item changes):

```http
POST /api/webhook/sharepoint HTTP/1.1
Content-Type: application/json

{
  "value": [
    {
      "subscriptionId": "abc123-...-def456",
      "clientState": "<matches WEBHOOK_CLIENT_STATE>",
      "changeType": "updated",
      "resource": "drives/{driveId}/root",
      "resourceData": {...},
      "tenantId": "..."
    }
  ]
}
```

**Response — 200 OK** (empty body)

**Error responses**

| Status | Body | Reason |
|---|---|---|
| 400 | (empty) | Body is not valid JSON |
| 403 | (empty) | `clientState` mismatch |
| 500 | (empty) | `WEBHOOK_CLIENT_STATE` env var not configured |

**Side effects**: on valid notification, starts `delta_sync_orchestrator` (unless full-sync is running or another delta-sync is in-flight). Writes an audit record with `operation=webhook_received`.

---

### `POST /api/webhook/lifecycle`

Receive Graph subscription lifecycle events (missed notifications, subscription removed, reauthorization required).

Same validation handshake as `/api/webhook/sharepoint` (returns `validationToken` as `text/plain` when queried with `?validationToken=`).

**Change notification**:

```http
POST /api/webhook/lifecycle HTTP/1.1
Content-Type: application/json

{
  "value": [
    {
      "subscriptionId": "abc123-...",
      "lifecycleEvent": "missed",  // or "removed", "reauthorizationRequired"
      "subscriptionExpirationDateTime": "..."
    }
  ]
}
```

**Response — 200 OK** (empty body)

**Behavior**: currently logs the event as a warning. The next `subscription_renew_timer` tick will create a fresh subscription if `lifecycleEvent=removed`.

---

## Retrieval Service Endpoints

The retrieval service is **internal only** — reachable from the Function App via `RETRIEVAL_SERVICE_URL` (ACA internal FQDN or AKS ClusterIP). Direct external access is blocked by VNet integration.

Base URL: `RETRIEVAL_SERVICE_URL` (from Bicep output)

### `POST /api/query`

Same request/response as the Function App's `/api/query`, but called directly. The Function App proxy forwards `X-MS-CLIENT-PRINCIPAL`; local dev must set it manually (see [Local Development](#local-development-bypass-easyauth)).

Request headers required:

| Header | Required | Description |
|---|---|---|
| `X-MS-CLIENT-PRINCIPAL` | Yes | Base64 JSON of the caller's claims (auto-set by EasyAuth in production) |
| `Content-Type` | Yes | `application/json` |

**Direct-call error responses** (in addition to the query errors documented above):

| Status | Body | Reason |
|---|---|---|
| 401 | `{"detail":"missing_auth_header"}` | `X-MS-CLIENT-PRINCIPAL` header not set |
| 401 | `{"detail":"invalid_principal"}` | Header value is not valid base64 or has no `oid`/`tid` claims |
| 401 | `{"detail":"tenant_mismatch"}` | Token `tid` claim does not match `TENANT_ID` env var |

Everything else — body schema, response format, error codes — is identical to the Function App query endpoint above.

---

### `GET /health/live`

Liveness probe. Always returns 200 if the process is alive.

**Response — 200 OK**

```json
{"status": "alive"}
```

---

### `GET /health/ready`

Readiness probe. Verifies Cosmos DB connectivity by listing containers.

**Response — 200 OK**

```json
{"status": "ready"}
```

**Response — 503 Service Unavailable**

```json
{"detail": "cosmos_unavailable"}
```

Kubernetes / ACA uses this to route traffic only to healthy replicas.

---

## Error Responses

### Standard Envelope

Most Function App error responses use this shape:

```json
{"error": "<error_code>"}
```

Retrieval service (FastAPI) errors follow FastAPI's default:

```json
{"detail": "<message>"}
```

### HTTP Status Meanings

| Status | Meaning |
|---|---|
| 200 | Success |
| 202 | Async operation started; poll status endpoint |
| 400 | Client error — invalid input, wrong content type, missing required field |
| 401 | Missing or invalid Bearer token (EasyAuth rejected the request) |
| 403 | Token valid but caller lacks permission (webhook clientState mismatch, or EasyAuth policy) |
| 404 | Resource not found (orchestration instance, document, container) |
| 409 | Conflict — already running, or state prevents this operation |
| 429 | Rate limit exceeded (per-user **per-replica** sliding window on `/api/query`) |
| 500 | Server misconfiguration (missing env var, invalid config) |
| 501 | Required env var not configured (`RETRIEVAL_SERVICE_URL`) |
| 503 | Downstream service unreachable (Cosmos, retrieval service) |

### Common Error Codes

| Code | Endpoint | Meaning |
|---|---|---|
| `missing_ingestion_source_id` | `POST /full-sync` | `INGESTION_SOURCE_ID` env var not set |
| `already_running` | `POST /full-sync` | Full-sync in progress; wait for completion |
| `sync_in_progress` | `POST /retry-failed` | Full-sync currently running |
| `nothing_to_retry` | `POST /retry-failed` | No failed documents |
| `not_found` | `GET /status` | Orchestration instance does not exist |
| `invalid_container` | `GET /inspect`, `DELETE /purge` | Container name not in allowlist |
| `invalid_json` | `DELETE /purge` | Body is not valid JSON |
| `question_required` | `POST /query` | Missing or empty `question` field |
| `rate_limit_exceeded` | `POST /query` | Per-user **per-replica** RPM exceeded |
| `cosmos_query_failed` | `GET /inspect` | Cosmos SDK exception |
| `purge_failed` | `DELETE /purge` | Cosmos delete error |
| `service_unavailable` | `POST /query` | Retrieval service timeout or network error |

---

## PowerShell Quick Reference

```powershell
# Setup once per session
$funcApp = "<function-app-name>"
$rg = "<resource-group>"
$clientId = "<ADMIN_API_CLIENT_ID>"
$base = "https://$funcApp.azurewebsites.net"
$token = az account get-access-token --resource "api://$clientId" --query accessToken -o tsv
$h = @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" }

# Start full-sync
Invoke-RestMethod -Method POST -Uri "$base/api/ingestion/full-sync" -Headers $h

# Check status
Invoke-RestMethod -Uri "$base/api/ingestion/status" -Headers $h

# Query
$body = @{ question = "What is the password policy?"; mode = "hybrid"; top_k = 5 } | ConvertTo-Json
Invoke-RestMethod -Method POST -Uri "$base/api/query" -Headers $h -Body $body

# Inspect a container
Invoke-RestMethod -Uri "$base/api/ingestion/inspect?container=source-documents&limit=10" -Headers $h

# Purge specific IDs
$body = @{ container = "search-chunks"; ids = @("chunk1", "chunk2") } | ConvertTo-Json
Invoke-RestMethod -Method DELETE -Uri "$base/api/ingestion/purge" -Headers $h -Body $body

# Terminate a stuck orchestration
Invoke-RestMethod -Method POST -Uri "$base/api/ingestion/terminate" -Headers $h

# Retry failed documents
Invoke-RestMethod -Method POST -Uri "$base/api/ingestion/retry-failed" -Headers $h
```

---

## Related Documentation

- [README.md](../README.md) — Project overview and env var reference
- [ARCHITECTURE.md](ARCHITECTURE.md) — System design, flows, and data model
- [AZURE_SETUP.md](AZURE_SETUP.md) — Deployment guide and operations
- [E2E_TEST_RUNBOOK.md](E2E_TEST_RUNBOOK.md) — Validation scenarios with real request/response examples
