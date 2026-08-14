@description('AKS cluster name')
param clusterName string

@description('Azure region')
param location string = resourceGroup().location

@description('Kubernetes version')
param kubernetesVersion string = '1.30'

@description('Existing virtual network resource ID')
param vnetId string

@description('Subnet address prefix for AKS nodes')
param aksSubnetPrefix string = '10.20.0.64/26'

@description('Log Analytics workspace resource ID for Container Insights')
param logAnalyticsWorkspaceId string

@description('Resource tags')
param tags object = {}

var aksSubnetName = 'aks-nodes'

resource aksSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-01-01' = {
  name: '${last(split(vnetId, '/'))}/${aksSubnetName}'
  properties: {
    addressPrefix: aksSubnetPrefix
    privateEndpointNetworkPolicies: 'Disabled'
  }
}

resource aks 'Microsoft.ContainerService/managedClusters@2024-06-02-preview' = {
  name: clusterName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: clusterName
    enableRBAC: true
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      networkPolicy: 'calico'
      serviceCidr: '10.100.0.0/16'
      dnsServiceIP: '10.100.0.10'
      podCidr: '10.244.0.0/16'
    }
    agentPoolProfiles: [
      {
        name: 'system'
        count: 2
        vmSize: 'Standard_D2s_v5'
        mode: 'System'
        osType: 'Linux'
        osSKU: 'AzureLinux'
        vnetSubnetID: aksSubnet.id
        enableAutoScaling: true
        minCount: 2
        maxCount: 4
      }
      {
        name: 'user'
        count: 2
        vmSize: 'Standard_D4s_v5'
        mode: 'User'
        osType: 'Linux'
        osSKU: 'AzureLinux'
        vnetSubnetID: aksSubnet.id
        enableAutoScaling: true
        minCount: 2
        maxCount: 10
        nodeTaints: []
      }
    ]
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    addonProfiles: {
      omsagent: {
        enabled: true
        config: {
          logAnalyticsWorkspaceResourceID: logAnalyticsWorkspaceId
        }
      }
    }
    apiServerAccessProfile: {
      enableVnetIntegration: true
      subnetId: aksSubnet.id
    }
  }
}

output clusterName string = aks.name
output clusterFqdn string = aks.properties.fqdn
output oidcIssuerUrl string = aks.properties.oidcIssuerProfile.issuerURL
output kubeletIdentityObjectId string = aks.properties.identityProfile.kubeletidentity.objectId
