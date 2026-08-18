# Azure Setup Guide

Complete setup instructions for the Enterprise RAG Pipeline. Follow these steps in order — each section depends on the previous one.

## Prerequisites

Install these tools before starting:

| Tool | Version | Install |
|---|---|---|
| Azure CLI | 2.60+ | `winget install Microsoft.AzureCLI` |
| PowerShell | 7+ | `winget install Microsoft.PowerShell` |
| Python | 3.12+ | `winget install Python.Python.3.12` |
| Azure Functions Core Tools | 4.x | `npm install -g azure-functions-core-tools@4` |
| Docker | Latest | Required for ACA retrieval service |

**Azure permissions required:**
- Owner or Contributor on the target subscription
- Application Administrator in Entra ID (for app registrations)

```powershell
# Verify you're logged in
az login
az account show --query "{subscription:name, user:user.name}" -o table
```

---

## Part 1: One-Time Azure Resource Setup

### 1.1 Create Resource Group

```powershell
az group create --name rg-rag-project --location eastus2
```

### 1.2 Create Azure OpenAI Resource and Deploy Models

```powershell
# Create OpenAI resource (or use existing)
az cognitiveservices account create `
  --name <openai-account-name> `
  --resource-group <openai-resource-group> `
  --kind OpenAI `
  --sku S0 `
  --location eastus2

# Deploy embedding model
az cognitiveservices account deployment create `
  --name <openai-account-name> `
  --resource-group <openai-resource-group> `
  --deployment-name text-embedding-3-large `
  --model-name text-embedding-3-large `
  --model-version "1" `
  --model-format OpenAI `
  --sku-capacity 120 `
  --sku-name Standard

# Deploy chat model
az cognitiveservices account deployment create `
  --name <openai-account-name> `
  --resource-group <openai-resource-group> `
  --deployment-name <chat-deployment-name> `
  --model-name <model-name> `
  --model-version "<version>" `
  --model-format OpenAI `
  --sku-capacity 80 `
  --sku-name Standard
```

Save these values:
- `AZURE_OPENAI_ACCOUNT_NAME` = the account name
- `AZURE_OPENAI_RESOURCE_GROUP` = the resource group
- `OPENAI_EMBEDDING_DEPLOYMENT_NAME` = `text-embedding-3-large`
- `OPENAI_CHAT_DEPLOYMENT_NAME` = your chat deployment name

### 1.3 Prepare SharePoint Site

1. Create a SharePoint site (or use existing): `https://<tenant>.sharepoint.com/sites/<site-name>`
2. Create a document library (e.g., "Policies")
3. Upload test PDF files to the library
4. Create Entra security groups (e.g., `SG-Finance`, `SG-Legal`) and assign them as readers on specific documents/folders
5. Find the drive ID:
   - Open Graph Explorer: https://developer.microsoft.com/graph/graph-explorer
   - Query: `GET https://graph.microsoft.com/v1.0/sites/<site-id>/drives`
   - Copy the `id` field of your document library's drive

Save: `SHAREPOINT_ASSIGNED_DRIVE_ID` (looks like `b!J4-sjvEAhkCVc8...`)

### 1.4 Create Entra App Registration (Graph API Access)

```powershell
# Create the app
$app = az ad app create `
  --display-name "RAG-SharePoint-Ingestion" `
  --sign-in-audience AzureADMyOrg `
  --query "{appId:appId, id:id}" -o json | ConvertFrom-Json

Write-Host "App Client ID: $($app.appId)"

# Grant Microsoft Graph application permissions
$graphAppId = "00000003-0000-0000-c000-000000000000"

# Files.Read.All
az ad app permission add --id $app.appId `
  --api $graphAppId `
  --api-permissions "df021288-bdef-4463-88db-98f22de89214=Role"

# Sites.Read.All
az ad app permission add --id $app.appId `
  --api $graphAppId `
  --api-permissions "01d4f6a1-cbc3-44db-832a-4ab4cfb6c8b3=Role"

# Group.Read.All
az ad app permission add --id $app.appId `
  --api $graphAppId `
  --api-permissions "5b567255-7703-4780-807c-7be8301ae99b=Role"

# Grant admin consent
az ad app permission admin-consent --id $app.appId
```

Save: `SHAREPOINT_APP_CLIENT_ID` = the appId

### 1.5 Create Certificate for Graph Authentication

The app authenticates to Microsoft Graph using a certificate (more secure than client secrets).

```powershell
# Create self-signed certificate (valid 2 years)
$cert = New-SelfSignedCertificate `
  -Subject "CN=RAG-SharePoint-Graph-Auth" `
  -CertStoreLocation "Cert:\CurrentUser\My" `
  -KeyExportPolicy Exportable `
  -KeySpec Signature `
  -KeyLength 2048 `
  -KeyAlgorithm RSA `
  -HashAlgorithm SHA256 `
  -NotAfter (Get-Date).AddYears(2)

