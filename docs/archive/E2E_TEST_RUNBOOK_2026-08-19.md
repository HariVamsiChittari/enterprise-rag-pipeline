# E2E Test Runbook — Full Sync, Delta-Sync, Webhooks, ACL & Retrieval

Executed 2026-08-19 against a non-production Function App in eastus2. Concrete deployment identifiers have been removed from this archived record.

## Variables

```powershell
$funcApp   = "<function-app-name>"
$base      = "https://$funcApp.azurewebsites.net"
$masterKey = az functionapp keys list --name $funcApp --resource-group <resource-group> --query "masterKey" -o tsv
$token     = az account get-access-token --resource "api://<function-api-client-id>" --query accessToken -o tsv
$authH     = @{Authorization = "Bearer $token"}
```

---

## F1: Full Sync — Ingest All Documents

```powershell
# Trigger full-sync
Invoke-WebRequest -Uri "$base/api/ingestion/full-sync" -Method POST -Headers $authH

# Monitor progress (poll every 30s)
do {
  Start-Sleep 30
  $r = Invoke-RestMethod -Uri "$base/api/ingestion/status" -Headers $authH
  Write-Host "Status: $($r.runtimeStatus)"
} while ($r.runtimeStatus -match "Running|Pending")

# View result
$r.output | ConvertTo-Json
```

### Result: PASS

```json
{
  "status": "completed",
  "discovered": 22,
  "succeeded": 22,
  "failed": 0,
  "runStatus": "completed"
}
```

**Verify documents in Cosmos:**

```powershell
$r = Invoke-RestMethod -Uri "$base/api/ingestion/inspect?container=source-documents&limit=50" -Headers $authH
$ready = $r.rows | Where-Object { $_.status -eq "ready" }
Write-Host "Ready documents: $($ready.Count)"
$ready | ForEach-Object { "$($_.sourceName) | groups=$($_.allowedGroupIds -join ', ')" }
```

**Verify search chunks:**

```powershell
$r = Invoke-RestMethod -Uri "$base/api/ingestion/inspect?container=search-chunks&limit=5" -Headers $authH
Write-Host "Total chunks: $($r.count)"
```

---

## S1: Verify Webhook Subscription Exists

```powershell
$r = Invoke-RestMethod -Uri "$base/api/ingestion/inspect?container=ingestion-runs&limit=50" -Headers $authH
$r.rows | Where-Object { $_.id -eq "webhook-subscription" } | ConvertTo-Json -Depth 3
```

### Result: PASS

```json
{
  "id": "webhook-subscription",
  "sourceId": "sharepoint-drive",
  "subscriptionId": "<graph-subscription-id>",
  "updatedAt": "2026-08-18T15:12:41Z"
}
```

---

## W1: Webhook Validation Handshake

```powershell
Invoke-WebRequest `
  -Uri "$base/api/webhook/sharepoint?validationToken=e2e-test-token-123" `
  -Method POST -ContentType "text/plain" -SkipHttpErrorCheck `
  | Select-Object StatusCode, Content
```

### Result: PASS

```text
StatusCode Content
---------- -------
       200 e2e-test-token-123
```

---

## D1: Verify Delta Cursor Exists

```powershell
$r = Invoke-RestMethod -Uri "$base/api/ingestion/inspect?container=ingestion-runs&limit=50" -Headers $authH
$r.rows | Where-Object { $_.id -eq "delta-control" } | ConvertTo-Json -Depth 3
```

**Result: PASS** — `delta-control` record with Graph delta token present.

---

## D2: New File Added → Ingested via Delta-Sync

**SharePoint action:** Upload `sample doc car - V1.pdf` (renamed from `sample doc car - Copy.pdf`).

**Verification:**

```powershell
# Check delta-sync status (webhook triggers automatically)
Invoke-RestMethod -Uri "$base/api/ingestion/status?instanceId=delta-sync-sharepoint-drive" `
  -Headers $authH | ConvertTo-Json -Depth 3
```

