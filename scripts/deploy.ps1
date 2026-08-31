<#
.SYNOPSIS
    Preview or execute one authorized phase of the greenfield ACA deployment.

.DESCRIPTION
    Preview is the default. Azure mutations require -Execute, exact plan/source
    hashes, and a target matching the current Azure CLI and azd context. The
    script never creates or deletes a resource group, mutates Entra directory
    objects, invents secrets, or preserves mutable image tags. Only the explicit
    OperationsCleanup phase deletes the uniquely tagged temporary catalog job.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Authority', 'Foundation', 'Build', 'Operations', 'Catalog', 'CatalogVerify', 'OperationsCleanup', 'Final', 'Function')]
    [string]$Phase,

    [string]$PlanId = 'aca-greenfield-retrieval-v1',
    [string]$ExpectedPlanHash,
    [string]$ExpectedSourceTreeHash,
    [string]$SubscriptionId,
    [string]$TenantId,
    [string]$ResourceGroup,
    [string]$Location,
    [string]$AzdEnvironment,
    [string]$DeploymentInstanceId,
    [string]$CatalogFile = 'app/retrieval/catalog.example.json',
    [string]$JobExecutionName,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PlanPath = Join-Path $ProjectRoot '.azure/deployment-plan.md'
$TemplatePath = Join-Path $ProjectRoot 'infra/main.bicep'
$ParameterPath = Join-Path $ProjectRoot 'infra/main.parameters.bicepparam'
$OperationsTemplatePath = Join-Path $ProjectRoot 'infra/modules/aca-operations-job.bicep'
$OperationsParameterPath = Join-Path $ProjectRoot 'infra/operations.parameters.bicepparam'
$ProtectedPathPatterns = @(
    '^\.git/', '^\.venv/', '^app/\.venv', '^data/', '^demo-output/',
    '/__pycache__/', '^\.azure/[^/]+/\.env$'
)

