<#
.SYNOPSIS
    End-to-end deployment: Bicep infrastructure + code deployment.

.DESCRIPTION
    Phase 1 (Declarative): az deployment group create with infra/main.bicep
    Phase 2 (Imperative):  ACR image build, ACA image update, Function App publish
    Phase 3 (Manual):      Certificate upload to Key Vault (interactive)

    All infrastructure configuration lives in Bicep. The imperative steps are
    limited to code deployment which cannot be expressed declaratively.

.PARAMETER ResourceGroup
    Target resource group name.

.PARAMETER Environment
    Environment name (dev/prod). Used to select .bicepparam file.

.PARAMETER SkipInfra
    Skip Phase 1 (useful when re-deploying code only).

.PARAMETER SkipCode
    Skip Phase 2 (useful when testing infra-only changes).
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = "rg-rag-project",
    [string]$Location = "eastus2",
    [ValidateSet("dev", "prod")]
    [string]$Environment = "dev",
    [switch]$SkipInfra,
    [switch]$SkipCode
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$InfraDir = Join-Path $ProjectRoot "infra"
$AppDir = Join-Path $ProjectRoot "app"
$EnvFile = Join-Path $ProjectRoot ".azure" "rag-project" ".env"

# ─── Load environment variables from .azure/rag-project/.env ───
function Import-EnvFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        throw "Environment file not found: $Path"
    }
    Get-Content $Path | ForEach-Object {
        if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*"?([^"]*)"?\s*$') {
            [System.Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
        }
    }
    Write-Host "  Loaded environment from: $Path" -ForegroundColor DarkGray
}

# ─── Phase 0: Prerequisites ───
Write-Host "`n=== Phase 0: Prerequisites ===" -ForegroundColor Cyan
Import-EnvFile -Path $EnvFile

# Set WEBHOOK_CLIENT_STATE if not already in environment
if (-not $env:WEBHOOK_CLIENT_STATE) {
    $env:WEBHOOK_CLIENT_STATE = "rag-dev-webhook-secret-2026"
    Write-Host "  WEBHOOK_CLIENT_STATE set to default (override via env var)" -ForegroundColor Yellow
}

# Verify Azure CLI login
$account = az account show --query "{sub:id, name:name}" -o json 2>&1 | ConvertFrom-Json
if (-not $account.sub) {
    throw "Not logged in to Azure CLI. Run 'az login' first."
}
Write-Host "  Subscription: $($account.name) ($($account.sub))"

# ─── Phase 1: Infrastructure (Bicep) ───
if (-not $SkipInfra) {
    Write-Host "`n=== Phase 1: Infrastructure Deployment ===" -ForegroundColor Cyan

    # Create resource group if it doesn't exist
    $rgExists = az group exists --name $ResourceGroup 2>&1
    if ($rgExists -eq "false") {
        Write-Host "  Creating resource group: $ResourceGroup ($Location)"
        az group create --name $ResourceGroup --location $Location --tags Environment=$Environment Project=RAG-SharePoint | Out-Null
    }
    else {
        Write-Host "  Resource group exists: $ResourceGroup"
    }

    # Deploy Bicep
    $paramFile = Join-Path $InfraDir "main.parameters.$Environment.bicepparam"
    Write-Host "  Deploying: infra/main.bicep with $Environment parameters..."
    Write-Host "  (This typically takes 8-12 minutes)" -ForegroundColor DarkGray

    $deployOutput = az deployment group create `
        --resource-group $ResourceGroup `
        --template-file (Join-Path $InfraDir "main.bicep") `
        --parameters $paramFile `
        --query "properties.outputs" `
        -o json 2>&1

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  DEPLOYMENT FAILED:" -ForegroundColor Red
        Write-Host $deployOutput
        throw "Bicep deployment failed"
    }

    $outputs = $deployOutput | ConvertFrom-Json
    Write-Host "  Deployment succeeded!" -ForegroundColor Green

    # Extract outputs
    $funcAppName = $outputs.functionAppName.value
    $acrLoginServer = $outputs.acrLoginServer.value
    $kvName = $outputs.keyVaultName.value
    $retrievalUrl = $outputs.retrievalServiceUrl.value

    Write-Host "`n  Deployment Outputs:" -ForegroundColor DarkGray
    Write-Host "    Function App:    $funcAppName"
    Write-Host "    ACR:             $acrLoginServer"
    Write-Host "    Key Vault:       $kvName"
    Write-Host "    Retrieval URL:   $retrievalUrl"
}
else {
    Write-Host "`n=== Phase 1: SKIPPED (--SkipInfra) ===" -ForegroundColor Yellow
    # Retrieve existing outputs
    $deployOutput = az deployment group show `
        --resource-group $ResourceGroup `
        --name "main" `
        --query "properties.outputs" `
        -o json 2>&1 | ConvertFrom-Json

    $funcAppName = $deployOutput.functionAppName.value
    $acrLoginServer = $deployOutput.acrLoginServer.value
    $kvName = $deployOutput.keyVaultName.value
    $retrievalUrl = $deployOutput.retrievalServiceUrl.value
}

