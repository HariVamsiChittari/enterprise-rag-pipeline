@description('Principal ID to assign OpenAI User role')
param principalId string

@description('Azure OpenAI account name')
param openAiAccountName string

resource openAi 'Microsoft.CognitiveServices/accounts@2024-06-01-preview' existing = {
  name: openAiAccountName
}

var cognitiveServicesOpenAIUserRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
)

resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAi.id, principalId, cognitiveServicesOpenAIUserRole)
  scope: openAi
  properties: {
    roleDefinitionId: cognitiveServicesOpenAIUserRole
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