function Get-RequiredValue {
    param([string]$Name, [string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name is required."
    }
    return $Value.Trim()
}

function Get-SourceTreeHash {
    $paths = git -C $ProjectRoot ls-files --cached --others --exclude-standard
    if ($LASTEXITCODE -ne 0) { throw 'Unable to enumerate the source tree.' }
    $included = @($paths | Sort-Object -Unique | Where-Object {
            $normalized = $_.Replace('\', '/')
            -not ($ProtectedPathPatterns | Where-Object { $normalized -match $_ }) -and
            (Test-Path -LiteralPath (Join-Path $ProjectRoot $_) -PathType Leaf)
        })
    $hashes = @($included | git -C $ProjectRoot hash-object --stdin-paths)
    if ($LASTEXITCODE -ne 0 -or $hashes.Count -ne $included.Count) {
        throw 'Unable to hash the source tree.'
    }
    $entries = for ($index = 0; $index -lt $included.Count; $index++) {
        $relativePath = $included[$index]
        $normalized = $relativePath.Replace('\', '/')
        "$normalized`n$($hashes[$index])"
    }
    $payload = [Text.Encoding]::UTF8.GetBytes(($entries -join "`n"))
    $stream = [IO.MemoryStream]::new($payload)
    try { return (Get-FileHash -InputStream $stream -Algorithm SHA256).Hash.ToLowerInvariant() }
    finally { $stream.Dispose() }
}

function Get-Authority {
    if (-not (Test-Path -LiteralPath $PlanPath -PathType Leaf)) {
        throw 'Deployment plan is missing.'
    }
    $planText = Get-Content -LiteralPath $PlanPath -Raw
    if ($planText -notmatch [regex]::Escape("Plan ID: ``$PlanId``")) {
        throw 'Deployment plan ID does not match this controller.'
    }
    if ($planText -match 'existing development environment') {
        throw 'Stale deployment target authority is present in the plan.'
    }
    return [ordered]@{
        planId         = $PlanId
        planHash       = (Get-FileHash -LiteralPath $PlanPath -Algorithm SHA256).Hash.ToLowerInvariant()
        sourceTreeHash = Get-SourceTreeHash
    }
}

function Assert-Authority {
    $authority = Get-Authority
    if ($authority.planHash -ne (Get-RequiredValue 'ExpectedPlanHash' $ExpectedPlanHash).ToLowerInvariant()) {
        throw 'Deployment plan hash changed after review.'
    }
    if ($authority.sourceTreeHash -ne (Get-RequiredValue 'ExpectedSourceTreeHash' $ExpectedSourceTreeHash).ToLowerInvariant()) {
        throw 'Source tree hash changed after review.'
    }
    return $authority
}

function Assert-Target {
    $script:SubscriptionId = Get-RequiredValue 'SubscriptionId' $SubscriptionId
    $script:TenantId = Get-RequiredValue 'TenantId' $TenantId
    $script:ResourceGroup = Get-RequiredValue 'ResourceGroup' $ResourceGroup
    $script:Location = Get-RequiredValue 'Location' $Location
    $script:AzdEnvironment = Get-RequiredValue 'AzdEnvironment' $AzdEnvironment
    $script:DeploymentInstanceId = Get-RequiredValue 'DeploymentInstanceId' $DeploymentInstanceId

    $account = az account show --query '{subscription:id,tenant:tenantId}' --output json --only-show-errors | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or -not $account.subscription) { throw 'Azure CLI authentication is required.' }
    if ($account.subscription -ne $SubscriptionId -or $account.tenant -ne $TenantId) {
        throw 'Azure CLI subscription or tenant does not match the reviewed target.'
    }
    $azdSubscription = azd env get-value AZURE_SUBSCRIPTION_ID --environment $AzdEnvironment 2>$null
    $azdLocation = azd env get-value AZURE_LOCATION --environment $AzdEnvironment 2>$null
    if ($LASTEXITCODE -ne 0 -or $azdSubscription.Trim() -ne $SubscriptionId -or $azdLocation.Trim() -ne $Location) {
        throw 'azd environment does not match the reviewed subscription and location.'
    }
    $exists = az group exists --name $ResourceGroup --subscription $SubscriptionId --only-show-errors
    if ($LASTEXITCODE -ne 0 -or $exists -ne 'true') {
        throw 'The reviewed resource group must already exist; this script will not create it.'
    }
}

function Assert-RequiredEnvironment {
    $required = @(
        'AZURE_OPENAI_ACCOUNT_NAME', 'AZURE_OPENAI_RESOURCE_GROUP',
        'OPENAI_CHAT_DEPLOYMENT_NAME', 'SHAREPOINT_TENANT_ID',
        'SHAREPOINT_APP_CLIENT_ID', 'SHAREPOINT_ASSIGNED_DRIVE_ID', 'SHAREPOINT_SITE_URL',
        'SHAREPOINT_KEY_VAULT_NAME', 'SHAREPOINT_KEY_VAULT_RESOURCE_GROUP',
        'INGESTION_SOURCE_ID', 'ADMIN_API_CLIENT_ID', 'FUNCTION_API_AUDIENCE',
        'RETRIEVAL_API_CLIENT_ID', 'RETRIEVAL_API_AUDIENCE',
        'RETRIEVAL_API_SERVICE_PRINCIPAL_ID',
        'FUNCTION_ALLOWED_CALLER_CLIENT_ID', 'WEBHOOK_CLIENT_STATE',
        'COST_CENTER', 'CLEANUP_DATE'
    )
    $missing = @($required | Where-Object { [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($_)) })
    if ($missing.Count -gt 0) { throw "Required deployment settings are missing: $($missing -join ', ')" }
}

function Import-AzdEnvironment {
    $names = @(
        'AZURE_OPENAI_ACCOUNT_NAME', 'AZURE_OPENAI_RESOURCE_GROUP',
        'OPENAI_EMBEDDING_DEPLOYMENT_NAME', 'OPENAI_CHAT_DEPLOYMENT_NAME',
        'SHAREPOINT_TENANT_ID', 'SHAREPOINT_APP_CLIENT_ID',
        'SHAREPOINT_ASSIGNED_DRIVE_ID', 'SHAREPOINT_SITE_URL',
        'SHAREPOINT_CERTIFICATE_SECRET_NAME', 'SHAREPOINT_KEY_VAULT_NAME',
        'SHAREPOINT_KEY_VAULT_RESOURCE_GROUP', 'INGESTION_SOURCE_ID',
        'ADMIN_API_CLIENT_ID', 'FUNCTION_API_AUDIENCE',
        'RETRIEVAL_API_CLIENT_ID', 'RETRIEVAL_API_AUDIENCE',
        'RETRIEVAL_API_SERVICE_PRINCIPAL_ID',
        'FUNCTION_ALLOWED_CALLER_CLIENT_ID', 'WEBHOOK_CLIENT_STATE',
        'COSMOS_DB_MODE', 'COSMOS_METADATA_AUTOSCALE_MAX_RUS',
        'COSMOS_SEARCH_AUTOSCALE_MAX_RUS', 'STORAGE_REDUNDANCY',
        'APPLICATION_INSIGHTS_DAILY_CAP_GB', 'RETRIEVAL_MIN_REPLICAS',
        'RETRIEVAL_MAX_REPLICAS', 'RETRIEVAL_ZONE_REDUNDANT',
        'COST_CENTER', 'CLEANUP_DATE', 'ACR_NAME', 'RELEASE_BUILD_ID',
        'RETRIEVAL_IMAGE_REFERENCE', 'RETRIEVAL_CATALOG_DIGEST'
    )
    foreach ($name in $names) {
        $value = azd env get-value $name --environment $AzdEnvironment 2>$null
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($value)) {
            [Environment]::SetEnvironmentVariable($name, $value.Trim(), 'Process')
        }
    }
}

function Set-ParameterEnvironment {
    $env:AZURE_LOCATION = $Location
    $env:DEPLOYMENT_INSTANCE_ID = $DeploymentInstanceId
}

function Get-SingleDeploymentResource {
    param([string]$ResourceType)
    $resources = @(az resource list `
            --subscription $SubscriptionId `
            --resource-group $ResourceGroup `
            --resource-type $ResourceType `
            --query "[?tags.DeploymentInstance=='$DeploymentInstanceId']" `
            --output json `
            --only-show-errors | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0 -or $resources.Count -ne 1) {
        throw "Expected exactly one $ResourceType resource for this deployment instance; found $($resources.Count)."
    }
    return $resources[0]
}

function Set-OperationsEnvironment {
    $identityName = "rag-$DeploymentInstanceId-operations-mi"
    $identity = az identity show `
        --subscription $SubscriptionId `
        --resource-group $ResourceGroup `
        --name $identityName `
        --output json `
        --only-show-errors | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $identity.tags.DeploymentInstance -ne $DeploymentInstanceId) {
        throw 'The operations identity does not match the reviewed deployment instance.'
    }
    $managedEnvironment = Get-SingleDeploymentResource 'Microsoft.App/managedEnvironments'
    $cosmosAccount = Get-SingleDeploymentResource 'Microsoft.DocumentDB/databaseAccounts'
    $cosmosEndpoint = az cosmosdb show `
        --subscription $SubscriptionId `
        --ids $cosmosAccount.id `
        --query documentEndpoint `
        --output tsv `
        --only-show-errors
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($cosmosEndpoint)) {
        throw 'The Cosmos DB endpoint could not be resolved.'
    }
    $registryName = Get-RequiredValue 'ACR_NAME' $env:ACR_NAME
    $registry = az acr show `
        --subscription $SubscriptionId `
        --resource-group $ResourceGroup `
        --name $registryName `
        --output json `
        --only-show-errors | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $registry.tags.DeploymentInstance -ne $DeploymentInstanceId) {
        throw 'The container registry does not match the reviewed deployment instance.'
    }
    $jobStem = ("rag-$DeploymentInstanceId" -replace '-', '')
    $jobStem = $jobStem.Substring(0, [Math]::Min(19, $jobStem.Length))
    $env:OPERATIONS_JOB_NAME = "$jobStem-catalog-job"
    $env:MANAGED_ENVIRONMENT_ID = $managedEnvironment.id
    $env:ACR_LOGIN_SERVER = $registry.loginServer
    $env:OPERATIONS_MANAGED_IDENTITY_ID = $identity.id
    $env:OPERATIONS_MANAGED_IDENTITY_CLIENT_ID = $identity.clientId
    $env:COSMOS_ENDPOINT = $cosmosEndpoint.Trim()
}

function Invoke-InfrastructurePhase {
    param([bool]$Serving, [bool]$Operations)
    Assert-RequiredEnvironment
    Set-ParameterEnvironment
    $env:DEPLOY_SERVING = if ($Serving) { 'true' } else { 'false' }
    $env:DEPLOY_OPERATIONS = if ($Operations) { 'true' } else { 'false' }
    if ($Serving -or $Operations) {
        if ($env:RETRIEVAL_IMAGE_REFERENCE -notmatch '^[a-z0-9.-]+/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$') {
            throw 'RETRIEVAL_IMAGE_REFERENCE must be repository@sha256:<64 lowercase hex>.'
        }
        if ($env:RETRIEVAL_CATALOG_DIGEST -notmatch '^sha256:[a-f0-9]{64}$') {
            throw 'RETRIEVAL_CATALOG_DIGEST must be sha256:<64 lowercase hex>.'
        }
    }
    else {
        $env:RETRIEVAL_IMAGE_REFERENCE = ''
        $env:RETRIEVAL_CATALOG_DIGEST = ''
    }

    az deployment group what-if `
        --subscription $SubscriptionId `
        --resource-group $ResourceGroup `
        --template-file $TemplatePath `
        --parameters $ParameterPath `
        --result-format FullResourcePayloads `
        --no-pretty-print `
        --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Infrastructure preview failed.' }
    if (-not $Execute) { return }

    az deployment group create `
        --subscription $SubscriptionId `
        --resource-group $ResourceGroup `
        --template-file $TemplatePath `
        --parameters $ParameterPath `
        --mode Incremental `
        --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Infrastructure deployment failed.' }
}

