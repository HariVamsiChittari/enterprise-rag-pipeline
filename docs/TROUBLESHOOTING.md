# Troubleshooting

Living runbook of **known, verified** failure classes for the ingestion pipeline. Each entry
was reproduced and root-caused in production — this is not speculative. Add a new entry only
after root-causing a real recurring failure; do not pre-document hypothetical issues (they go
stale and are unreliable).

Format: **Symptom → Root Cause → Recovery Commands → Verification**.

---

## 1. Full-sync completes with `failed > 0` and/or `runStatus: "finalization_failed"`

### Symptom
`GET /api/ingestion/status?instanceId=<full-sync-id>` returns:
```json
{
  "runtimeStatus": "OrchestrationRuntimeStatus.Completed",
  "output": { "discovered": 22, "succeeded": 19, "failed": 3, "runStatus": "finalization_failed" }
}
```
`GET /api/ingestion/inspect?container=source-documents` shows some documents stuck at
`status: "processing"`, `error: null` (no error recorded) — they never reach `ready` or `failed`.

### Root cause
Two possible causes, check in this order:

**A. Retryable errors from a shared dependency (e.g. Azure OpenAI 429 rate limit)** during a
large fan-out burst. Confirm via Application Insights (this is the *only* place with the actual
exception — `service-audit` and `source-documents.error` do not capture it):
```powershell
az monitor app-insights query -g rg-rag-project -a rag-dev-ai --analytics-query "traces | where timestamp > ago(2h) | where severityLevel >= 3 | project timestamp, message | order by timestamp desc"
```
Look for `openai.RateLimitError` / `RateLimitReached` in the message. This is a capacity issue —
see the OpenAI TPM/RPM quota for the embedding deployment if it recurs frequently.

**B. Pre-fix code defect (resolved 2026-08-21)** — before the fix in `function_app.py`
(`process_document_activity` re-raising retryable errors, orchestrator catching wave exceptions
via `fail_wave_documents_activity`), a retryable error would be silently swallowed after 1
attempt instead of the configured 5, leaving the document permanently stuck in `processing`.
If you see this behavior on a Function App older than 2026-08-21, redeploy current
`app/function_app.py` first.

### Recovery commands
```powershell
$env:RAG_TOKEN = (az account get-access-token --resource "api://<admin-api-client-id>" --query accessToken -o tsv)
$base = "https://<func-app-name>.azurewebsites.net"
$h = @{ Authorization = "Bearer $env:RAG_TOKEN" }

# 1. Identify stuck documents
Invoke-RestMethod -Uri "$base/api/ingestion/inspect?container=source-documents&limit=50" -Headers $h |
  Select-Object -ExpandProperty rows | Where-Object { $_.status -ne "ready" } |
  Select-Object sourceName, status, stage, error

# 2. Terminate the run — force-fails non-terminal docs, finalizes as TERMINATED
Invoke-RestMethod -Uri "$base/api/ingestion/terminate" -Method POST -Headers $h

# 3. Retry only the failed documents (not a full re-scan)
Invoke-RestMethod -Uri "$base/api/ingestion/retry-failed" -Method POST -Headers $h

# 4. Poll the retry orchestration until Completed
Invoke-RestMethod -Uri "$base/api/ingestion/status?instanceId=retry-failed-<full-sync-id>" -Headers $h
```
If step 3 returns `409 sync_in_progress`, wait for the current orchestration to finish first.

### Verification
```powershell
Invoke-RestMethod -Uri "$base/api/ingestion/inspect?container=source-documents&limit=50" -Headers $h |
  Select-Object -ExpandProperty rows | Where-Object { $_.status -ne "ready" }
# Expect: no rows returned
```

---

## 2. Ingested documents show `pageCount: 2` regardless of actual page count, no error recorded

### Symptom
Every (or most) documents in `source-documents` show `pageCount: 2`, small `writtenChunkCount`
(e.g. 2 or 8), `status: "ready"`, `error: null` — looks successful but content is truncated.
Retrieval queries against later pages of the document return "insufficient evidence" even
though the source PDF clearly has that content.

