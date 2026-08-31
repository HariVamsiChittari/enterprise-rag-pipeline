<#
.SYNOPSIS
    Generate Kubernetes configmap.yaml from Bicep deployment outputs.
.DESCRIPTION
    Reads the 'retrievalConfigMap' output from the last Bicep deployment and
    writes the canonical configmap.yaml for AKS. Ensures ACA and AKS always
    use identical configuration from infra/modules/retrieval-config.bicep.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$ResourceGroup,
    [string]$DeploymentName = "main",
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"

if (-not $OutputPath) {
    $OutputPath = Join-Path $PSScriptRoot ".." "app" "retrieval" "kubernetes" "configmap.yaml"
}

Write-Host "Reading deployment outputs from $ResourceGroup/$DeploymentName..."
$configMap = az deployment group show `
    --resource-group $ResourceGroup `
    --name $DeploymentName `
    --query "properties.outputs.retrievalConfigMap.value" `
    -o json 2>&1 | ConvertFrom-Json

if (-not $configMap) {
    throw "Could not read retrievalConfigMap output. Has the deployment run?"
}

$yaml = @"
## Generated from infra/modules/retrieval-config.bicep output.
## Run: scripts/generate-k8s-configmap.ps1 to regenerate from deployment outputs.
## Do NOT edit individual values here — update retrieval-config.bicep instead.
apiVersion: v1
kind: ConfigMap
metadata:
  name: retrieval-config
data:
"@

$configMap.PSObject.Properties | Sort-Object Name | ForEach-Object {
    $yaml += "`n  $($_.Name): `"$($_.Value)`""
}

Set-Content -Path $OutputPath -Value $yaml -Encoding utf8NoBOM
Write-Host "Written: $OutputPath ($($configMap.PSObject.Properties.Count) keys)"