Write-Host "Thumbprint: $($cert.Thumbprint)"
Write-Host "Expires: $($cert.NotAfter)"

# Export public key for app registration
Export-Certificate -Cert $cert -FilePath "./sharepoint-graph-cert.cer"

# Upload public key to the Entra App Registration
az ad app credential reset --id $app.appId --cert @sharepoint-graph-cert.cer --append
```

### 1.6 Upload Certificate to Key Vault

After infrastructure is deployed (Step 2.2), upload the certificate:

```powershell
$kvName = "<your-keyvault-name>"  # Created by Bicep deployment

# Temporarily enable access (KV is behind VNet)
az keyvault update --name $kvName --resource-group rg-rag-project `
  --public-network-access Enabled
$myIp = (Invoke-RestMethod -Uri "https://api.ipify.org")
az keyvault network-rule add --name $kvName `
  --resource-group rg-rag-project --ip-address "$myIp/32"

# Grant yourself secret write access
$userId = (az ad signed-in-user show --query id -o tsv)
$kvScope = "/subscriptions/<sub-id>/resourceGroups/rg-rag-project/providers/Microsoft.KeyVault/vaults/$kvName"
az role assignment create --role "Key Vault Secrets Officer" `
  --assignee $userId --scope $kvScope
Start-Sleep 30  # Wait for RBAC propagation

# Upload PFX as base64 secret (no password)
$pfxBytes = $cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Pfx)
$base64 = [System.Convert]::ToBase64String($pfxBytes)
az keyvault secret set --vault-name $kvName `
  --name "sharepoint-app-cert" --value $base64

# Restore firewall
az keyvault network-rule remove --name $kvName `
  --resource-group rg-rag-project --ip-address "$myIp/32"
az keyvault update --name $kvName --resource-group rg-rag-project `
  --public-network-access Disabled

# Clean up local files
Remove-Item ./sharepoint-graph-cert.cer
Remove-Item "Cert:\CurrentUser\My\$($cert.Thumbprint)"
```

### 1.7 Create Admin API App Registration (Easy Auth)

This protects the Function App HTTP endpoints.

```powershell
$adminApp = az ad app create `
  --display-name "RAG-Admin-API" `
  --sign-in-audience AzureADMyOrg `
  --query "{appId:appId}" -o json | ConvertFrom-Json

# Set identifier URI
az ad app update --id $adminApp.appId `
  --identifier-uris "api://$($adminApp.appId)"

Write-Host "Admin API Client ID: $($adminApp.appId)"
```

Save: `ADMIN_API_CLIENT_ID` = the appId

### 1.8 Get Graph Service Principal ID

```powershell
$graphSpId = az ad sp show --id 00000003-0000-0000-c000-000000000000 --query id -o tsv
Write-Host "Graph SP ID: $graphSpId"
```

Save: `GRAPH_SERVICE_PRINCIPAL_ID` = the ID

### 1.9 Generate Webhook Secret

```powershell
$webhookSecret = python -c "import secrets; print(secrets.token_urlsafe(32))"
Write-Host "Webhook Secret: $webhookSecret"
```

Save: `WEBHOOK_CLIENT_STATE` = the generated secret

---

## Part 2: Deploy and Run

### 2.1 Configure Environment Variables

Create `.azure/rag-project/.env`:

```
ADMIN_API_CLIENT_ID="<from 1.7>"
AZURE_ENV_NAME="rag-project"
AZURE_LOCATION="eastus2"
AZURE_OPENAI_ACCOUNT_NAME="<from 1.2>"
AZURE_OPENAI_RESOURCE_GROUP="<from 1.2>"
GRAPH_SERVICE_PRINCIPAL_ID="<from 1.8>"
INGESTION_SOURCE_ID="sharepoint-drive"
OPENAI_CHAT_DEPLOYMENT_NAME="<from 1.2>"
OPENAI_EMBEDDING_DEPLOYMENT_NAME="text-embedding-3-large"
SHAREPOINT_APP_CLIENT_ID="<from 1.4>"
SHAREPOINT_ASSIGNED_DRIVE_ID="<from 1.3>"
SHAREPOINT_CERTIFICATE_SECRET_NAME="sharepoint-app-cert"
SHAREPOINT_TENANT_ID="<your Entra tenant ID>"
WEBHOOK_CLIENT_STATE="<from 1.9>"
```

See `README.md` for the full environment variable reference.

### 2.2 Deploy Infrastructure (Bicep)

```powershell
# Load env vars
Get-Content .azure/rag-project/.env | ForEach-Object {
  if ($_ -match '^([^=]+)="?([^"]*)"?$') {
    [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
  }
}

# Deploy
az deployment group create `
  --resource-group rg-rag-project `
  --template-file infra/main.bicep `
  --parameters infra/main.parameters.dev.bicepparam
```

This creates: Cosmos DB, Storage, Key Vault, Functions App, Document Intelligence, Language Service, App Insights, VNet, and all RBAC assignments.

**After first deployment:** Go back to Step 1.6 to upload the certificate to the newly created Key Vault.

### 2.3 Deploy Function App

```powershell
cd app
func azure functionapp publish <function-app-name> --python
```

The function app name is in the deployment output or: `az functionapp list --resource-group rg-rag-project --query "[0].name" -o tsv`

### 2.4 Deploy Retrieval Service (ACA)

```powershell
# Build and push Docker image
cd app/retrieval
az acr build --registry <acr-name> `
  --image rag-retrieval:latest .

# ACA is deployed by Bicep — it pulls from ACR automatically
# To force a new revision:
az containerapp update `
  --name <aca-name> `
  --resource-group rg-rag-project `
  --image <acr-name>.azurecr.io/rag-retrieval:latest
```

### 2.5 Run Initial Ingestion

```powershell
$base = "https://<function-app-name>.azurewebsites.net"
$token = (az account get-access-token `
  --resource "api://<ADMIN_API_CLIENT_ID>" --query accessToken -o tsv)