function Invoke-OperationsInfrastructure {
    Assert-RequiredEnvironment
    Set-ParameterEnvironment
    if ($env:RETRIEVAL_IMAGE_REFERENCE -notmatch '^[a-z0-9.-]+/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$') {
        throw 'RETRIEVAL_IMAGE_REFERENCE must be repository@sha256:<64 lowercase hex>.'
    }
    if ($env:RETRIEVAL_CATALOG_DIGEST -notmatch '^sha256:[a-f0-9]{64}$') {
        throw 'RETRIEVAL_CATALOG_DIGEST must be sha256:<64 lowercase hex>.'
    }
    Set-OperationsEnvironment
    az deployment group what-if `
        --subscription $SubscriptionId `
        --resource-group $ResourceGroup `
        --template-file $OperationsTemplatePath `
        --parameters $OperationsParameterPath `
        --result-format FullResourcePayloads `
        --no-pretty-print `
        --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Operations job preview failed.' }
    if (-not $Execute) { return }

    az deployment group create `
        --subscription $SubscriptionId `
        --resource-group $ResourceGroup `
        --template-file $OperationsTemplatePath `
        --parameters $OperationsParameterPath `
        --mode Incremental `
        --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Operations job deployment failed.' }
}

function Invoke-ImageBuild {
    $registryName = Get-RequiredValue 'ACR_NAME' $env:ACR_NAME
    $buildId = Get-RequiredValue 'RELEASE_BUILD_ID' $env:RELEASE_BUILD_ID
    if ($buildId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$') { throw 'RELEASE_BUILD_ID is invalid.' }
    if (-not $Execute) {
        [ordered]@{ action = 'preview'; image = "rag-retrieval:$buildId" } | ConvertTo-Json -Compress
        return
    }
    az acr build --registry $registryName --image "rag-retrieval:$buildId" --file app/retrieval/Dockerfile app --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'ACR build failed.' }
    $digest = az acr repository show --name $registryName --image "rag-retrieval:$buildId" --query digest --output tsv --only-show-errors
    if ($LASTEXITCODE -ne 0 -or $digest -notmatch '^sha256:[a-f0-9]{64}$') { throw 'ACR did not return an immutable digest.' }
    $loginServer = az acr show --name $registryName --query loginServer --output tsv --only-show-errors
    [ordered]@{ imageReference = "$loginServer/rag-retrieval@$digest"; buildId = $buildId } | ConvertTo-Json -Compress
}

function Get-ReviewedCatalogDigest {
    $catalogPath = Join-Path $ProjectRoot $CatalogFile
    $output = & python @(
        (Join-Path $ProjectRoot 'tools/publish_retrieval_catalog.py'),
        'validate',
        '--file', $catalogPath,
        '--deployment-instance-id', $DeploymentInstanceId
    )
    if ($LASTEXITCODE -ne 0) { throw 'Catalog validation failed.' }
    $catalog = $output | ConvertFrom-Json
    if ($catalog.catalogDigest -notmatch '^sha256:[a-f0-9]{64}$') {
        throw 'Catalog validation did not return an immutable digest.'
    }
    return $catalog.catalogDigest
}

function Get-OperationsJobName {
    $names = @(az containerapp job list `
            --subscription $SubscriptionId `
            --resource-group $ResourceGroup `
            --query "[?tags.DeploymentInstance=='$DeploymentInstanceId'].name" `
            --output tsv `
            --only-show-errors)
    if ($LASTEXITCODE -ne 0 -or $names.Count -ne 1 -or [string]::IsNullOrWhiteSpace($names[0])) {
        throw "Expected exactly one private operations job for this deployment instance; found $($names.Count)."
    }
    return $names[0].Trim()
}