### Root cause
The Document Intelligence resource is on the **F0 (Free) pricing tier**, which silently
processes only the first 2 pages of any PDF — no exception is raised. Confirmed via
[Microsoft Learn: Document Intelligence layout model — Input requirements](https://learn.microsoft.com/azure/ai-services/document-intelligence/prebuilt/layout?view=doc-intel-4.0.0#input-requirements):
"With a free-tier subscription, only the first two pages are processed."

Check the tier:
```powershell
az cognitiveservices account list -g rg-rag-project --query "[].{name:name, kind:kind, sku:sku.name}" -o table
```

### Recovery commands
```powershell
# 1. Update infra/main.parameters.dev.bicepparam: useDocumentIntelligenceFreeTier = false (and useLanguageFreeTier = false)

# 2. Deploy only the ai-services module (does not touch Cosmos/Functions/ACA/networking)
az deployment group create --resource-group rg-rag-project --template-file "infra/modules/ai-services.bicep" `
  --parameters documentIntelligenceName=<di-name> languageServiceName=<lang-name> useFreeF0=false useLanguageFreeTier=false location=<region>

# 3. Purge stale ingested data (deletes rows only, containers/schema untouched)
$body = @{ container = "search-chunks"; purgeAll = $true; confirm = "yes" } | ConvertTo-Json
Invoke-RestMethod -Uri "$base/api/ingestion/purge" -Method DELETE -Headers $h -Body $body -ContentType "application/json"
# repeat for "source-documents" and "ingestion-runs"

# 4. Re-run full-sync
Invoke-RestMethod -Uri "$base/api/ingestion/full-sync" -Method POST -Headers $h
```

### Verification
```powershell
Invoke-RestMethod -Uri "$base/api/ingestion/inspect?container=source-documents&limit=50" -Headers $h |
  Select-Object -ExpandProperty rows | Select-Object sourceName, pageCount, writtenChunkCount
# Expect: pageCount matches the real page count of each source PDF, not capped at 2
```

---

## 3. Full-sync/delta-sync/ACL-resync fails with "Non-Deterministic workflow detected"

### Symptom
`GET /api/ingestion/status?instanceId=<id>` returns:
```json
{
  "runtimeStatus": "OrchestrationRuntimeStatus.Failed",
  "output": "Non-Deterministic workflow detected: A previous execution of this orchestration scheduled a timer task with sequence number 12 named but the current orchestration replay instead produced a ScheduleTaskOrchestratorAction action with this sequence number. Was a change made to the orchestrator code after this instance had already started running?"
}
```
Fails consistently at the same replay sequence number on every retry, even with no code change
to the orchestrator.

### Root cause
**Pre-fix code defect (resolved 2026-08-24).** Before the fix, full-sync/delta-sync/ACL-resync
each reused one fixed, predictable Durable instance ID across every run/tick (derived from
`INGESTION_SOURCE_ID`). Durable instance-ID reuse is documented as best-effort/racy at the
storage layer — confirmed by a Microsoft engineer:
["We cannot reliably silently replace instanceIDs that are in the Pending state ... there exists
a race condition where you have scheduled an orchestrator to be created but that information has
not yet been propagated throughout our storage."](https://github.com/Azure/azure-functions-durable-python/issues/410)
Reusing an ID across separate runs corrupted the replay history, which the Durable Task
Scheduler surfaced as this error.

If you see this on a Function App older than 2026-08-24, redeploy current `app/function_app.py`,
`app/ingestion/models.py`, `app/ingestion/services.py`, and `app/ingestion/lifecycle_repository.py`
first — the fix removes fixed instance IDs entirely (every run/tick gets a fresh, randomly
generated ID; "is it already running" is tracked via Cosmos instead of polling a fixed ID).

### Recovery commands
```powershell
# 1. Terminate the stuck run and force-fail non-terminal docs
Invoke-RestMethod -Uri "$base/api/ingestion/terminate" -Method POST -Headers $h

# 2. Re-run full-sync — it will get a fresh instance ID, not the failed one
Invoke-RestMethod -Uri "$base/api/ingestion/full-sync" -Method POST -Headers $h
```

### Verification
```powershell
Invoke-RestMethod -Uri "$base/api/ingestion/status" -Headers $h | ConvertTo-Json -Depth 5
# Expect: a different "instanceId" than the failed run, and runtimeStatus progressing to Completed
```
