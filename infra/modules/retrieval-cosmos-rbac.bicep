@description('Retrieval managed identity principal ID')
param principalId string

@description('Cosmos account resource ID')
param cosmosAccountId string

@description('Cosmos database name')
param cosmosDatabaseName string

@description('Audit container name')
param serviceAuditContainerName string

@description('Search chunks container name')
param searchChunksContainerName string

@description('Source documents container name')
param sourceDocumentsContainerName string

@description('Retrieval catalog container name')
param retrievalConfigContainerName string

var readableContainerNames = [
  searchChunksContainerName
  sourceDocumentsContainerName
  retrievalConfigContainerName
]

resource cosmosDataReaders 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = [
  for containerName in readableContainerNames: {
    name: '${last(split(cosmosAccountId, '/'))}/${guid(principalId, cosmosAccountId, 'retrieval-reader', containerName)}'
    properties: {
      roleDefinitionId: '${cosmosAccountId}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000001'
      principalId: principalId
      scope: '${cosmosAccountId}/dbs/${cosmosDatabaseName}/colls/${containerName}'
    }
  }
]

resource cosmosAuditWriter 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  name: '${last(split(cosmosAccountId, '/'))}/${guid(principalId, cosmosAccountId, 'retrieval-audit-writer')}'
  properties: {
    roleDefinitionId: '${cosmosAccountId}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    principalId: principalId
    scope: '${cosmosAccountId}/dbs/${cosmosDatabaseName}/colls/${serviceAuditContainerName}'
  }
}
