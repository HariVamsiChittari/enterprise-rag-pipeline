# Production Readiness Plan

## Current State (Dev)

| Resource | Dev Configuration | Prod Target |
|----------|------------------|-------------|
| Cosmos DB | Serverless (5,000 RU/s burst) | Provisioned autoscale (Tmax=20,000 RU/s) |
| OpenAI TPM | 30K TPM | 2M+ TPM (quota increase required) |
| WAVE_SIZE | 4 | 20 |
| Language AI | F0 free tier | S tier |
| Document Intelligence | F0 free tier | S0 standard |
| Storage redundancy | LRS | ZRS |

## Bottleneck Analysis (10K Files, ~378 Chunks/File)

| Resource | At Dev Quota | At Prod Quota | Acceptable? |
|----------|-------------|---------------|-------------|
| OpenAI embedding | 43 days (30K TPM) | ~16 hours (2M TPM) | Yes |
| Cosmos chunk writes | 21 hours + 429 failures | ~2 hours (20K autoscale) | Yes |
| Document Intelligence | ~10 hours | ~10 hours (S0 sufficient) | Yes |
| Language AI enrichment | ~10 hours (batch=5) | ~10 hours (S sufficient) | Yes |
| Total pipeline (parallelized) | Blocked | ~16 hours | Yes |

## Infrastructure Changes Required

### 1. Cosmos DB: Serverless → Provisioned Autoscale

Already supported via Bicep parameter. No code changes needed.

```
# infra/main.parameters.prod.bicepparam (already configured)
param cosmosDbMode = 'provisioned'
```

**Action**: Update `cosmosSearchChunksAutoscaleMaxRUs` from `1000` to `20000` in `main.parameters.prod.bicepparam` before deploying to production at scale.

| Parameter | Current (Prod) | Recommended (10K files) |
|-----------|---------------|-------------------------|
| `cosmosMetadataAutoscaleMaxRUs` | 1000 | 1000 (sufficient) |
| `cosmosSearchChunksAutoscaleMaxRUs` | 1000 | 20000 |

**Cost estimate** (autoscale, idle at 10%):
- Idle: 2,000 RU/s × $0.008/100 RU/hr ≈ $116/month
- During ingestion burst: 20,000 RU/s × $0.008/100 RU/hr = $16/hr

### 2. OpenAI Quota: 30K → 2M+ TPM

**Action**: Request quota increase via Azure Portal → Azure OpenAI → Quotas → `text-embedding-3-large`.

This is not automatable via IaC. Must be done manually per subscription.

Formula for WAVE_SIZE: `WAVE_SIZE ≈ TPM / 200,000` (leave headroom for retries).

| TPM Granted | Recommended WAVE_SIZE | Full Sync Time (10K files) |
|-------------|----------------------|---------------------------|
| 300K | 4 | ~4.4 days |
| 1M | 5-8 | ~31 hours |
| 2M | 10-15 | ~16 hours |
| 5M | 20-30 | ~6 hours |

### 3. WAVE_SIZE: 4 → 20 (Environment Variable)

Already reads from environment:

```python
# function_app.py line 23
WAVE_SIZE = int(os.getenv("WAVE_SIZE", "4"))
```

**Action**: Set app setting after OpenAI quota is confirmed:

```bash
az functionapp config appsettings set \
  --name <func-app-name> \
  --resource-group <rg-name> \
  --settings "WAVE_SIZE=20"
```

## Code Fixes Already Deployed

| Fix | File | Purpose |
|-----|------|---------|
| Cosmos 429 retry with exponential backoff | `ingestion/repository.py` | `_create_chunk_batch` → `_execute_batch_with_throttle_retry` (5 retries, 1s×2^n backoff) |
| Language AI batch size 25→5 | `ingestion/enrichment.py` | Complies with entity API limit (max 5 per request) |
| Error logging before SafeError | `ingestion/services.py` | `logger.error(...)` with `exc_info=True` for App Insights |

## Security Groups (100+ Users)

No code changes needed. The existing design handles this:

- `read_verified_acl()` stores `allowed_group_ids` per document and chunk
- Search queries filter via `ARRAY_CONTAINS(c.allowedGroupIds, <user_group_id>)`
- Microsoft Graph rate limit (10K req/10 min per app) handles 10K ACL checks

## Production Security Hardening

### EasyAuth: Restrict Allowed Applications

In production, add the front-end client ID to `allowedApplications` so only your approved app can call the API:

