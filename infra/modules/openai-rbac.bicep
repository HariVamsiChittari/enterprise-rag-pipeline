targetScope = 'resourceGroup'

@description('Function App managed identity principal ID')
param principalId string

@description('Existing Azure OpenAI account name')
param openAiAccountName string

@description('Skip if role assignment already exists from a prior deployment')
param roleAssignmentExists bool = false

var cognitiveServicesOpenAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource openAiAccount 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: openAiAccountName
}

resource openAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!roleAssignmentExists) {
  name: guid(openAiAccount.id, principalId, cognitiveServicesOpenAiUserRoleId)
  scope: openAiAccount
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesOpenAiUserRoleId
    )
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
