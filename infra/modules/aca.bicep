@description('Container App name')
param containerAppName string

@description('Azure region')
param location string = resourceGroup().location

@description('ACR login server (e.g. myacr.azurecr.io)')
param acrLoginServer string

@description('Container image name and tag')
param imageName string = 'retrieval-agent:latest'

@description('User-assigned managed identity resource ID for the container app')
param managedIdentityId string

@description('User-assigned managed identity client ID')
param managedIdentityClientId string

@description('VNet integration subnet resource ID')
param infrastructureSubnetId string

@description('Log Analytics workspace resource ID')
param logAnalyticsWorkspaceId string

@description('Cosmos DB endpoint')
param cosmosEndpoint string

@description('Cosmos DB database name')
param cosmosDatabaseName string

@description('Azure OpenAI endpoint')
param openAiEndpoint string

@description('Chat deployment name')
param chatDeploymentName string

@description('Embedding deployment name')
param embeddingDeploymentName string

@description('Entra tenant ID')
param tenantId string

@description('Application Insights connection string')
@secure()
param appInsightsConnectionString string = ''

@description('Resource tags')
param tags object = {}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${containerAppName}-env'
  location: location
  tags: tags
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: infrastructureSubnetId
      internal: true
    }
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: reference(logAnalyticsWorkspaceId, '2023-09-01').customerId
        sharedKey: listKeys(logAnalyticsWorkspaceId, '2023-09-01').primarySharedKey
      }
    }
    zoneRedundant: false
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      ingress: {
        external: false
        targetPort: 8080
        transport: 'http'
      }
      registries: [
        {
          server: acrLoginServer
          identity: managedIdentityId
        }
      ]
      activeRevisionsMode: 'Single'
    }
    template: {
      containers: [
        {
          name: 'retrieval-agent'
          image: '${acrLoginServer}/${imageName}'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'COSMOS_ENDPOINT', value: cosmosEndpoint }
            { name: 'COSMOS_DATABASE', value: cosmosDatabaseName }
            { name: 'AZURE_OPENAI_ENDPOINT', value: openAiEndpoint }
            { name: 'CHAT_DEPLOYMENT', value: chatDeploymentName }
            { name: 'EMBEDDING_DEPLOYMENT', value: embeddingDeploymentName }
            { name: 'TENANT_ID', value: tenantId }
            { name: 'MANAGED_IDENTITY_CLIENT_ID', value: managedIdentityClientId }
            { name: 'INCLUDE_CITATIONS', value: 'true' }
            { name: 'MAX_EVIDENCE_CHUNKS', value: '5' }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/health/live', port: 8080 }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
            {
              type: 'Readiness'
              httpGet: { path: '/health/ready', port: 8080 }
              initialDelaySeconds: 10
              periodSeconds: 15
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 5
      }
    }
  }
}

@description('Container App FQDN (internal)')
output fqdn string = containerApp.properties.configuration.ingress.fqdn

@description('Container App internal URL')
output internalUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
