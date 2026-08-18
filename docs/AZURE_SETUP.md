# Azure Setup Guide

Step-by-step commands to configure all Azure prerequisites for the Enterprise RAG Pipeline.

## Prerequisites

- Azure CLI (`az`) installed and logged in
- PowerShell 7+
- Owner or Contributor role on the Azure subscription
- Application Administrator role in Entra ID (for app registration)

## 1. Create Resource Group

```powershell
az group create --name rg-rag-project --location eastus2
```

## 2. Create Entra ID App Registration (for SharePoint Graph API access)

```powershell
# Create the app registration
az ad app create --display-name "RAG-SharePoint-Ingestion" \
  --sign-in-audience AzureADMyOrg \
  --query "{appId:appId, objectId:id}" -o json

# Save the appId — you'll need it as SHAREPOINT_APP_CLIENT_ID
# Grant Microsoft Graph API permissions (application-level)
$appId = "<appId from above>"
az ad app permission add --id $appId \
  --api 00000003-0000-0000-c000-000000000000 \
  --api-permissions \
    df021288-bdef-4463-88db-98f22de89214=Role \
    01d4f6a1-cbc3-44db-832a-4ab4cfb6c8b3=Role \
    5b567255-7703-4780-807c-7be8301ae99b=Role

# Grant admin consent
az ad app permission admin-consent --id $appId
```

Permissions added:
- `Files.Read.All` — read all files in SharePoint
- `Sites.Read.All` — enumerate site collections
- `Group.Read.All` — resolve security group membership for ACLs

## 3. Create Self-Signed Certificate for Graph Authentication

The app uses certificate-based authentication (more secure than client secrets).

```powershell
# Step 3a: Create the certificate
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

# Step 3b: Export public key (.cer) for app registration
Export-Certificate -Cert $cert -FilePath "./sharepoint-graph-cert.cer"

# Step 3c: Upload public key to the Entra App Registration
az ad app credential reset --id $appId --cert @sharepoint-graph-cert.cer --append
```

## 4. Upload Certificate to Azure Key Vault

The Function App reads the certificate from Key Vault at runtime.

```powershell
$kvName = "<your-keyvault-name>"  # e.g., rag-dev-kv-apniu6o4

# Step 4a: Temporarily enable public access if KV is behind a VNet
az keyvault update --name $kvName --resource-group rg-rag-project --public-network-access Enabled

# Step 4b: Add your IP to the firewall allow list
$myIp = (Invoke-RestMethod -Uri "https://api.ipify.org")
az keyvault network-rule add --name $kvName --resource-group rg-rag-project --ip-address "$myIp/32"

# Step 4c: Grant yourself Key Vault Secrets Officer role
$userId = (az ad signed-in-user show --query id -o tsv)
az role assignment create --role "Key Vault Secrets Officer" --assignee $userId \
  --scope "/subscriptions/<subscription-id>/resourceGroups/rg-rag-project/providers/Microsoft.KeyVault/vaults/$kvName"

# Wait for RBAC propagation
Start-Sleep 30

# Step 4d: Export PFX (no password) and upload as base64 secret
$pfxBytes = $cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Pfx)
$base64 = [System.Convert]::ToBase64String($pfxBytes)
az keyvault secret set --vault-name $kvName --name "sharepoint-app-cert" --value $base64

# Step 4e: Restore Key Vault firewall
az keyvault network-rule remove --name $kvName --resource-group rg-rag-project --ip-address "$myIp/32"
az keyvault update --name $kvName --resource-group rg-rag-project --public-network-access Disabled

# Step 4f: Clean up local files
Remove-Item ./sharepoint-graph-cert.cer
Remove-Item "Cert:\CurrentUser\My\$($cert.Thumbprint)"
```

## 5. Find Your SharePoint Drive ID

