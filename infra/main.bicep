targetScope = 'resourceGroup'

@description('Environment name')
@minLength(1)
param environmentName string

@description('Azure region')
param location string = resourceGroup().location

@description('Existing Azure OpenAI account name')
@minLength(1)
param openAiAccountName string = 'ragpg-dev-split-k6ex5fnwyrghq-openai'

@description('Resource group containing the existing Azure OpenAI account')
@minLength(1)
param openAiResourceGroupName string = resourceGroup().name

@description('Existing text-embedding-3-large deployment name')
param embeddingDeploymentName string = 'text-embedding-3-large'

@description('Existing chat deployment name')
@minLength(1)
param chatDeploymentName string

@description('SharePoint tenant ID')
param sharePointTenantId string

@description('SharePoint application client ID')
param sharePointAppClientId string

@description('Assigned SharePoint document-library drive ID')
param sharePointDriveId string

@description('Stable source registration ID')
param ingestionSourceId string

@description('Key Vault secret containing the exportable SharePoint PFX')
param sharePointCertificateSecretName string = 'sharepoint-app-cert'

@description('Microsoft Entra application client ID protecting operator and query endpoints')
param adminApiClientId string

@secure()
@description('Shared secret for Microsoft Graph webhook clientState validation')
param webhookClientState string

@description('Microsoft Graph service principal object ID (tenant-specific, from az ad sp show --id 00000003-0000-0000-c000-000000000000 --query id)')
param graphServicePrincipalId string

@description('Cosmos DB mode')
@allowed(['serverless', 'provisioned'])
param cosmosDbMode string = 'serverless'

@description('Cosmos DB shared metadata autoscale maximum RU/s in provisioned mode')
@minValue(1000)
param cosmosMetadataAutoscaleMaxRUs int = 1000

@description('Cosmos DB dedicated search-chunks autoscale maximum RU/s in provisioned mode')
@minValue(1000)
param cosmosSearchChunksAutoscaleMaxRUs int = 1000

@description('Storage redundancy')
@allowed(['LRS', 'ZRS', 'GRS'])
param storageRedundancy string = 'ZRS'

@description('Use Document Intelligence F0 for development')
param useDocumentIntelligenceFreeTier bool = false

@description('Use Azure AI Language F0 for development')
param useLanguageFreeTier bool = false

@description('Deploy AKS cluster (true for prod, false for dev/ACA)')
param deployAks bool = false

@description('Application Insights daily cap in GB; -1 means unlimited')
param applicationInsightsDailyCapGb int = -1

@description('Enable ACL filtering in the retrieval service')
param aclEnabled bool = true

@description('Resource tags')
param tags object = {
  Environment: environmentName
  Project: 'RAG-SharePoint'
  ManagedBy: 'azd'
}

var suffix = take(uniqueString(resourceGroup().id, environmentName), 8)
var prefix = 'rag-${environmentName}'
var storageName = take('st${replace(prefix, '-', '')}${suffix}', 24)
var openAiEndpoint = 'https://${openAiAccountName}.openai.azure.com/'

module monitoring './modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    logAnalyticsName: '${prefix}-logs'
    applicationInsightsName: '${prefix}-ai'
    location: location
    dailyQuotaGb: applicationInsightsDailyCapGb
    tags: tags
  }
}

module storage './modules/storage.bicep' = {
  name: 'storage'
  params: {
    storageAccountName: storageName
    location: location
    redundancy: storageRedundancy
    tags: tags
  }
}

module keyVault './modules/keyvault.bicep' = {
  name: 'key-vault'
  params: {
    keyVaultName: take('${prefix}-kv2-${suffix}', 24)
    location: location
    enablePurgeProtection: false
    tags: tags
  }
}

module identity './modules/identity.bicep' = {
  name: 'identity'
  params: {
    identityName: '${prefix}-functions-mi'
    location: location
    tags: tags
  }
}

module cosmos './modules/cosmos.bicep' = {
  name: 'cosmos'
  params: {
    cosmosAccountName: take('${prefix}-cosmos-${suffix}', 44)
    location: location
    mode: cosmosDbMode
    metadataAutoscaleMaxThroughput: cosmosMetadataAutoscaleMaxRUs
    searchChunksAutoscaleMaxThroughput: cosmosSearchChunksAutoscaleMaxRUs
    tags: tags
  }
}

