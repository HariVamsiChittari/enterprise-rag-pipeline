@description('Container Registry name')
param registryName string

@description('Azure region')
param location string = resourceGroup().location

@description('AKS kubelet identity principal ID for AcrPull')
param kubeletPrincipalId string

@description('Functions/ACA managed identity principal ID for AcrPull')
param appIdentityPrincipalId string = ''

@description('Resource tags')
param tags object = {}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

var acrPullRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)

resource kubeletAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(kubeletPrincipalId)) {
  name: guid(acr.id, kubeletPrincipalId, acrPullRole)
  scope: acr
  properties: {
    roleDefinitionId: acrPullRole
    principalId: kubeletPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource appAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(appIdentityPrincipalId)) {
  name: guid(acr.id, appIdentityPrincipalId, acrPullRole)
  scope: acr
  properties: {
    roleDefinitionId: acrPullRole
    principalId: appIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output registryName string = acr.name
output loginServer string = acr.properties.loginServer