```powershell
# Get the Graph service principal object ID (needed for Bicep)
az ad sp show --id 00000003-0000-0000-c000-000000000000 --query id -o tsv
# Save as GRAPH_SERVICE_PRINCIPAL_ID

# Use Graph Explorer or PowerShell to find the drive ID:
# Navigate to: https://graph.microsoft.com/v1.0/sites/<site-id>/drives
# The drive ID looks like: b!J4-sjvEAhkCVc8qNomfl2k2gxfZ9VfdJmLYF3BRDHbtMesveBYpUSqoGBndmtynK
# Save as SHAREPOINT_ASSIGNED_DRIVE_ID
```

## 6. Create Entra App Registration for API Authentication (Easy Auth)

This app protects the Function App HTTP endpoints (ingestion/status, full-sync, etc.).

```powershell
az ad app create --display-name "RAG-Admin-API" \
  --sign-in-audience AzureADMyOrg \
  --identifier-uris "api://<unique-id>" \
  --query "{appId:appId}" -o json

# Save the appId as ADMIN_API_CLIENT_ID
```

## 7. Set Environment Variables

Create a `.azure/<env-name>/.env` file with all required values:

```
ADMIN_API_CLIENT_ID="<from step 6>"
AZURE_ENV_NAME="rag-project"
AZURE_LOCATION="eastus2"
AZURE_OPENAI_ACCOUNT_NAME="<your-openai-account>"
AZURE_OPENAI_RESOURCE_GROUP="<openai-resource-group>"
GRAPH_SERVICE_PRINCIPAL_ID="<from step 5>"
INGESTION_SOURCE_ID="sharepoint-drive"
OPENAI_CHAT_DEPLOYMENT_NAME="<your-chat-model>"
OPENAI_EMBEDDING_DEPLOYMENT_NAME="text-embedding-3-large"
SHAREPOINT_APP_CLIENT_ID="<from step 2>"
SHAREPOINT_ASSIGNED_DRIVE_ID="<from step 5>"
SHAREPOINT_CERTIFICATE_SECRET_NAME="sharepoint-app-cert"
SHAREPOINT_TENANT_ID="<your-tenant-id>"
WEBHOOK_CLIENT_STATE="<generate-a-random-secret>"
```

## 8. Deploy Infrastructure

```powershell
# Load environment variables
Get-Content .azure/rag-project/.env | ForEach-Object {
    if ($_ -match '^([^=]+)="?([^"]*)"?$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
    }
}

# Deploy Bicep
az deployment group create \
  --resource-group rg-rag-project \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.dev.bicepparam
```

## 9. Deploy Function App

```powershell
cd app
func azure functionapp publish <function-app-name> --python
```

## 10. Run Initial Ingestion

```powershell
$base = "https://<function-app-name>.azurewebsites.net"
$token = (az account get-access-token --resource "api://<ADMIN_API_CLIENT_ID>" --query accessToken -o tsv)
$headers = @{ Authorization = "Bearer $token" }

# Trigger full-sync
Invoke-WebRequest -Uri "$base/api/ingestion/full-sync" -Method POST -Headers $headers

# Check status
Invoke-WebRequest -Uri "$base/api/ingestion/status" -Method GET -Headers $headers | Select-Object -ExpandProperty Content | ConvertFrom-Json
```

## 11. Certificate Renewal

The self-signed certificate expires after 2 years. To renew:

1. Repeat steps 3-4 with a new certificate
2. The old certificate will continue to work until its expiry
3. Use `--append` flag in step 3c to add the new cert without removing the old one
4. After the app starts using the new cert (it reads from Key Vault on each cold start), you can remove the old cert from the app registration

## Security Notes

- **Never commit certificates, PFX files, or Key Vault secrets to source control**
- The `.gitignore` should exclude `*.pfx`, `*.cer`, `*.pem` files
- Key Vault public access should remain disabled in production
- Use CA-signed certificates in production (not self-signed)
- The `WEBHOOK_CLIENT_STATE` should be a strong random secret (32+ characters)