$headers = @{ Authorization = "Bearer $token" }

# Trigger full-sync
Invoke-WebRequest -Uri "$base/api/ingestion/full-sync" `
  -Method POST -Headers $headers

# Monitor progress (poll every 30s)
do {
  Start-Sleep 30
  $r = Invoke-WebRequest -Uri "$base/api/ingestion/status" `
    -Method GET -Headers $headers
  $status = ($r.Content | ConvertFrom-Json).runtimeStatus
  Write-Host "Status: $status"
} while ($status -match "Running|Pending")

# View final result
($r.Content | ConvertFrom-Json).output | ConvertTo-Json
```

### 2.6 Activate Webhook (for real-time sync)

The `subscription_renew_timer` creates the Graph webhook subscription automatically at 02:00 UTC daily. To verify the webhook endpoint is working:

```powershell
# Test validation handshake
Invoke-WebRequest `
  -Uri "$base/api/webhook/sharepoint?validationToken=test" `
  -Method POST -ContentType "text/plain" -SkipHttpErrorCheck
# Should return 200 with body "test"
```

---

## Part 3: Local Development

### 3.1 Set Up Python Environment

```powershell
cd <project-root>
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pip install -r app/requirements.txt
```

### 3.2 Run Tests

```powershell
python -m pytest tests/ --tb=short
```

### 3.3 Run Function App Locally

```powershell
cd app
func start
```

Requires `local.settings.json` with valid Azure endpoints (uses real cloud services for Cosmos, OpenAI, etc.).

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `403 Forbidden: readMetadata` on Cosmos | RBAC role assignment missing or not propagated | Re-deploy Bicep (RBAC is always-create now) or wait 5 min |
| `ResourceNotFoundError` for Key Vault secret | Certificate not uploaded to Key Vault | Follow Step 1.6 |
| `400 Bad Request` on Graph drive API | Drive ID has `\!` instead of `!` | Fix in `.env` file: remove the backslash |
| `401 Unauthorized` on ingestion endpoints | Token expired or wrong audience | Re-run `az account get-access-token --resource "api://<CLIENT_ID>"` |
| `429 Too Many Requests` from Graph | Rate limited during ingestion | Wait and retry; reduce `WAVE_SIZE` |
| Webhook returns `403` | Wrong `WEBHOOK_CLIENT_STATE` in request vs app setting | Verify the secret matches in both places |
| Key Vault `ForbiddenByFirewall` | Public access disabled + IP not allowed | Temporarily add your IP (Step 1.6) |

---

## Certificate Renewal

The self-signed certificate expires after 2 years. To renew:

1. Create a new certificate (Step 1.5)
2. Upload to Key Vault (Step 1.6) — overwrites the existing secret
3. Upload public key to app registration with `--append` flag
4. The Function App picks up the new cert on next cold start
5. After confirming it works, optionally remove the old cert from the app registration

---

## Security Checklist

- [ ] Key Vault public access is **Disabled** in production
- [ ] No certificates or secrets in source control (`.gitignore` covers `*.pfx`, `*.cer`, `*.pem`)
- [ ] `WEBHOOK_CLIENT_STATE` is a strong random secret (32+ characters)
- [ ] Use CA-signed certificates in production (not self-signed)
- [ ] All service communication uses Managed Identity (no connection strings)
- [ ] Function App Easy Auth blocks unauthenticated access (except webhook paths)
