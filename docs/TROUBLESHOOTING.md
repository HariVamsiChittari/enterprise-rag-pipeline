# Troubleshooting

These procedures follow the current Function, ACA, Cosmos lifecycle, and guarded deployment contracts. They describe failures observed in prior deployed environments; they are not all production incidents.

## Establish the Target

Before diagnosis, capture:

- Subscription, tenant, resource group, azd environment, and deployment instance.
- Function name and ACA latest ready revision.
- Full image and catalog digests.
- Exact orchestration ID or query request ID.

Do not mutate resources until the target and artifact identity are unambiguous.

## Authentication Failures

### Function returns 401

Check that the token:

- Uses exactly `FUNCTION_API_AUDIENCE`.
- Comes from an application in `FUNCTION_ALLOWED_CALLER_CLIENT_ID`.
- Represents a user and includes `user_impersonation` for `/api/query`.
- Uses the expected tenant.

A Function master key does not bypass EasyAuth.

### Function query returns `retrieval_auth_failed`

Verify:

- `RETRIEVAL_SERVICE_URL` is the expected ACA private FQDN.
- `RETRIEVAL_SERVICE_SCOPE` targets the retrieval API.
- ACA Authentication allows the Function UAMI client and principal.
- The Function UAMI has `Retrieval.Gateway` on the retrieval API application.
- Retrieval settings contain the matching gateway client/principal IDs and audience.

Do not forward or synthesize a user `X-MS-CLIENT-PRINCIPAL` to ACA. The Function creates `X-RAG-GATEWAY-CONTEXT` and `X-RAG-REQUEST-ID` after validating the delegated user.

## Full Sync Has Failed or Stuck Documents

Inspect the current run and source documents:

```powershell
$rows = (Invoke-RestMethod `
  -Uri "$baseUrl/api/ingestion/inspect?container=source-documents&limit=200" `
  -Headers $headers).rows
$rows | Where-Object status -ne 'ready' |
  Select-Object sourceName, status, stage, attemptCount, error
```

The inspect endpoint is capped at 200 rows. `runId` partition filtering applies only to `source-documents`; omit it for other containers.

If the orchestration must be terminated:

```powershell
Invoke-RestMethod -Uri "$baseUrl/api/ingestion/terminate" -Method Post -Headers $headers
```

Retry failed documents and capture the returned random orchestration ID:

```powershell
$retry = Invoke-RestMethod `
  -Uri "$baseUrl/api/ingestion/retry-failed" `
  -Method Post `
  -Headers $headers

Invoke-RestMethod `
  -Uri "$baseUrl/api/ingestion/status?instanceId=$($retry.orchestrationId)" `
  -Headers $headers
```

Do not construct `retry-failed-<full-sync-id>`; retry IDs use a new UUID.

## Document Intelligence Rejection

`document_intelligence_rejected` is terminal for that document attempt. Verify the source is a valid supported PDF, within size/page limits, and readable through the source connector.

Tier changes or AI-service replacements are infrastructure changes. Update reviewed deployment inputs and use `scripts/deploy.ps1`; do not deploy `ai-services.bicep` directly or delete private endpoints to work around access.

## SharePoint ACL or Site-Group Failures

Verify:

- `SHAREPOINT_SITE_URL` is present, HTTPS, and resolves to the configured drive.
- The ingestion app has the required Graph and SharePoint application permissions and site grant.
- The certificate secret is readable by the Function UAMI.
- SharePoint site groups contain Entra security groups, not only direct users or sharing links.
- Entra groups return `securityEnabled=true`.

An ACL-revoked document can be restored only when its current source eTag matches, no active version exists, and it is the authoritative matching historical version.

## Delta Sync Does Not Advance

- Resolve the random instance ID from the `delta-sync-trigger` control record.
- Inspect the exact orchestration output.
- Any failed delta item retains the previous cursor; a later tick replays the round.
- A `410 Gone` cursor causes reset/re-bootstrap handling.
- A full sync or prior delta run can intentionally suppress a new trigger.

Do not manually replace the cursor unless a separately approved recovery procedure requires it.

## Lifecycle Transition Stuck

The 10-minute lifecycle reconciliation handles:

- `admitting` documents.
- `acl_refreshing` documents.
- `retiring` documents.
- `deleting` documents.
- Orphan chunks.
- Older duplicate ready versions when the current full-sync run supplies the ready winner.

Inspect the manifest status, pending fields, lifecycle generation, expected chunk count, and exact document key before intervening.

## Retrieval Startup or Readiness Fails

Readiness executes a small query against the first configured chunks container. Check:

- Cosmos private DNS/network reachability.
- Retrieval UAMI data-reader assignments on `search-chunks`, `source-documents`, and `retrieval-config`.
- Exact `DEPLOYMENT_INSTANCE_ID` and `RETRIEVAL_CATALOG_DIGEST`.
- Presence and validity of `catalog:<digest>` in the deployment-instance partition.
- Catalog default profile and synonym-map references.

Changing the publication `active` pointer does not change runtime startup selection.

## Query Failures

| Function error code | Meaning |
| --- | --- |
| `unauthorized` | Delegated user claims failed validation |
| `gateway_not_configured` | Retrieval URL or service scope is missing/invalid |
| `retrieval_auth_failed` | ACA/retrieval rejected Function service authentication |
| `retrieval_unavailable` | Retrieval returned a server error |
| `invalid_retrieval_response` | Retrieval returned malformed, oversized, or incompatible JSON |
| `retrieval_request_failed` | Retrieval returned another 4xx response |
| `retrieval_timeout` | Function proxy deadline expired |

Use `request_id` to correlate Function logs, ACA logs, and `service-audit`. The audit container is best-effort and has a 90-day TTL.

## Cosmos Throttling

The repositories retry supported 429 paths. Long-running ingestion can therefore be slow without being incorrect. Check dependency telemetry and orchestration progress before terminating.

Do not disable ACL checks, open public Cosmos access, or use account keys to make a diagnostic pass.

## Safe Data Cleanup

`DELETE /api/ingestion/purge` supports exact item IDs and guarded purge-all for `ingestion-runs`, `source-documents`, and `search-chunks`. It refuses `service-audit`.

Prefer exact IDs. A complete cleanup of one document must account for its manifest partition and all chunk IDs in its `documentKey` partition. The inspect endpoint's 200-row cap cannot prove complete large-container cleanup.

## Deployment Failures

Use the controller's preview first:

```powershell
.\scripts\deploy.ps1 -Phase <phase> @target
```

Common guards:

- Plan or source hash changed: rerun `Authority`, review changes, and obtain approval again.
- Target mismatch: align Azure CLI and azd subscription/location with the reviewed target.
- Mutable image/tag: run `Build` and set the returned digest reference.
- Catalog mismatch: validate the exact catalog and set its returned digest.
- Temporary job remains: run `OperationsCleanup` only after E2E gates and explicit approval.

Do not use direct `az containerapp update`, direct Bicep deployment, or direct Function publishing as recovery shortcuts.