### Result: PASS

```json
{
  "status": "completed",
  "bootstrapped": false,
  "createdOrUpdated": 1,
  "deleted": 0,
  "aclResynced": 0,
  "failed": 0,
  "itemsSeen": 2
}
```

**Verify document in Cosmos:**

```powershell
$r = Invoke-RestMethod -Uri "$base/api/ingestion/inspect?container=source-documents&limit=50" -Headers $authH
$r.rows | Where-Object { $_.sourceName -like "*V1*" } | ConvertTo-Json -Depth 4
```

**Key fields:**

```text
sourceName:        sample doc car - V1.pdf
status:            ready
stage:             terminal
ingestionMode:     delta-sync
pageCount:         2
writtenChunkCount: 12
allowedGroupIds:   <security-group-id-1>, <security-group-id-2>
```

---

## D3: File Updated → Re-Ingested

**SharePoint action:** Re-upload `sample doc car - Copy.pdf` with different content.

### Result: PASS

```json
{
  "status": "completed",
  "bootstrapped": false,
  "createdOrUpdated": 1,
  "deleted": 0,
  "aclResynced": 0,
  "failed": 0,
  "itemsSeen": 2
}
```

**Verify both versions:**

```powershell
$r = Invoke-RestMethod -Uri "$base/api/ingestion/inspect?container=source-documents&limit=50" -Headers $authH
$r.rows | Where-Object { $_.sourceName -like "*car*" -or $_.sourceName -like "*copy*" } `
  | ForEach-Object { "$($_.sourceName) | status=$($_.status) | mode=$($_.ingestionMode) | retired=$($_.retiredReason)" }
```

```text
sample doc car - V1.pdf  | status=retired | mode=delta-sync | retired=deleted
sample doc car - Copy.pdf | status=ready   | mode=delta-sync | retired=
```

---

## D4: File Deleted → Document Retired

**SharePoint action:** Delete `sample doc car - V1.pdf` from the library.

**Verification (check document record):**

```powershell
$r = Invoke-RestMethod -Uri "$base/api/ingestion/inspect?container=source-documents&limit=50" -Headers $authH
$r.rows | Where-Object { $_.sourceName -like "*V1*" } `
  | Select-Object sourceName, status, stage, retiredAt, retiredReason | ConvertTo-Json
```

### Result: PASS

```json
{
  "sourceName": "sample doc car - V1.pdf",
  "status": "retired",
  "stage": "terminal",
  "retiredAt": "2026-08-19T12:00:04Z",
  "retiredReason": "deleted"
}
```

**Confirmed via App Insights:**

```powershell
az monitor app-insights query --app <application-insights-name> --resource-group <resource-group> `
  --analytics-query "traces | where timestamp between(datetime('2026-08-19T11:59:00Z') .. datetime('2026-08-19T12:01:00Z')) and message contains 'delta_sync_completed' | project timestamp, message"
```

```text
12:00:05Z: delta_sync_completed bootstrapped=False created_or_updated=0 deleted=1 acl_resynced=0 failed=0 items_seen=2
```

> **Note:** The deletion is a soft delete — the document record stays in Cosmos with `status=retired`. Chunks remain in `search-chunks` and are excluded at query time by ACL/status filters.

---

## D5: Permission Change → ACL Resynced

**SharePoint action:** Remove Entra SG `<revoked-security-group-id>` from the library's direct permissions.

**Trigger (ACL resync timer — safety-net that queries permissions directly):**

```powershell
Invoke-WebRequest `
  -Uri "$base/admin/functions/acl_resync_timer" `
  -Method POST `
  -Headers @{"x-functions-key" = $masterKey; "Content-Type" = "application/json"} `
  -Body '{}' -SkipHttpErrorCheck | Select-Object StatusCode
```

**Check result:**

```powershell
Invoke-RestMethod -Uri "$base/api/ingestion/status?instanceId=acl-resync-sharepoint-drive" `
  -Headers $authH | ConvertTo-Json -Depth 3
```