function Invoke-CatalogJob {
    $reviewedDigest = Get-ReviewedCatalogDigest
    if ($env:RETRIEVAL_CATALOG_DIGEST -ne $reviewedDigest) {
        throw 'RETRIEVAL_CATALOG_DIGEST differs from the reviewed catalog file.'
    }
    $jobName = Get-OperationsJobName
    if (-not $Execute) {
        [ordered]@{ action = 'preview'; jobName = $jobName; catalogDigest = $reviewedDigest } | ConvertTo-Json -Compress
        return
    }
    $executionName = az containerapp job start `
        --subscription $SubscriptionId `
        --resource-group $ResourceGroup `
        --name $jobName `
        --query name `
        --output tsv `
        --only-show-errors
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($executionName)) {
        throw 'Private catalog job failed to start.'
    }
    [ordered]@{ jobName = $jobName; executionName = $executionName.Trim(); catalogDigest = $reviewedDigest } | ConvertTo-Json -Compress
}

function Test-CatalogJob {
    $executionName = Get-RequiredValue 'JobExecutionName' $JobExecutionName
    $jobName = Get-OperationsJobName
    $execution = az containerapp job execution show `
        --subscription $SubscriptionId `
        --resource-group $ResourceGroup `
        --name $jobName `
        --job-execution-name $executionName `
        --query '{name:name,status:properties.status,startTime:properties.startTime,endTime:properties.endTime}' `
        --output json `
        --only-show-errors | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Private catalog job execution could not be read.' }
    if ($execution.status -ne 'Succeeded') {
        throw "Private catalog job has not succeeded; current status: $($execution.status)."
    }
    $execution | ConvertTo-Json -Compress
}

function Remove-OperationsJob {
    $jobName = Get-OperationsJobName
    if (-not $Execute) {
        [ordered]@{ action = 'preview-delete'; jobName = $jobName; deploymentInstanceId = $DeploymentInstanceId } | ConvertTo-Json -Compress
        return
    }
    az containerapp job delete `
        --subscription $SubscriptionId `
        --resource-group $ResourceGroup `
        --name $jobName `
        --yes `
        --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Temporary operations job cleanup failed.' }
    $remaining = @(az containerapp job list `
            --subscription $SubscriptionId `
            --resource-group $ResourceGroup `
            --query "[?tags.DeploymentInstance=='$DeploymentInstanceId'].name" `
            --output tsv `
            --only-show-errors)
    if ($LASTEXITCODE -ne 0 -or $remaining.Count -ne 0) {
        throw 'Temporary operations job still exists after cleanup.'
    }
    [ordered]@{ action = 'deleted'; jobName = $jobName; deploymentInstanceId = $DeploymentInstanceId } | ConvertTo-Json -Compress
}

$authority = Get-Authority
if ($Phase -eq 'Authority') {
    $authority | ConvertTo-Json -Compress
    return
}

Assert-Authority | Out-Null
Assert-Target
Import-AzdEnvironment
Set-ParameterEnvironment

switch ($Phase) {
    'Foundation' { Invoke-InfrastructurePhase -Serving $false -Operations $false }
    'Build' { Invoke-ImageBuild }
    'Operations' { Invoke-OperationsInfrastructure }
    'Catalog' { Invoke-CatalogJob }
    'CatalogVerify' { Test-CatalogJob }
    'OperationsCleanup' { Remove-OperationsJob }
    'Final' { Invoke-InfrastructurePhase -Serving $true -Operations $false }
    'Function' {
        if (-not $Execute) {
            [ordered]@{ action = 'preview'; service = 'rag-functions'; environment = $AzdEnvironment } | ConvertTo-Json -Compress
            return
        }
        azd deploy rag-functions --environment $AzdEnvironment --no-prompt
        if ($LASTEXITCODE -ne 0) { throw 'Function deployment failed.' }
    }
}
