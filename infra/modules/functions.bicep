// =========================================
// Azure Functions Module
// =========================================
// Creates Azure Functions Flex Consumption plan + Function App for ingestion pipeline

@description('Function App name')
param functionAppName string

@description('Azure region')
param location string = resourceGroup().location

@description('Instance memory in MB: 512, 1024, 2048, 4096')
@allowed([512, 1024, 2048, 4096])
param instanceMemoryMB int = 2048

@description('Maximum Flex Consumption instance count')
@minValue(40)
@maxValue(1000)
param maximumInstanceCount int = 40

@description('Managed identity resource ID')
param managedIdentityId string

@description('Managed identity client ID')
param managedIdentityClientId string

@description('Storage account name for Functions host')
param storageAccountName string

@description('Blob container URL used for Flex Consumption package deployment')
param deploymentStorageContainerUrl string

@description('Resource ID of the Flex Consumption VNet integration subnet')
param integrationSubnetId string

@description('Application Insights connection string')
@secure()
param appInsightsConnectionString string

@description('Cosmos DB endpoint')
param cosmosEndpoint string

@description('Cosmos DB database name')
param cosmosDatabaseName string

@description('Cosmos DB ingestion container names')
param cosmosContainerNames object

@description('Durable Task Scheduler endpoint')
param durableTaskSchedulerEndpoint string

@description('Durable task hub name')
param durableTaskHubName string

@description('Azure OpenAI endpoint')
param openAiEndpoint string

@description('Azure OpenAI embedding deployment name')
param embeddingDeploymentName string

@description('Azure OpenAI chat deployment name')
param chatDeploymentName string

@description('Document Intelligence endpoint')
param documentIntelligenceEndpoint string

@description('Azure AI Language endpoint')
param languageEndpoint string

@description('Key Vault URI')
param keyVaultUri string

@description('Microsoft Entra tenant ID used by SharePoint and Function authentication')
param entraTenantId string

@description('SharePoint application client ID')
param sharePointAppClientId string

@description('Stable source registration ID')
param ingestionSourceId string

@description('Assigned SharePoint document-library drive ID')
param sharePointDriveId string

@description('Key Vault secret name containing the exportable SharePoint PFX')
param sharePointCertificateSecretName string

@description('Microsoft Entra application client ID protecting operator endpoints')
param adminApiClientId string

@description('NCRONTAB schedule for the delta-sync timer (Goal 8 incremental add/update/delete)')
param deltaSyncSchedule string = '0 */15 * * * *'

@description('NCRONTAB schedule for the ACL-resync timer (Goal 6b)')
param aclResyncSchedule string = '0 0 3 * * *'

@description('Page size for each ACL-resync activity call')
@minValue(1)
@maxValue(100)
param aclResyncPageSize int = 50

@description('Internal URL of the retrieval service for query proxy')
param retrievalServiceUrl string = ''

@description('Resource tags')
param tags object = {}

// =========================================
// Resources
// =========================================

// Flex Consumption hosting plan
#disable-next-line BCP081
resource hostingPlan 'Microsoft.Web/serverfarms@2025-03-01' = {
  name: '${functionAppName}-plan'
  location: location
  kind: 'functionapp'
  sku: {
    name: 'FC1' // Flex Consumption
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true // Linux
  }
  tags: tags
}