# ─── Phase 2: Code Deployment ───
if (-not $SkipCode) {
    Write-Host "`n=== Phase 2: Code Deployment ===" -ForegroundColor Cyan

    # 2a. Build and push container image to ACR
    $acrName = $acrLoginServer -replace '\.azurecr\.io$', ''
    Write-Host "  Building container image in ACR: $acrName..."
    Push-Location $AppDir
    try {
        az acr build `
            --registry $acrName `
            --image rag-retrieval:latest `
            --file retrieval/Dockerfile . 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "ACR build failed" }
        Write-Host "    Image built: $acrLoginServer/rag-retrieval:latest" -ForegroundColor Green
    }
    finally {
        Pop-Location
    }

    # 2b. Update ACA to use the new image
    $acaName = $retrievalUrl -replace '^https?://', '' -replace '\.internal\..*$', ''
    Write-Host "  Updating Container App image: $acaName..."
    az containerapp update `
        --name $acaName `
        --resource-group $ResourceGroup `
        --image "$acrLoginServer/rag-retrieval:latest" `
        --container-name retrieval-agent 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "ACA image update failed" }
    Write-Host "    Container App updated" -ForegroundColor Green

    # 2c. Publish Function App
    Write-Host "  Publishing Function App: $funcAppName..."
    Push-Location $AppDir
    try {
        func azure functionapp publish $funcAppName --python 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Function App publish failed" }
        Write-Host "    Function App published" -ForegroundColor Green
    }
    finally {
        Pop-Location
    }
}
else {
    Write-Host "`n=== Phase 2: SKIPPED (--SkipCode) ===" -ForegroundColor Yellow
}

# ─── Phase 3: Post-deploy reminders ───
Write-Host "`n=== Phase 3: Manual Steps ===" -ForegroundColor Cyan
Write-Host @"
  If this is a fresh deployment, complete these steps:

  1. Upload SharePoint certificate to Key Vault:
     az keyvault secret set --vault-name $kvName \
       --name sharepoint-app-cert \
       --file <path-to-pfx-base64> \
       --content-type application/x-pkcs12

  2. Register the public key with the Entra app:
     az ad app credential reset --id $($env:SHAREPOINT_APP_CLIENT_ID) \
       --cert <path-to-cer>

  3. Create Graph webhook subscription:
     POST https://<func-app-url>/api/ingestion/full-sync
     (The first delta sync will auto-create the subscription)

"@ -ForegroundColor DarkGray

Write-Host "=== Deployment Complete ===" -ForegroundColor Green
Write-Host "  Function App URL: https://$funcAppName.azurewebsites.net"
Write-Host "  Retrieval Service: $retrievalUrl"
