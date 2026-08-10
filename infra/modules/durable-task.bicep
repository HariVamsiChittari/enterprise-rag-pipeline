@description('Durable Task Scheduler name')
param schedulerName string

@description('Azure region')
param location string = resourceGroup().location

@description('Function App managed identity principal ID')
param functionAppPrincipalId string

@description('Durable task hub name')
param taskHubName string = 'full-sync'

@description('Resource tags')
param tags object = {}

var durableTaskDataContributorRoleId = '0ad04412-c4d5-4796-b79c-f76d14c8d402'

#disable-next-line BCP081
resource scheduler 'Microsoft.DurableTask/schedulers@2025-11-01' = {
  name: schedulerName
  location: location
  properties: {
    sku: {
      name: 'Consumption'
    }
    ipAllowlist: [
      '0.0.0.0/0'
    ]
  }
  tags: tags
}

#disable-next-line BCP081
resource taskHub 'Microsoft.DurableTask/schedulers/taskHubs@2025-11-01' = {
  parent: scheduler
  name: taskHubName
  properties: {}
}

resource durableTaskDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(taskHub.id, functionAppPrincipalId, durableTaskDataContributorRoleId)
  scope: taskHub
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      durableTaskDataContributorRoleId
    )
    principalId: functionAppPrincipalId
    principalType: 'ServicePrincipal'
  }
}

@description('Durable Task Scheduler resource ID')
output schedulerId string = scheduler.id

@description('Durable Task Scheduler endpoint')
output endpoint string = scheduler.properties.endpoint

@description('Durable task hub name')
output taskHubName string = taskHub.name
