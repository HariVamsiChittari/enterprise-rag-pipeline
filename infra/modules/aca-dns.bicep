// Private DNS zone for internal ACA environment.
// Enables VNet-integrated services (Function App) to resolve the ACA FQDN.

@description('The ACA environment default domain (from environment.properties.defaultDomain)')
param domainName string

@description('The ACA environment static IP (from environment.properties.staticIp)')
param staticIp string

@description('Virtual network resource ID to link the DNS zone to')
param virtualNetworkId string

@description('Resource tags')
param tags object = {}

resource dnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: domainName
  location: 'global'
  tags: tags
}

resource wildcardRecord 'Microsoft.Network/privateDnsZones/A@2024-06-01' = {
  parent: dnsZone
  name: '*'
  properties: {
    ttl: 300
    aRecords: [{ ipv4Address: staticIp }]
  }
}

resource apexRecord 'Microsoft.Network/privateDnsZones/A@2024-06-01' = {
  parent: dnsZone
  name: '@'
  properties: {
    ttl: 300
    aRecords: [{ ipv4Address: staticIp }]
  }
}

resource vnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: dnsZone
  name: 'aca-vnet-link'
  location: 'global'
  properties: {
    virtualNetwork: { id: virtualNetworkId }
    registrationEnabled: false
  }
}
