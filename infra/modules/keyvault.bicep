// =========================================
// Azure Key Vault Module
// =========================================
// Creates Key Vault for SharePoint certificate and secrets storage

@description('Key Vault name (3-24 alphanumeric + hyphens)')
@minLength(3)
@maxLength(24)
param keyVaultName string

@description('Azure region')
param location string = resourceGroup().location

@description('Resource tags')
param tags object = {}

@description('Enable soft delete (recommended for production)')
param enableSoftDelete bool = true

@description('Soft delete retention period in days')
@minValue(7)
@maxValue(90)
param softDeleteRetentionDays int = 90

@description('Enable purge protection (recommended for production)')
param enablePurgeProtection bool = true

// =========================================
// Resources
// =========================================

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true // Use RBAC instead of access policies
    enableSoftDelete: enableSoftDelete
    softDeleteRetentionInDays: softDeleteRetentionDays
    enablePurgeProtection: enablePurgeProtection ? true : null
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      ipRules: []
      virtualNetworkRules: []
    }
    publicNetworkAccess: 'Disabled'
  }
  tags: tags
}

// =========================================
// Outputs
// =========================================

@description('Resource ID of the Key Vault')
output keyVaultId string = keyVault.id

@description('Key Vault name')
output keyVaultName string = keyVault.name

@description('Key Vault URI')
output keyVaultUri string = keyVault.properties.vaultUri

@description('Key Vault tenant ID')
output tenantId string = keyVault.properties.tenantId
