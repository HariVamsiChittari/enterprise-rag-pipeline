extension 'br:mcr.microsoft.com/bicep/extensions/microsoftgraph/v1.0:1.0.0'

@description('Managed Identity name for AKS workload')
param identityName string

@description('Azure region')
param location string = resourceGroup().location

@description('AKS OIDC issuer URL')
param oidcIssuerUrl string

@description('Kubernetes namespace for the retrieval agent')
param k8sNamespace string = 'default'

@description('Kubernetes service account name')
param k8sServiceAccountName string = 'retrieval-agent-sa'

@description('Cosmos DB account resource ID')
param cosmosAccountId string

@description('Cosmos DB database name')
param cosmosDatabaseName string

@description('Service audit container name (retrieval identity needs write access here only)')
param serviceAuditContainerName string

@description('Azure OpenAI account name')
param openAiAccountName string

@description('Resource group containing the Azure OpenAI account')
param openAiResourceGroupName string

@description('Microsoft Graph service principal object ID (tenant-specific)')
param graphServicePrincipalId string

@description('Resource tags')
param tags object = {}

// Microsoft Graph application role IDs (stable across all tenants)
var graphAppRoles = {
  groupMemberReadAll: '98830695-27a2-44f7-8c18-0c3ebc9698f6'
  userReadAll: 'df021288-bdef-4463-88db-98f22de89214'
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-07-31-preview' = {
  name: identityName
  location: location
  tags: tags
}

resource federatedCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-07-31-preview' = {
  parent: identity
  name: 'aks-retrieval-agent'
  properties: {
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:${k8sNamespace}:${k8sServiceAccountName}'
    audiences: ['api://AzureADTokenExchange']
  }
}

// Cosmos DB Built-in Data Reader (00000000-0000-0000-0000-000000000001)
resource cosmosDataReader 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  name: '${last(split(cosmosAccountId, '/'))}/${guid(identity.id, cosmosAccountId, 'cosmos-reader')}'
  properties: {
    roleDefinitionId: '${cosmosAccountId}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000001'
    principalId: identity.properties.principalId
    scope: cosmosAccountId
  }
}

// Cosmos DB Built-in Data Contributor (00000000-0000-0000-0000-000000000002), scoped ONLY to the
// service-audit container so the retrieval identity cannot write to search-chunks/source-documents.
resource cosmosAuditWriter 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  name: '${last(split(cosmosAccountId, '/'))}/${guid(identity.id, cosmosAccountId, 'cosmos-audit-writer')}'
  properties: {
    roleDefinitionId: '${cosmosAccountId}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    principalId: identity.properties.principalId
    scope: '${cosmosAccountId}/dbs/${cosmosDatabaseName}/colls/${serviceAuditContainerName}'
  }
}

module openAiRoleAssignment 'openai-rbac-aks.bicep' = {
  name: 'aks-openai-rbac'
  scope: resourceGroup(openAiResourceGroupName)
  params: {
    principalId: identity.properties.principalId
    openAiAccountName: openAiAccountName
  }
}

// Microsoft Graph app role assignments for group resolution via /users/{id}/transitiveMemberOf
resource graphGroupMemberReadAll 'Microsoft.Graph/appRoleAssignedTo@v1.0' = {
  appRoleId: graphAppRoles.groupMemberReadAll
  principalId: identity.properties.principalId
  resourceId: graphServicePrincipalId
}

resource graphUserReadAll 'Microsoft.Graph/appRoleAssignedTo@v1.0' = {
  appRoleId: graphAppRoles.userReadAll
  principalId: identity.properties.principalId
  resourceId: graphServicePrincipalId
}

output identityClientId string = identity.properties.clientId
output identityPrincipalId string = identity.properties.principalId
output identityId string = identity.id
