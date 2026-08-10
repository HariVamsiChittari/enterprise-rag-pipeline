// =========================================
// Azure AI Services Module
// =========================================
// Creates Document Intelligence for PDF layout extraction

@description('Document Intelligence service name')
param documentIntelligenceName string

@description('Azure AI Language service name')
param languageServiceName string

@description('Azure region')
param location string = resourceGroup().location

@description('Use F0 Free tier (dev only - limited capacity)')
param useFreeF0 bool = false

@description('Use F0 Free tier for Azure AI Language (dev only - limited capacity)')
param useLanguageFreeTier bool = false

@description('Resource tags')
param tags object = {}

// =========================================
// Resources
// =========================================

// Document Intelligence (formerly Form Recognizer)
resource documentIntelligence 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: documentIntelligenceName
  location: location
  kind: 'FormRecognizer'
  sku: {
    name: useFreeF0 ? 'F0' : 'S0'
  }
  properties: {
    customSubDomainName: documentIntelligenceName
    networkAcls: {
      defaultAction: 'Deny'
      virtualNetworkRules: []
      ipRules: []
    }
    publicNetworkAccess: 'Disabled'
    disableLocalAuth: true
  }
  tags: tags
}

// Azure AI Language (Text Analytics)
resource languageService 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: languageServiceName
  location: location
  kind: 'TextAnalytics'
  sku: {
    name: useLanguageFreeTier ? 'F0' : 'S'
  }
  properties: {
    customSubDomainName: languageServiceName
    networkAcls: {
      defaultAction: 'Deny'
      virtualNetworkRules: []
      ipRules: []
    }
    publicNetworkAccess: 'Disabled'
    disableLocalAuth: true
  }
  tags: tags
}

// =========================================
// Outputs
// =========================================

@description('Document Intelligence resource ID')
output documentIntelligenceId string = documentIntelligence.id

@description('Document Intelligence endpoint')
output documentIntelligenceEndpoint string = documentIntelligence.properties.endpoint

@description('Document Intelligence name')
output documentIntelligenceName string = documentIntelligence.name

@description('Azure AI Language resource ID')
output languageServiceId string = languageService.id

@description('Azure AI Language endpoint')
output languageServiceEndpoint string = languageService.properties.endpoint

@description('Azure AI Language name')
output languageServiceName string = languageService.name
