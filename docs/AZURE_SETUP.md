# Azure Setup Guide

This guide covers the active Azure Container Apps deployment. `scripts/deploy.ps1` is the deployment authority. Preview is the default; Azure mutations require `-Execute`, reviewed plan/source hashes, and an exact target.

## Prerequisites

Install and authenticate:

- Python 3.12.
- Azure CLI with Bicep support.
- Azure Developer CLI (`azd`).
- PowerShell 7.
- Docker is optional because ACR builds the retrieval image remotely.

Verify the selected account before any deployment:

```powershell
az account show --query '{subscription:id,tenant:tenantId}' -o json
azd env list
```

The resource group must already exist. The deployment controller will not create or delete it. The existing Azure OpenAI account and certificate Key Vault resource groups must be in the same subscription selected for deployment because the active Bicep modules scope them through that subscription.

## External Prerequisites

The repository does not create these resources or directory objects:

1. A SharePoint site and document library.
2. An Azure OpenAI account with chat and `text-embedding-3-large` deployments.
3. A Key Vault containing the exportable SharePoint application PFX as a secret.
4. A SharePoint ingestion application registration.
5. A Function API application registration.
6. A retrieval API application registration.

### SharePoint Ingestion Application

The deployed connector uses certificate-based application authentication. Grant and consent only the permissions required by the connector:

- Microsoft Graph application permissions: `Sites.Selected`, `Sites.Read.All`, `GroupMember.Read.All`, and `User.Read.All`.
- SharePoint application permission: `Sites.Read.All` for site-group membership expansion.
- The required site-level grant for the target SharePoint site.

