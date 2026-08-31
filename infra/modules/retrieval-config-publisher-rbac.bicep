@description('Deployment publisher principal ID; empty disables assignment')
param publisherPrincipalId string = ''

@description('Cosmos account resource ID')
param cosmosAccountId string

@description('Cosmos database name')
param cosmosDatabaseName string

@description('Retrieval configuration container name')
param retrievalConfigContainerName string

resource publisherWriter 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = if (!empty(publisherPrincipalId)) {
  name: '${last(split(cosmosAccountId, '/'))}/${guid(publisherPrincipalId, cosmosAccountId, retrievalConfigContainerName)}'
  properties: {
    roleDefinitionId: '${cosmosAccountId}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    principalId: publisherPrincipalId
    scope: '${cosmosAccountId}/dbs/${cosmosDatabaseName}/colls/${retrievalConfigContainerName}'
  }
}