### Result: PASS

```json
{
  "status": "completed",
  "checked": 21,
  "updated": 21,
  "retired": 0
}
```

**Verify ACL on document:**

```powershell
$r = Invoke-RestMethod -Uri "$base/api/ingestion/inspect?container=source-documents&limit=50" -Headers $authH
$doc = $r.rows | Where-Object { $_.sourceName -like "*Copy*" -and $_.status -eq "ready" }
Write-Host "ACL groups: $($doc.allowedGroupIds -join ', ')"
Write-Host "ACL hash:   $($doc.aclHash)"
```

```text
Before: ACL groups: <remaining-security-group-id>, <revoked-security-group-id>
After:  ACL groups: <remaining-security-group-id>
```

> **Note:** Both **direct Entra SG grants** and **Entra SGs nested inside SharePoint site groups** are tracked. Site group members are resolved via the SharePoint REST API (`/_api/web/sitegroups({id})/users`) when `SHAREPOINT_SITE_URL` is configured. Permission-only changes (no content change) are caught by the auto ACL resync that runs whenever a webhook fires with zero content changes. The `acl_resync_timer` (weekly Sunday 03:00 UTC) is a secondary safety net for edge cases.

---

## D7/D8: Non-PDF File → Skipped

**SharePoint action:** Upload `sample doc.docx` to the library.

### Result: PASS

```json
{
  "status": "completed",
  "bootstrapped": false,
  "createdOrUpdated": 0,
  "deleted": 0,
  "aclResynced": 0,
  "failed": 0,
  "itemsSeen": 2
}
```

`itemsSeen: 2` (the .docx + parent) but `createdOrUpdated: 0` — file was seen and skipped because `.docx` is not in `allowed_extensions`. No document record created in `source-documents`.

---

## W3: Invalid clientState → 403

```powershell
Invoke-WebRequest `
  -Uri "$base/api/webhook/sharepoint" `
  -Method POST -ContentType "application/json" `
  -Body '{"value":[{"clientState":"WRONG","resource":"d/x","changeType":"updated"}]}' `
  -SkipHttpErrorCheck | Select-Object StatusCode
```

### Result: PASS

```text
StatusCode
----------
       403
```

---

## W4: Duplicate Webhook → Idempotent

```powershell
$cs = az functionapp config appsettings list --name $funcApp --resource-group <resource-group> `
  --query "[?name=='WEBHOOK_CLIENT_STATE'].value" -o tsv
$body = '{"value":[{"clientState":"' + $cs + '","resource":"drives/b!.../root","changeType":"updated"}]}'

# Send two valid webhooks rapidly
$r1 = Invoke-WebRequest -Uri "$base/api/webhook/sharepoint" -Method POST -ContentType "application/json" -Body $body -SkipHttpErrorCheck
$r2 = Invoke-WebRequest -Uri "$base/api/webhook/sharepoint" -Method POST -ContentType "application/json" -Body $body -SkipHttpErrorCheck
Write-Host "First: $($r1.StatusCode), Second: $($r2.StatusCode)"
```

### Result: PASS

```text
First: 200, Second: 200
```

**App Insights confirmed:** `webhook_sharepoint: delta-sync already running` — second webhook accepted but did not start a duplicate orchestration.

---

## S2: Subscription Renewal

```powershell
Invoke-WebRequest `
  -Uri "$base/admin/functions/subscription_renew_timer" `
  -Method POST `
  -Headers @{"x-functions-key" = $masterKey; "Content-Type" = "application/json"} `
  -Body '{}' -SkipHttpErrorCheck | Select-Object StatusCode
```

### Result: PASS

**App Insights confirmed:**

```text
subscription_renewed: <graph-subscription-id> expires 2026-09-16T23:16:57Z
```

---

## R1: Reconciliation Timer (Safety-Net Delta-Sync)

```powershell
Invoke-WebRequest `
  -Uri "$base/admin/functions/reconciliation_timer" `
  -Method POST `
  -Headers @{"x-functions-key" = $masterKey; "Content-Type" = "application/json"} `
  -Body '{}' -SkipHttpErrorCheck | Select-Object StatusCode