See the current [Microsoft Graph permissions reference](https://learn.microsoft.com/graph/permissions-reference) and [Selected permissions overview](https://learn.microsoft.com/graph/permissions-selected-overview).

`SHAREPOINT_SITE_URL` and `SHAREPOINT_ASSIGNED_DRIVE_ID` are both required. At runtime, the Function resolves the site and verifies that its Graph `/drives` relationship contains the configured document-library drive before SharePoint REST group expansion is enabled.

### Function API Application

- Configure a single-tenant application ID URI.
- Expose delegated scope `user_impersonation`.
- Record the client ID as `ADMIN_API_CLIENT_ID`.
- Record the exact API audience as `FUNCTION_API_AUDIENCE`.
- Record the approved client application as `FUNCTION_ALLOWED_CALLER_CLIENT_ID`.

Function EasyAuth requires authentication for all paths except `/api/webhook/*`, accepts exactly `FUNCTION_API_AUDIENCE`, and requires the configured caller application.

### Retrieval API Application

- Configure a single-tenant application.
- Expose application role `Retrieval.Gateway`.
- Assign that role to the Function UAMI service principal after the Foundation phase creates it.
- Record the client ID/audience and service-principal object ID as the `RETRIEVAL_API_*` values.
- Grant the retrieval UAMI the Microsoft Graph application permissions required for transitive group resolution.

ACA Authentication and retrieval application code both restrict access to the Function UAMI application and principal. The Function sends a managed-identity service token plus `X-RAG-GATEWAY-CONTEXT` and `X-RAG-REQUEST-ID`; it does not forward the user's EasyAuth principal header.

### Existing Key Vault

The certificate Key Vault is externally supplied. Use an approved private-connected administrative path to upload or renew the PFX. Do not delete its private endpoint or temporarily enable public access as part of this workflow.

The Bicep deployment creates a private endpoint/private DNS integration and assigns Key Vault Secrets User to the Function UAMI. It does not alter the existing vault's public-access policy.

## Configure the azd Environment

See the complete [environment variable reference](CONFIGURATION.md) for ownership, defaults, accepted values, generated settings, and secrets.

Create or select the environment:

```powershell
azd env new <environment-name>
azd env select <environment-name>
azd env set AZURE_SUBSCRIPTION_ID <subscription-id>
azd env set AZURE_LOCATION <region>
```

Set the required source and external-resource values:

```powershell
azd env set AZURE_OPENAI_ACCOUNT_NAME <openai-account>
azd env set AZURE_OPENAI_RESOURCE_GROUP <openai-resource-group>
azd env set OPENAI_CHAT_DEPLOYMENT_NAME <chat-deployment>
azd env set OPENAI_EMBEDDING_DEPLOYMENT_NAME text-embedding-3-large

azd env set SHAREPOINT_TENANT_ID <tenant-id>
azd env set SHAREPOINT_APP_CLIENT_ID <ingestion-app-client-id>
azd env set SHAREPOINT_ASSIGNED_DRIVE_ID <drive-id>
azd env set SHAREPOINT_SITE_URL https://<tenant>.sharepoint.com/sites/<site>
azd env set SHAREPOINT_KEY_VAULT_NAME <vault-name>
azd env set SHAREPOINT_KEY_VAULT_RESOURCE_GROUP <vault-resource-group>
azd env set SHAREPOINT_CERTIFICATE_SECRET_NAME sharepoint-app-cert
azd env set INGESTION_SOURCE_ID <stable-source-id>

azd env set ADMIN_API_CLIENT_ID <function-api-client-id>
azd env set FUNCTION_API_AUDIENCE api://<function-api-client-id>
azd env set FUNCTION_ALLOWED_CALLER_CLIENT_ID <approved-client-id>
azd env set RETRIEVAL_API_CLIENT_ID <retrieval-api-client-id>
azd env set RETRIEVAL_API_AUDIENCE api://<retrieval-api-client-id>
azd env set RETRIEVAL_API_SERVICE_PRINCIPAL_ID <retrieval-api-sp-object-id>

azd env set WEBHOOK_CLIENT_STATE <random-secret>
azd env set COST_CENTER <cost-center>
azd env set CLEANUP_DATE <yyyy-mm-dd>
```

Do not print `WEBHOOK_CLIENT_STATE` in logs or reports.

Optional capacity/reliability settings:

```powershell
azd env set COSMOS_DB_MODE serverless
azd env set COSMOS_METADATA_AUTOSCALE_MAX_RUS 1000
azd env set COSMOS_SEARCH_AUTOSCALE_MAX_RUS 1000
azd env set STORAGE_REDUNDANCY ZRS
azd env set APPLICATION_INSIGHTS_DAILY_CAP_GB 5
azd env set RETRIEVAL_MIN_REPLICAS 1
azd env set RETRIEVAL_MAX_REPLICAS 5
azd env set RETRIEVAL_ZONE_REDUNDANT false
```

`infra/main.parameters.bicepparam` is the single parameter file. The retired dev/prod parameter files are not deployment inputs.

## Local Validation

```powershell
python -m venv .venv-py312
.\.venv-py312\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest
az bicep build --file infra/main.bicep
az bicep build-params --file infra/main.parameters.bicepparam
```

If the environment cannot compile the retained inactive AKS module, report that separately; do not treat AKS as part of the active serving deployment.

## Guarded Deployment

Every phase accepts the same reviewed target arguments. Obtain the current authority first:

```powershell
$authority = .\scripts\deploy.ps1 -Phase Authority | ConvertFrom-Json

$target = @{
  ExpectedPlanHash       = $authority.planHash
  ExpectedSourceTreeHash = $authority.sourceTreeHash
  SubscriptionId         = '<subscription-id>'
  TenantId               = '<tenant-id>'
  ResourceGroup          = '<existing-resource-group>'
  Location               = '<region>'
  AzdEnvironment         = '<environment-name>'
  DeploymentInstanceId   = '<deployment-instance-id>'
}
```

Do not edit tracked source after capturing the hashes. Re-run `Authority` if the source tree changes.

### 1. Foundation

Preview, review, then execute:

```powershell
.\scripts\deploy.ps1 -Phase Foundation @target
.\scripts\deploy.ps1 -Phase Foundation @target -Execute
```

Foundation creates shared infrastructure and the three UAMIs without creating the Function, retrieval app, or catalog job.

Complete the external Entra role assignments that depend on the newly created UAMI principal IDs before serving deployment.

### 2. Build the Retrieval Image

Set a reviewed release build ID and ACR name, then preview and execute:

```powershell
azd env set RELEASE_BUILD_ID <release-id>
azd env set ACR_NAME <acr-name>

.\scripts\deploy.ps1 -Phase Build @target
$build = .\scripts\deploy.ps1 -Phase Build @target -Execute | ConvertFrom-Json
azd env set RETRIEVAL_IMAGE_REFERENCE $build.imageReference
```

The executed phase resolves the immutable registry digest. Do not use mutable tags as serving inputs.

### 3. Validate the Catalog

Review the complete [catalog property reference](API_REFERENCE.md#catalog-property-reference) before changing client-specific scoring, freshness, or synonym values. Keep the authoring JSON under review; do not write arbitrary items directly to Cosmos.

```powershell
$catalog = python tools/publish_retrieval_catalog.py validate `
  --file app/retrieval/catalog.example.json `
  --deployment-instance-id $target.DeploymentInstanceId | ConvertFrom-Json
azd env set RETRIEVAL_CATALOG_DIGEST $catalog.catalogDigest
```

### 4. Publish and Verify the Catalog

```powershell
.\scripts\deploy.ps1 -Phase Operations @target
.\scripts\deploy.ps1 -Phase Operations @target -Execute

.\scripts\deploy.ps1 -Phase Catalog @target
$publication = .\scripts\deploy.ps1 -Phase Catalog @target -Execute | ConvertFrom-Json
.\scripts\deploy.ps1 -Phase CatalogVerify @target -JobExecutionName $publication.executionName
```

The temporary job runs inside the private ACA environment with a dedicated operations UAMI. The existing rationale and consequences are recorded in the [private catalog publication decision](decisions/0001-private-catalog-publication.md).

### 5. Deploy Serving Resources

```powershell
.\scripts\deploy.ps1 -Phase Final @target
.\scripts\deploy.ps1 -Phase Final @target -Execute

.\scripts\deploy.ps1 -Phase Function @target
.\scripts\deploy.ps1 -Phase Function @target -Execute
```

`Final` creates the Function and ACA resources with the immutable image and catalog digests. `Function` delegates only the Function package deployment to `azd`.

### 6. Validate End to End

Before cleanup, verify:

- Function and ACA health.
- Deployed ACA revision and immutable image digest.
- Exact catalog digest and deployment instance.
- EasyAuth and ACA authentication settings.
- Full-sync/delta/ACL flows and retrieval authorization.
- Standard and agentic paths across required retrieval modes.
- Scoring profiles, freshness, and synonyms.
- Application Insights and Cosmos audit evidence.
- Temporary fixture deletion and data cleanup.

Use [DEMO_RUNBOOK.md](DEMO_RUNBOOK.md) for commands and [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) for release gates.

### 7. Remove the Temporary Catalog Job

After all E2E gates pass, preview the exact deletion and obtain approval:

```powershell
.\scripts\deploy.ps1 -Phase OperationsCleanup @target
.\scripts\deploy.ps1 -Phase OperationsCleanup @target -Execute
```

The controller deletes the uniquely tagged job and verifies that no job remains for the deployment instance.

## Initial Ingestion

Acquire a delegated Function API token and start full sync:

```powershell
$clientId = '<function-api-client-id>'
$baseUrl = 'https://<function-app>.azurewebsites.net'
$token = az account get-access-token --resource "api://$clientId" --query accessToken -o tsv
$headers = @{ Authorization = "Bearer $token"; 'Content-Type' = 'application/json' }

$start = Invoke-RestMethod -Uri "$baseUrl/api/ingestion/full-sync" -Method Post -Headers $headers
Invoke-RestMethod -Uri $start.statusQueryGetUri -Headers $headers
```

Use the returned orchestration identifiers and status URLs; periodic orchestration IDs are random and persisted in Cosmos controls.

## Local Development

Local Function execution does not reproduce EasyAuth or the managed-identity gateway automatically. Unit and component tests are the supported local validation path. Live query and SharePoint integration tests should target an approved deployed environment.

## Recovery Boundaries

- Do not clear all Cosmos containers as a setup shortcut.
- Use the bounded `/api/ingestion/purge` contract only for explicitly approved item IDs or a separately approved purge-all operation.
- Do not update ACA directly with `az containerapp update`; use the guarded `Final` phase.
- Do not publish the Function directly with Core Tools; use the guarded `Function` phase.
- Roll forward or back with a compatible immutable image, catalog digest, and Function artifact under newly reviewed hashes.

## Related Documentation

- [Documentation index](README.md)
- [Configuration reference](CONFIGURATION.md)
- [Architecture](ARCHITECTURE.md)
- [API reference](API_REFERENCE.md)
- [Azure resource inventory](AZURE_RESOURCES.md)
- [Demo and E2E runbook](DEMO_RUNBOOK.md)
- [Cloud operations request](INFRASTRUCTURE_REQUEST.md)
- [Production readiness](PRODUCTION_READINESS.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Protected evaluation](../evaluation/README.md)