```bash
az rest --method PUT \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/sites/{func-app}/config/authsettingsV2?api-version=2022-03-01" \
  --body '{
    "properties": {
      "identityProviders": {
        "azureActiveDirectory": {
          "validation": {
            "allowedAudiences": ["api://{client-id}"],
            "defaultAuthorizationPolicy": {
              "allowedApplications": ["{frontend-client-id}"]
            }
          }
        }
      }
    }
  }'
```

Per [MS Learn](https://learn.microsoft.com/azure/app-service/configure-authentication-provider-aad#authorize-requests): only tokens from the named client are accepted.

### Per-User, Per-Replica Rate Limiting

The retrieval service enforces a sliding-window rate limiter (`RATE_LIMIT_RPM`, default 30 requests/minute per user **per replica**). Returns HTTP 429 when exceeded. **Effective ceiling** ≈ `RATE_LIMIT_RPM × replicaCount` — at `maxReplicas=5`, a single user can reach ~150 RPM before any replica rejects. This is **not a hard DoS control** at scale-out; enforce upstream via Azure Front Door + WAF rate-limit rules, Azure API Management, or replace the in-memory limiter with a shared counter (Redis / Cosmos atomic increment).

### Thread Safety

Concurrent retrieval tasks use `threading.Lock` for usage-record collection, ensuring audit data integrity under parallel load.

### Container Security (AKS)

Pod Security Standards (Restricted): `runAsNonRoot`, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `capabilities: drop ALL`, `seccompProfile: RuntimeDefault`. Writable `/tmp` via `emptyDir` (64Mi).

### OpenAI Resilience

Both sync and async OpenAI clients set `max_retries=2` for automatic exponential backoff on transient 429/5xx errors.

## Deployment Checklist

```
[ ] 1. Request OpenAI quota increase (2M+ TPM) via Azure Portal
[ ] 2. Wait for quota approval
[ ] 3. Update prod params: cosmosSearchChunksAutoscaleMaxRUs = 20000
[ ] 4. Deploy infrastructure: azd provision --environment prod
[ ] 5. Deploy code: azd deploy --environment prod
[ ] 6. Build and deploy retrieval image: az acr build + az containerapp update
[ ] 7. Set WAVE_SIZE app setting (match to granted TPM)
[ ] 8. Set RETRIEVAL_SERVICE_URL on Function App (HTTPS, internal ACA FQDN)
[ ] 9. Set WEBHOOK_CLIENT_STATE and FUNCTION_PUBLIC_BASE_URL on Function App
[ ] 10. Set SHAREPOINT_SITE_URL if site uses site groups for permissions
[ ] 11. Run full-sync test with 24 files → expect 21+ success
[ ] 12. Verify webhook subscription created (check logs for subscription_created)
[ ] 13. Verify reconciliation_timer fires daily at 04:00 UTC (safety-net delta query)
[ ] 14. Verify acl_resync_timer fires weekly Sunday at 03:00 UTC
[ ] 15. Test retrieval query via /api/query endpoint
[ ] 16. Verify service-audit container has both ingestion + retrieval records
[ ] 17. Monitor Cosmos 429 metrics (should be near zero)
[ ] 18. Monitor OpenAI 429 metrics (should be within retry budget)
```

## Orchestration Batching Strategy (continue_as_new)

### Decision: Not Required — Durable Task Scheduler Handles It

The project uses **Durable Task Scheduler** (not Azure Storage provider). DTS manages orchestration history caching, partitioning, and replay internally. The replay overhead that motivates `continue_as_new` batching is primarily an Azure Storage provider concern.

**Evidence**: [MS Learn – Performance and Scale](https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-perf-and-scale) states: "The Durable Task Scheduler manages caching internally."

### Why Durable Task Scheduler Over Azure Storage Provider

Both are GA and supported on Flex Consumption. DTS was chosen because:

| Criteria | DTS (chosen) | Azure Storage (alternative) |
|----------|-------------|---------------------------|
| Throughput | Very high | Moderate |
| Max orchestration nodes | Unlimited | 16 partitions |
| At 10K activities | Designed for this | Queue latency + partition limits |
| Observability | Built-in dashboard | Manual (App Insights only) |
| Cost | Consumption SKU (free tier) | ~$0.0004/10K storage transactions |
| Local dev tooling | Docker emulator | Azurite (simpler, no Docker) |

Azure Storage provider **would work** for dev scale (23 files) but becomes a throughput bottleneck at production scale (10K+ activity invocations). Microsoft explicitly recommends DTS for new projects.

**Reference**: [Storage Providers Comparison – MS Learn](https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-storage-providers)

### Scale Thresholds

| File Count | Approach | Rationale |
|-----------|----------|-----------|
| ≤500 | Current architecture, no changes | DTS handles replay efficiently |
| 500–5K | Monitor replay time via App Insights. Add `continue_as_new` if replays >5s | Defensive validation |
| 5K–10K | Implement `continue_as_new` with BATCH_SIZE=500 if needed | ~30 lines in orchestrator |
| >10K | Sub-orchestrations by folder/source | Fan-in limited to single VM per MS docs |

### If Batching Becomes Necessary

The implementation is a ~30-line change in `function_app.py` using the standard Eternal Orchestration pattern:

1. First iteration: activate + discover ALL + process first BATCH_SIZE docs
2. Call `context.continue_as_new(state)` with remaining docs and cumulative counters
3. Subsequent iterations: process next BATCH_SIZE docs, repeat
4. Final iteration: process remaining + finalize

Two env vars would control throughput:
- `WAVE_SIZE` (default 4) = concurrent docs per wave
- `BATCH_SIZE` (default 200) = docs per orchestration iteration before history reset

**Reference**: [Eternal Orchestrations – MS Learn](https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-eternal-orchestrations)

### Validation Step Before Implementing

Run 1K+ file ingestion and check:
```bash
# Query App Insights for orchestrator replay duration
traces | where message contains "full_sync_orchestrator" and message contains "IsReplay: True"
| summarize avg(duration), max(duration) by bin(timestamp, 5m)
```

If `max(duration) > 5000ms`, implement `continue_as_new` batching.

## Incremental Ingestion (Delta-Sync) — Implemented

Delta-sync is primarily triggered by **Microsoft Graph webhooks** — when a file is added, modified, or deleted in SharePoint, Graph sends a change notification to `POST /api/webhook/sharepoint`, which starts the `delta_sync_orchestrator`. A daily **reconciliation timer** (`DELTA_SYNC_SCHEDULE`, default 04:00 UTC) runs the same delta query as a safety net to catch any missed webhook notifications.

The delta query uses the Microsoft Graph Delta API to detect changed files. Deleted files are hard-deleted from Cosmos (source-documents + search-chunks). Updated files are re-processed, and the old version is retired (`status=retired`, `retired_reason=superseded`). The delta cursor is stored in a dedicated `delta-control` item in `ingestion-runs`.

When the delta feed returns zero content changes (`itemsSeen == 0`), the orchestrator automatically runs one page of ACL resync to catch permission-only changes that Graph does not surface via `@microsoft.graph.sharedChanged`.

ACL resync runs weekly on Sunday at 03:00 UTC (`ACL_RESYNC_SCHEDULE`). It re-verifies document permissions via Graph and the SharePoint REST API (for site group resolution), then patches `allowedGroupIds` on both source-documents and search-chunks when ACLs change. Documents with revoked access are retired (`retired_reason=acl_revoked`).

All timers and webhook handlers skip execution while a full-sync orchestration is running.

## Hybrid RAG Retrieval — Implemented

The retrieval service uses hybrid routing based on LLM query planning:

| Configuration | Env Var | Default | Description |
|---|---|---|---|
| Agent timeout | `AGENT_TIMEOUT_SECONDS` | `8.0` | Max time for agentic path before fallback |
| Agent max iterations | `AGENT_MAX_ITERATIONS` | `5` | Max LLM reasoning roundtrips per request |

### Operational Notes

- All queries are analyzed by the LLM planner regardless of conversation history
- Simple queries (1 planned query) use the standard path (~5s, lower token cost)
- Complex queries (2+ planned queries) use the Agent Framework agentic path (~8-10s)
- Agentic path timeout/error triggers automatic fallback to standard path
- Structured logs include `path=standard|agentic|agentic_fallback` for monitoring
- Agent Framework package (`agent-framework-core`, `agent-framework-openai`) must be installed; if unavailable, all queries use standard path

## Dev vs. Prod Parameter Files

| File | Purpose |
|------|---------|
| `infra/main.parameters.dev.bicepparam` | Serverless, free tiers, LRS, WAVE_SIZE=4 |
| `infra/main.parameters.prod.bicepparam` | Provisioned autoscale, standard tiers, ZRS, WAVE_SIZE=20 |