# Wait 15s, then check
Start-Sleep -Seconds 15
Invoke-RestMethod -Uri "$base/api/ingestion/status?instanceId=delta-sync-sharepoint-drive" `
  -Headers $authH | ConvertTo-Json -Depth 3
```

### Result: PASS

```json
{
  "status": "completed",
  "bootstrapped": false,
  "createdOrUpdated": 0,
  "deleted": 0,
  "aclResynced": 0,
  "failed": 0,
  "itemsSeen": 0
}
```

No pending changes missed by webhooks.

---

## Bugs Found and Fixed During Testing

### Bug 1: Delta cursor permanently invalidated (triple-410)

**Symptom:** Delta-sync failed with `graph_delta_reset_required`.

**Root cause:** Graph returned 410 on the stored cursor AND on both reset-location URLs. The code only handled one level of 410 reset.

**Fix 1 — Double-410 handler** ([graph.py#L448-L458](../../app/ingestion/graph.py)):

```python
except DeltaResetRequired as reset:
    if not allow_reset:
        raise
    try:
        values, final_delta_link = read_json_pages(client, reset.location, ...)
    except DeltaResetRequired as second_reset:
        values, final_delta_link = read_json_pages(client, second_reset.location, ...)
    is_reset = True
```

**Fix 2 — Catch-all re-bootstrap** ([services.py#L371-L376](../../app/ingestion/services.py)):

```python
try:
    delta = connector.read_drive_delta(config.delta_max_pages, delta_link=cursor)
except DeltaResetRequired:
    logger.warning("delta_cursor_invalidated, re-bootstrapping")
    bootstrap_link = connector.bootstrap_delta_cursor()
    lifecycle_repository.save_delta_cursor(config.source_id, bootstrap_link)
    return DeltaSyncOutcome(bootstrapped=True)
```

### Bug 2: Bootstrap cursor incompatible with delta read

**Symptom:** Every delta-sync re-bootstrapped because Graph returned 410 on freshly-bootstrapped cursors.

**Root cause:** `bootstrap_delta_cursor()` did not send `Prefer: deltashowremovedasdeleted, deltatraversepermissiongaps, deltashowsharingchanges` headers. The token obtained without these headers was incompatible with delta reads that included them.

**Fix** ([graph.py#L516](../../app/ingestion/graph.py)):

```python
# Before:
response = client.get(url)

# After:
response = client.get(url, headers={"Prefer": DELTA_PREFER})
```

### Unit test added

```python
# tests/ingestion/test_graph_delta.py
def test_read_drive_delta_handles_double_410_reset():
    """Graph can return 410 on the reset URL itself during server-side transitions."""
```

All 5 delta tests pass: `python -m pytest tests/ingestion/test_graph_delta.py -v`

---

## Summary

| # | Test | Result | Key Evidence |
|---|---|---|---|
| S1 | Subscription exists | PASS | Subscription record present |
| W1 | Validation handshake | PASS | 200 + echo |
| D1 | Delta cursor exists | PASS | `delta-control` record |
| D2 | New file → ingested | PASS | 12 chunks, status=ready |
| D3 | File updated → re-ingested | PASS | New version ready, old superseded |
| D4 | File deleted → retired | PASS | status=retired, retiredReason=deleted |
| D5 | Permission removed → ACL updated | PASS | Target group removed, 21 docs updated |
| D7/D8 | Non-PDF → skipped | PASS | itemsSeen=2, createdOrUpdated=0 |
| W3 | Invalid clientState | PASS | 403 |
| W4 | Duplicate webhook | PASS | "delta-sync already running" |
| S2 | Subscription renewal | PASS | Renewed to 2026-09-16 |
| R1 | Reconciliation timer | PASS | Runs cleanly, 0 pending |
