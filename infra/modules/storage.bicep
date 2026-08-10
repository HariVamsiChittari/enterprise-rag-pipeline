// =========================================
// Storage Account Module
// =========================================
// Creates identity-only storage for Azure Functions deployment and Durable state

@description('Storage account name (3-24 lowercase alphanumeric)')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Azure region')
param location string = resourceGroup().location

@description('Storage redundancy: LRS, ZRS, GRS')
@allowed(['LRS', 'ZRS', 'GRS'])
param redundancy string = 'ZRS'

@description('Resource tags')
param tags object = {}

// =========================================
// Resources
// =========================================

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_${redundancy}'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    encryption: {
      services: {
        blob: { enabled: true }
        file: { enabled: true }
        table: { enabled: true }
        queue: { enabled: true }
      }
      keySource: 'Microsoft.Storage'
    }
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
    }
    publicNetworkAccess: 'Disabled'
  }
  tags: tags
}

// Blob service
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

// Queue service
resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

// Table service for Function host state
resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

// Flex Consumption package deployment container
resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'app-package'
  properties: {
    publicAccess: 'None'
  }
}

// =========================================
// Outputs
// =========================================

@description('Resource ID of the storage account')
output storageAccountId string = storageAccount.id

@description('Storage account name')
output storageAccountName string = storageAccount.name

@description('Blob service endpoint')
output blobEndpoint string = storageAccount.properties.primaryEndpoints.blob

@description('Queue service endpoint')
output queueEndpoint string = storageAccount.properties.primaryEndpoints.queue

@description('Table service endpoint')
output tableEndpoint string = storageAccount.properties.primaryEndpoints.table

@description('Flex Consumption deployment container URL')
output deploymentContainerUrl string = '${storageAccount.properties.primaryEndpoints.blob}${deploymentContainer.name}'
