@description('Container App name')
param containerAppName string

@description('Azure region')
param location string = resourceGroup().location

@description('ACR login server (e.g. myacr.azurecr.io)')
param acrLoginServer string

@description('Container image name and tag (MCR placeholder for initial deploy; CI/CD overrides)')
param imageName string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('User-assigned managed identity resource ID for the container app')
param managedIdentityId string

@description('VNet integration subnet resource ID')
param infrastructureSubnetId string

@description('Log Analytics workspace resource ID')
param logAnalyticsWorkspaceId string

@description('Retrieval service env vars (from retrieval-config module)')
param retrievalEnvVars array

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
        external: true
        targetPort: 8080
        transport: 'auto'
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
          image: startsWith(imageName, 'mcr.microsoft.com/') ? imageName : '${acrLoginServer}/${imageName}'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: retrievalEnvVars
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

@description('ACA environment default domain (for private DNS zone)')
output environmentDefaultDomain string = environment.properties.defaultDomain

@description('ACA environment static IP (for private DNS A records)')
output environmentStaticIp string = environment.properties.staticIp
