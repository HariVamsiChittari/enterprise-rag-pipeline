// =========================================
// User-Assigned Managed Identity Module
// =========================================
// Creates a User-Assigned Managed Identity (UAMI) for Function data-plane access

@description('Name of the managed identity')
param identityName string

@description('Azure region for the identity')
param location string = resourceGroup().location

@description('Resource tags')
param tags object = {}

// =========================================
// Resources
// =========================================

resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

// =========================================
// Outputs
// =========================================

@description('Resource ID of the managed identity')
output identityId string = managedIdentity.id

@description('Principal ID for RBAC assignments')
output identityPrincipalId string = managedIdentity.properties.principalId

@description('Client ID for application configuration')
output identityClientId string = managedIdentity.properties.clientId

@description('Identity name')
output identityName string = managedIdentity.name
