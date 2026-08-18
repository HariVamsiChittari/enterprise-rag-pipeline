@description('Cosmos DB account name (3-44 lowercase alphanumeric characters and hyphens)')
@minLength(3)
@maxLength(44)
param cosmosAccountName string

@description('Azure region')
param location string = resourceGroup().location

@description('Cosmos DB capacity mode')
@allowed(['serverless', 'provisioned'])
param mode string = 'serverless'

@description('Shared autoscale maximum RU/s for metadata containers in provisioned mode')
@minValue(1000)
param metadataAutoscaleMaxThroughput int = 1000

@description('Dedicated autoscale maximum RU/s for search-chunks in provisioned mode')
@minValue(1000)
param searchChunksAutoscaleMaxThroughput int = 1000

@description('Resource tags')
param tags object = {}

@description('Database name')
param databaseName string = 'rag-db'

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2025-05-01-preview' = {
  name: cosmosAccountName
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    capacityMode: mode == 'serverless' ? 'Serverless' : 'Provisioned'
    capabilities: [
      { name: 'EnableNoSQLVectorSearch' }
      { name: 'EnableNoSQLFullTextSearch' }
    ]
    enableAutomaticFailover: false
    enableMultipleWriteLocations: false
    isVirtualNetworkFilterEnabled: true
    publicNetworkAccess: 'Disabled'
    disableLocalAuth: true
  }
  tags: tags
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2025-05-01-preview' = {
  parent: cosmosAccount
  name: databaseName
  properties: mode == 'serverless'
    ? {
        resource: {
          id: databaseName
        }
      }
    : {
        resource: {
          id: databaseName
        }
        options: {
          autoscaleSettings: {
            maxThroughput: metadataAutoscaleMaxThroughput
          }
        }
      }
}

resource ingestionRunsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-05-01-preview' = {
  parent: database
  name: 'ingestion-runs'
  properties: {
    resource: {
      id: 'ingestion-runs'
      partitionKey: {
        paths: ['/sourceId']
        kind: 'Hash'
        version: 2
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          { path: '/runId/?' }
          { path: '/status/?' }
          { path: '/stage/?' }
          { path: '/startedAt/?' }
          { path: '/completedAt/?' }
        ]
        excludedPaths: [
          { path: '/*' }
        ]
      }
    }
    options: {}
  }
}

resource sourceDocumentsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-05-01-preview' = {
  parent: database
  name: 'source-documents'
  properties: {
    resource: {
      id: 'source-documents'
      partitionKey: {
        paths: ['/sourceRunId']
        kind: 'Hash'
        version: 2
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          { path: '/runId/?' }
          { path: '/status/?' }
          { path: '/stage/?' }
          { path: '/documentId/?' }
          { path: '/discoveryOrdinal/?' }
          { path: '/itemId/?' }
          { path: '/discoveredAt/?' }
          { path: '/processingStartedAt/?' }
          { path: '/readyAt/?' }
          { path: '/failedAt/?' }
          { path: '/updatedAt/?' }
        ]
        excludedPaths: [
          { path: '/*' }
        ]
        compositeIndexes: [
          [
            {
              path: '/status'
              order: 'ascending'
            }
            {
              path: '/discoveryOrdinal'
              order: 'ascending'
            }
          ]
        ]
      }
    }
    options: {}
  }
}

resource searchChunksContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-05-01-preview' = {
  parent: database
  name: 'search-chunks'
  properties: {
    resource: {
      id: 'search-chunks'
      partitionKey: {
        paths: ['/documentKey']
        kind: 'Hash'
        version: 2
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          { path: '/sourceId/?' }
          { path: '/runId/?' }
          { path: '/sourceRunId/?' }
          { path: '/documentId/?' }
          { path: '/documentKey/?' }
          { path: '/allowedGroupIds/[]/?' }
          { path: '/pageStart/?' }
          { path: '/pageEnd/?' }
          { path: '/chunkIndex/?' }
          { path: '/createdAt/?' }
        ]
        excludedPaths: [
          { path: '/*' }
          { path: '/embedding/*' }
        ]
        #disable-next-line BCP037
        fullTextIndexes: [
          { path: '/content' }
          { path: '/searchableText' }
        ]
        vectorIndexes: [
          {
            path: '/embedding'
            type: 'diskANN'
          }
        ]
      }
      fullTextPolicy: {
        defaultLanguage: 'en-US'
        fullTextPaths: [
          {
            path: '/content'
            language: 'en-US'
          }
          {
            path: '/searchableText'
            language: 'en-US'
          }
        ]
      }
      vectorEmbeddingPolicy: {
        vectorEmbeddings: [
          {
            path: '/embedding'
            dataType: 'float32'
            dimensions: 3072
            distanceFunction: 'cosine'
          }
        ]
      }
    }
    options: mode == 'serverless'
      ? {}
      : {
          autoscaleSettings: {
            maxThroughput: searchChunksAutoscaleMaxThroughput
          }
        }
  }
}

resource serviceAuditContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-05-01-preview' = {
  parent: database
  name: 'service-audit'
  properties: {
    resource: {
      id: 'service-audit'
      // Item ID as partition key: write-heavy, unique-per-record audit log (Cosmos DB partitioning best practice)
      partitionKey: {
        paths: ['/id']
        kind: 'Hash'
        version: 2
      }
      defaultTtl: 7776000 // 90 days in seconds
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          { path: '/*' }
        ]
        excludedPaths: []
      }
    }
    options: {}
  }
}

@description('Cosmos DB account ID')
output cosmosAccountId string = cosmosAccount.id

@description('Cosmos DB account name')
output cosmosAccountName string = cosmosAccount.name

@description('Cosmos DB endpoint')
output endpoint string = cosmosAccount.properties.documentEndpoint

@description('Database name')
output databaseName string = database.name

@description('Ingestion runs container name')
output ingestionRunsContainerName string = ingestionRunsContainer.name

@description('Source documents container name')
output sourceDocumentsContainerName string = sourceDocumentsContainer.name

@description('Search chunks container name')
output searchChunksContainerName string = searchChunksContainer.name

@description('Service audit container name')
output serviceAuditContainerName string = serviceAuditContainer.name