module durableTask './modules/durable-task.bicep' = {
  params: {
    schedulerName: take('${prefix}-dts-${suffix}', 45)
    location: location
    functionAppPrincipalId: identity.outputs.identityPrincipalId
    // Goal 7: independent scaling per source -- each source's azd deployment provisions
    // its own scheduler (per confirmed answer), so the task hub is named from the
    // source rather than the misleading hardcoded 'full-sync' literal.
    taskHubName: take('${ingestionSourceId}-sync', 45)
    tags: tags
  }
}

module documentIntelligence './modules/ai-services.bicep' = {
  name: 'document-intelligence'
  params: {
    documentIntelligenceName: '${prefix}-doc-intel-${suffix}'
    languageServiceName: '${prefix}-lang-${suffix}'
    location: location
    useFreeF0: useDocumentIntelligenceFreeTier
    useLanguageFreeTier: useLanguageFreeTier
    tags: tags
  }
}

module networking './modules/networking.bicep' = {
  name: 'networking'
  params: {
    virtualNetworkName: '${prefix}-vnet'
    location: location
    privateEndpointTargets: [
      {
        name: 'storage-blob'
        resourceId: storage.outputs.storageAccountId
        groupId: 'blob'
        dnsZoneName: 'privatelink.blob.${environment().suffixes.storage}'
      }
      {
        name: 'storage-queue'
        resourceId: storage.outputs.storageAccountId
        groupId: 'queue'
        dnsZoneName: 'privatelink.queue.${environment().suffixes.storage}'
      }
      {
        name: 'storage-table'
        resourceId: storage.outputs.storageAccountId
        groupId: 'table'
        dnsZoneName: 'privatelink.table.${environment().suffixes.storage}'
      }
      {
        name: 'cosmos-sql'
        resourceId: cosmos.outputs.cosmosAccountId
        groupId: 'Sql'
        dnsZoneName: 'privatelink.documents.azure.com'
      }
      {
        name: 'key-vault'
        resourceId: keyVault.outputs.keyVaultId
        groupId: 'vault'
        dnsZoneName: 'privatelink.vaultcore.azure.net'
      }
      {
        name: 'document-intelligence'
        resourceId: documentIntelligence.outputs.documentIntelligenceId
        groupId: 'account'
        dnsZoneName: 'privatelink.cognitiveservices.azure.com'
      }
      {
        name: 'language-service'
        resourceId: documentIntelligence.outputs.languageServiceId
        groupId: 'account'
        dnsZoneName: 'privatelink.cognitiveservices.azure.com'
      }
    ]
    tags: tags
  }
}

module functions './modules/functions.bicep' = {
  name: 'functions'
  params: {
    functionAppName: '${prefix}-func-${suffix}'
    location: location
    managedIdentityId: identity.outputs.identityId
    managedIdentityClientId: identity.outputs.identityClientId
    storageAccountName: storage.outputs.storageAccountName
    deploymentStorageContainerUrl: storage.outputs.deploymentContainerUrl
    integrationSubnetId: networking.outputs.integrationSubnetId
    appInsightsConnectionString: monitoring.outputs.connectionString
    cosmosEndpoint: cosmos.outputs.endpoint
    cosmosDatabaseName: cosmos.outputs.databaseName
    cosmosContainerNames: {
      ingestionRuns: cosmos.outputs.ingestionRunsContainerName
      sourceDocuments: cosmos.outputs.sourceDocumentsContainerName
      searchChunks: cosmos.outputs.searchChunksContainerName
    }
    durableTaskSchedulerEndpoint: durableTask.outputs.endpoint
    durableTaskHubName: durableTask.outputs.taskHubName
    openAiEndpoint: openAiEndpoint
    embeddingDeploymentName: embeddingDeploymentName
    chatDeploymentName: chatDeploymentName
    documentIntelligenceEndpoint: documentIntelligence.outputs.documentIntelligenceEndpoint
    languageEndpoint: documentIntelligence.outputs.languageServiceEndpoint
    keyVaultUri: keyVault.outputs.keyVaultUri
    entraTenantId: sharePointTenantId
    sharePointAppClientId: sharePointAppClientId
    ingestionSourceId: ingestionSourceId
    sharePointDriveId: sharePointDriveId
    sharePointCertificateSecretName: sharePointCertificateSecretName
    adminApiClientId: adminApiClientId
    webhookClientState: webhookClientState
    retrievalServiceUrl: deployAks ? '' : aca.outputs.internalUrl
    tags: tags
  }
}

module rbac './modules/rbac.bicep' = {
  name: 'rbac'
  params: {
    principalId: identity.outputs.identityPrincipalId
    cosmosAccountId: cosmos.outputs.cosmosAccountId
    storageAccountId: storage.outputs.storageAccountId
    keyVaultId: keyVault.outputs.keyVaultId
    documentIntelligenceId: documentIntelligence.outputs.documentIntelligenceId
    languageServiceId: documentIntelligence.outputs.languageServiceId
    applicationInsightsId: monitoring.outputs.applicationInsightsId
  }
}

