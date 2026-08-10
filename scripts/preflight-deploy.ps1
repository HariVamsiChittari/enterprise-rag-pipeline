[CmdletBinding()]
param(
    [string]$ProjectPath = "C:\Users\hchittari\OneDrive - Microsoft\Work\Project\BFL\bfl-ai-portfolio-repo\src\customer-solutions\rag-project",
    [switch]$CheckAzure
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host "=== $Title ===" -ForegroundColor Cyan
}

function Get-AzdEnvValue {
    param([string]$Name)
    $value = ""
    try {
        $value = (& azd env get-value $Name 2>$null)
    }
    catch {
        return ""
    }
    if ($LASTEXITCODE -ne 0) {
        return ""
    }
    return ($value | Out-String).Trim()
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

if (-not (Test-Path $ProjectPath)) {
    throw "Project path not found: $ProjectPath"
}

Set-Location $ProjectPath

if (-not (Test-Path "azure.yaml")) {
    throw "azure.yaml not found. Run from rag-project root."
}

if (-not (Test-CommandExists "azd")) {
    throw "azd is not installed or not on PATH."
}

Write-Section "Azd Environment"
$envName = Get-AzdEnvValue "AZURE_ENV_NAME"
if ([string]::IsNullOrWhiteSpace($envName)) {
    Write-Host "[FAIL] AZURE_ENV_NAME is not set." -ForegroundColor Red
    Write-Host "Run: azd env new rag-project"
}
else {
    Write-Host "[PASS] AZURE_ENV_NAME = $envName" -ForegroundColor Green
}

$requiredAzdKeys = @(
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_LOCATION",
    "infra.parameters.environmentName",
    "infra.parameters.sharePointTenantId",
    "infra.parameters.sharePointAppClientId",
    "infra.parameters.sharePointDriveId",
    "infra.parameters.ingestionSourceId",
    "infra.parameters.sharePointWebhookClientState",
    "infra.parameters.sharePointWebhookResourcePrefix",
    "infra.parameters.adminApiClientId",
    "infra.parameters.chatDeploymentName"
)

$missingAzdKeys = @()
foreach ($key in $requiredAzdKeys) {
    $value = Get-AzdEnvValue $key
    if ([string]::IsNullOrWhiteSpace($value)) {
        $missingAzdKeys += $key
        Write-Host "[MISSING] $key" -ForegroundColor Yellow
    }
    else {
        if ($key -like "*ClientState*") {
            Write-Host "[SET] $key = ***" -ForegroundColor Green
        }
        else {
            Write-Host "[SET] $key = $value" -ForegroundColor Green
        }
    }
}

Write-Section "Readme Deployment Inputs"
$readmeKeys = @(
    "AZURE_OPENAI_ACCOUNT_NAME",
    "AZURE_OPENAI_RESOURCE_GROUP",
    "OPENAI_EMBEDDING_DEPLOYMENT_NAME",
    "OPENAI_CHAT_DEPLOYMENT_NAME",
    "SHAREPOINT_TENANT_ID",
    "SHAREPOINT_APP_CLIENT_ID",
    "SHAREPOINT_ASSIGNED_DRIVE_ID",
    "INGESTION_SOURCE_ID",
    "SHAREPOINT_CERTIFICATE_SECRET_NAME",
    "SHAREPOINT_WEBHOOK_CLIENT_STATE",
    "SHAREPOINT_WEBHOOK_RESOURCE_PREFIX",
    "ADMIN_API_CLIENT_ID"
)

$missingReadmeKeys = @()
foreach ($key in $readmeKeys) {
    $value = Get-AzdEnvValue $key
    if ([string]::IsNullOrWhiteSpace($value)) {
        $missingReadmeKeys += $key
        Write-Host "[MISSING] $key" -ForegroundColor Yellow
    }
    else {
        if ($key -like "*CLIENT_STATE*" -or $key -like "*SECRET*") {
            Write-Host "[SET] $key = ***" -ForegroundColor Green
        }
        else {
            Write-Host "[SET] $key = $value" -ForegroundColor Green
        }
    }
}

$languageSkuCheckFailed = $false

Write-Section "Local Build Gates"
try {
    & az bicep build --file infra/main.bicep | Out-Null
    Write-Host "[PASS] bicep build infra/main.bicep" -ForegroundColor Green
}
catch {
    Write-Host "[FAIL] bicep build infra/main.bicep" -ForegroundColor Red
    throw
}

if ($CheckAzure) {
    if (-not (Test-CommandExists "az")) {
        throw "Azure CLI (az) is not installed or not on PATH."
    }

    Write-Section "Azure Account Context"
    try {
        $account = & az account show --query "{subscriptionId:id,tenantId:tenantId,name:name}" -o json | ConvertFrom-Json
        Write-Host "[PASS] Azure CLI logged in: $($account.name)" -ForegroundColor Green
        Write-Host "       Subscription: $($account.subscriptionId)"
        Write-Host "       Tenant: $($account.tenantId)"
    }
    catch {
        Write-Host "[FAIL] az account show" -ForegroundColor Red
        throw
    }

    Write-Section "Provider Registration"
    $providers = @(
        "Microsoft.Web",
        "Microsoft.Storage",
        "Microsoft.DocumentDB",
        "Microsoft.KeyVault",
        "Microsoft.CognitiveServices",
        "Microsoft.ManagedIdentity",
        "Microsoft.OperationalInsights",
        "Microsoft.Insights",
        "Microsoft.Network",
        "Microsoft.Authorization"
    )
    foreach ($provider in $providers) {
        $state = (& az provider show --namespace $provider --query "registrationState" -o tsv 2>$null)
        if ($state -eq "Registered") {
            Write-Host "[PASS] $provider = Registered" -ForegroundColor Green
        }
        else {
            Write-Host "[WARN] $provider = $state" -ForegroundColor Yellow
        }
    }

    Write-Section "OpenAI Deployment Checks"
    $openAiAccount = Get-AzdEnvValue "AZURE_OPENAI_ACCOUNT_NAME"
    $openAiRg = Get-AzdEnvValue "AZURE_OPENAI_RESOURCE_GROUP"
    $embedDeployment = Get-AzdEnvValue "OPENAI_EMBEDDING_DEPLOYMENT_NAME"
    $chatDeployment = Get-AzdEnvValue "OPENAI_CHAT_DEPLOYMENT_NAME"

    if ([string]::IsNullOrWhiteSpace($openAiAccount) -or [string]::IsNullOrWhiteSpace($openAiRg)) {
        Write-Host "[SKIP] OpenAI account check: missing AZURE_OPENAI_ACCOUNT_NAME or AZURE_OPENAI_RESOURCE_GROUP" -ForegroundColor Yellow
    }
    else {
        try {
            & az cognitiveservices account show --name $openAiAccount --resource-group $openAiRg --query "name" -o tsv | Out-Null
            Write-Host "[PASS] OpenAI account found: $openAiAccount" -ForegroundColor Green
        }
        catch {
            Write-Host "[FAIL] OpenAI account lookup failed" -ForegroundColor Red
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($embedDeployment) -and -not [string]::IsNullOrWhiteSpace($openAiAccount) -and -not [string]::IsNullOrWhiteSpace($openAiRg)) {
        try {
            & az cognitiveservices account deployment show --name $openAiAccount --resource-group $openAiRg --deployment-name $embedDeployment --query "name" -o tsv | Out-Null
            Write-Host "[PASS] Embedding deployment found: $embedDeployment" -ForegroundColor Green
        }
        catch {
            Write-Host "[FAIL] Embedding deployment lookup failed: $embedDeployment" -ForegroundColor Red
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($chatDeployment) -and -not [string]::IsNullOrWhiteSpace($openAiAccount) -and -not [string]::IsNullOrWhiteSpace($openAiRg)) {
        try {
            & az cognitiveservices account deployment show --name $openAiAccount --resource-group $openAiRg --deployment-name $chatDeployment --query "name" -o tsv | Out-Null
            Write-Host "[PASS] Chat deployment found: $chatDeployment" -ForegroundColor Green
        }
        catch {
            Write-Host "[FAIL] Chat deployment lookup failed: $chatDeployment" -ForegroundColor Red
        }
    }

    Write-Section "Azure AI Language SKU Availability"
    $useLanguageFreeTier = Get-AzdEnvValue "infra.parameters.useLanguageFreeTier"
    $languageSku = if (($useLanguageFreeTier | Out-String).Trim().ToLowerInvariant() -eq "true") { "F0" } else { "S" }
    $languageLocation = Get-AzdEnvValue "AZURE_LOCATION"
    if ([string]::IsNullOrWhiteSpace($languageLocation)) {
        $languageLocation = "eastus"
    }
    try {
        $availableLanguageSkus = & az cognitiveservices account list-skus --kind TextAnalytics --location $languageLocation --query "[].name" -o tsv
        $normalizedSkus = @($availableLanguageSkus | ForEach-Object { ($_ | Out-String).Trim() } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($normalizedSkus -contains $languageSku) {
            Write-Host "[PASS] TextAnalytics SKU available in ${languageLocation}: $languageSku" -ForegroundColor Green
        }
        else {
            Write-Host "[FAIL] TextAnalytics SKU unavailable in ${languageLocation}: $languageSku" -ForegroundColor Red
            Write-Host "       Set: azd env config set infra.parameters.useLanguageFreeTier true" -ForegroundColor Yellow
            $languageSkuCheckFailed = $true
        }
    }
    catch {
        Write-Host "[WARN] Could not validate TextAnalytics SKU availability: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

Write-Section "Summary"
if ($missingAzdKeys.Count -eq 0 -and $missingReadmeKeys.Count -eq 0 -and -not $languageSkuCheckFailed) {
    Write-Host "[READY] Required deployment inputs are set." -ForegroundColor Green
}
else {
    Write-Host "[NOT READY] Missing inputs detected." -ForegroundColor Yellow
    foreach ($key in $missingAzdKeys) {
        Write-Host "  azd env config set $key <value>"
    }
    foreach ($key in $missingReadmeKeys) {
        Write-Host "  azd env set $key <value>"
    }
    if ($languageSkuCheckFailed) {
        Write-Host "  Validate Azure AI Language SKU availability in the target region or toggle free tier." -ForegroundColor Yellow
    }
}