// Function App
#disable-next-line BCP081
resource functionApp 'Microsoft.Web/sites@2025-03-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  tags: union(tags, {
    'azd-service-name': 'rag-functions'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityId}': {}
    }
  }
  properties: {
    serverFarmId: hostingPlan.id
    siteConfig: {
      appSettings: [
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'AzureWebJobsStorage__blobServiceUri'
          value: 'https://${storageAccountName}.blob.${environment().suffixes.storage}'
        }
        {
          name: 'AzureWebJobsStorage__queueServiceUri'
          value: 'https://${storageAccountName}.queue.${environment().suffixes.storage}'
        }
        {
          name: 'AzureWebJobsStorage__tableServiceUri'
          value: 'https://${storageAccountName}.table.${environment().suffixes.storage}'
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'AzureWebJobsStorage__clientId'
          value: managedIdentityClientId
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsightsConnectionString
        }
        {
          name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING'
          value: 'ClientId=${managedIdentityClientId};Authorization=AAD'
        }
        {
          name: 'AZURE_CLIENT_ID'
          value: managedIdentityClientId
        }
        {
          name: 'COSMOS_ENDPOINT'
          value: cosmosEndpoint
        }
        {
          name: 'COSMOS_DATABASE_NAME'
          value: cosmosDatabaseName
        }
        {
          name: 'COSMOS_INGESTION_RUNS_CONTAINER_NAME'
          value: cosmosContainerNames.ingestionRuns
        }
        {
          name: 'COSMOS_SOURCE_DOCUMENTS_CONTAINER_NAME'
          value: cosmosContainerNames.sourceDocuments
        }
        {
          name: 'COSMOS_SEARCH_CHUNKS_CONTAINER_NAME'
          value: cosmosContainerNames.searchChunks
        }
        {
          name: 'DURABLE_TASK_SCHEDULER_CONNECTION_STRING'
          value: 'Endpoint=${durableTaskSchedulerEndpoint};Authentication=ManagedIdentity;ClientID=${managedIdentityClientId}'
        }
        {
          name: 'TASKHUB_NAME'
          value: durableTaskHubName
        }
        {
          name: 'OPENAI_ENDPOINT'
          value: openAiEndpoint
        }
        {
          name: 'OPENAI_EMBEDDING_DEPLOYMENT_NAME'
          value: embeddingDeploymentName
        }
        {
          name: 'OPENAI_CHAT_DEPLOYMENT_NAME'
          value: chatDeploymentName
        }
        {
          name: 'DOCUMENT_INTELLIGENCE_ENDPOINT'
          value: documentIntelligenceEndpoint
        }
        {
          name: 'AZURE_LANGUAGE_ENDPOINT'
          value: languageEndpoint
        }
        {
          name: 'KEY_VAULT_URI'
          value: keyVaultUri
        }
        {
          name: 'SHAREPOINT_TENANT_ID'
          value: entraTenantId
        }
        {
          name: 'SHAREPOINT_APP_CLIENT_ID'
          value: sharePointAppClientId
        }
        {
          name: 'INGESTION_SOURCE_ID'
          value: ingestionSourceId
        }
        {
          name: 'SHAREPOINT_ASSIGNED_DRIVE_ID'
          value: sharePointDriveId
        }
        {
          name: 'SHAREPOINT_CERTIFICATE_SECRET_NAME'
          value: sharePointCertificateSecretName
        }
        {
          name: 'FUNCTION_PUBLIC_BASE_URL'
          value: 'https://${functionAppName}.azurewebsites.net'
        }
        {
          name: 'FULL_SYNC_APP_ROLE'
          value: 'Rag.FullSync'
        }
        {
          name: 'DELTA_SYNC_SCHEDULE'
          value: deltaSyncSchedule
        }
        {
          name: 'ACL_RESYNC_SCHEDULE'
          value: aclResyncSchedule
        }
        {
          name: 'ACL_RESYNC_PAGE_SIZE'
          value: string(aclResyncPageSize)
        }
        {
          name: 'CHUNK_MAX_TOKENS'
          value: '800'
        }
        {
          name: 'CHUNK_OVERLAP_TOKENS'
          value: '100'
        }
        {
          name: 'ACL_MAX_PAGES'
          value: '10'
        }
        {
          name: 'DOWNLOAD_TIMEOUT_SECONDS'
          value: '120'
        }
        {
          name: 'DELTA_MAX_PAGES'
          value: '200'
        }
        {
          name: 'EMBEDDING_BATCH_SIZE'
          value: '100'
        }
        {
          name: 'MAX_PDF_PAGES'
          value: '500'
        }
        {
          name: 'QUERY_PROXY_TIMEOUT_SECONDS'
          value: '30'
        }
        {
          name: 'RETRIEVAL_SERVICE_URL'
          value: retrievalServiceUrl
        }
        {
          name: 'INSTANCE_MEMORY_MB'
          value: string(instanceMemoryMB)
        }
      ]
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      use32BitWorkerProcess: false
      cors: {
        allowedOrigins: []
      }
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: deploymentStorageContainerUrl
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: managedIdentityId
          }
        }
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
      scaleAndConcurrency: {
        maximumInstanceCount: maximumInstanceCount
        instanceMemoryMB: instanceMemoryMB
      }
    }
    httpsOnly: true
    keyVaultReferenceIdentity: managedIdentityId
    publicNetworkAccess: 'Enabled'
    virtualNetworkSubnetId: integrationSubnetId
  }
}

#disable-next-line BCP081
resource authSettings 'Microsoft.Web/sites/config@2025-03-01' = {
  parent: functionApp
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'Return401'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: adminApiClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            'api://${adminApiClientId}'
          ]
        }
      }
    }
    httpSettings: {
      requireHttps: true
    }
  }
}

// =========================================
// Outputs
// =========================================

@description('Function App resource ID')
output functionAppId string = functionApp.id

@description('Function App name')
output functionAppName string = functionApp.name

@description('Function App URL')
output functionAppUrl string = 'https://${functionApp.properties.defaultHostName}'