module openAiRbac './modules/openai-rbac.bicep' = {
  name: 'openai-rbac'
  scope: resourceGroup(openAiResourceGroupName)
  params: {
    principalId: identity.outputs.identityPrincipalId
    openAiAccountName: openAiAccountName
  }
}

module graphRbac './modules/graph-rbac.bicep' = {
  name: 'graph-rbac'
  params: {
    principalId: identity.outputs.identityPrincipalId
    graphServicePrincipalId: graphServicePrincipalId
  }
}

// --- AKS Retrieval Agent (prod only) ---

module aks './modules/aks.bicep' = if (deployAks) {
  name: 'aks'
  params: {
    clusterName: '${prefix}-aks-${suffix}'
    location: location
    vnetId: networking.outputs.virtualNetworkId
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsId
    tags: tags
  }
}

module acr './modules/acr.bicep' = {
  name: 'acr'
  params: {
    registryName: take('${replace(prefix, '-', '')}acr${suffix}', 50)
    location: location
    kubeletPrincipalId: deployAks ? aks.outputs.kubeletIdentityObjectId : ''
    appIdentityPrincipalId: identity.outputs.identityPrincipalId
    tags: tags
  }
}

module aksIdentity './modules/aks-identity.bicep' = if (deployAks) {
  name: 'aks-identity'
  params: {
    identityName: '${prefix}-aks-retrieval-mi'
    location: location
    oidcIssuerUrl: aks.outputs.oidcIssuerUrl
    cosmosAccountId: cosmos.outputs.cosmosAccountId
    cosmosDatabaseName: cosmos.outputs.databaseName
    serviceAuditContainerName: cosmos.outputs.serviceAuditContainerName
    openAiAccountName: openAiAccountName
    openAiResourceGroupName: openAiResourceGroupName
    graphServicePrincipalId: graphServicePrincipalId
    tags: tags
  }
}

// --- ACA Retrieval Agent (dev, when AKS not deployed) ---

module retrievalConfig './modules/retrieval-config.bicep' = {
  name: 'retrieval-config'
  params: {
    cosmosEndpoint: cosmos.outputs.endpoint
    cosmosDatabaseName: cosmos.outputs.databaseName
    openAiEndpoint: openAiEndpoint
    chatDeploymentName: chatDeploymentName
    embeddingDeploymentName: embeddingDeploymentName
    tenantId: sharePointTenantId
    managedIdentityClientId: deployAks ? aksIdentity.outputs.identityClientId : identity.outputs.identityClientId
    aclEnabled: aclEnabled
    appInsightsConnectionString: monitoring.outputs.connectionString
  }
}

module aca './modules/aca.bicep' = if (!deployAks) {
  name: 'aca'
  params: {
    containerAppName: '${prefix}-retrieval-${suffix}'
    location: location
    acrLoginServer: acr.outputs.loginServer
    managedIdentityId: identity.outputs.identityId
    infrastructureSubnetId: networking.outputs.acaSubnetId
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsId
    retrievalEnvVars: retrievalConfig.outputs.envVars
    tags: tags
  }
}

module acaDns './modules/aca-dns.bicep' = if (!deployAks) {
  name: 'aca-dns'
  params: {
    domainName: aca.outputs.environmentDefaultDomain
    staticIp: aca.outputs.environmentStaticIp
    virtualNetworkId: networking.outputs.virtualNetworkId
    tags: tags
  }
}

output functionAppName string = functions.outputs.functionAppName
output functionAppUrl string = functions.outputs.functionAppUrl
output managedIdentityClientId string = identity.outputs.identityClientId
output keyVaultName string = keyVault.outputs.keyVaultName
output cosmosDbEndpoint string = cosmos.outputs.endpoint
output cosmosDbDatabaseName string = cosmos.outputs.databaseName
output documentIntelligenceEndpoint string = documentIntelligence.outputs.documentIntelligenceEndpoint
output openAiEndpoint string = openAiEndpoint
output acrLoginServer string = acr.outputs.loginServer
output aksClusterName string = deployAks ? aks.outputs.clusterName : ''
output aksRetrievalIdentityClientId string = deployAks ? aksIdentity.outputs.identityClientId : ''
output retrievalServiceUrl string = deployAks ? '' : aca.outputs.internalUrl
output retrievalConfigMap object = retrievalConfig.outputs.configMap
