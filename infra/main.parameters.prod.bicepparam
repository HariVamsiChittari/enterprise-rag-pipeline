using './main.bicep'

param environmentName = 'prod'
param location = readEnvironmentVariable('AZURE_LOCATION', 'eastus')
param openAiAccountName = readEnvironmentVariable('AZURE_OPENAI_ACCOUNT_NAME')
param openAiResourceGroupName = readEnvironmentVariable('AZURE_OPENAI_RESOURCE_GROUP')
param embeddingDeploymentName = readEnvironmentVariable('OPENAI_EMBEDDING_DEPLOYMENT_NAME', 'text-embedding-3-large')
param chatDeploymentName = readEnvironmentVariable('OPENAI_CHAT_DEPLOYMENT_NAME')
param sharePointTenantId = readEnvironmentVariable('SHAREPOINT_TENANT_ID')
param sharePointAppClientId = readEnvironmentVariable('SHAREPOINT_APP_CLIENT_ID')
param sharePointDriveId = readEnvironmentVariable('SHAREPOINT_ASSIGNED_DRIVE_ID')
param ingestionSourceId = readEnvironmentVariable('INGESTION_SOURCE_ID')
param sharePointCertificateSecretName = readEnvironmentVariable(
  'SHAREPOINT_CERTIFICATE_SECRET_NAME',
  'sharepoint-app-cert'
)
param adminApiClientId = readEnvironmentVariable('ADMIN_API_CLIENT_ID')
param cosmosDbMode = 'provisioned'
param cosmosMetadataAutoscaleMaxRUs = 1000
param cosmosSearchChunksAutoscaleMaxRUs = 1000
param storageRedundancy = 'ZRS'
param useDocumentIntelligenceFreeTier = false
param useLanguageFreeTier = false
param applicationInsightsDailyCapGb = -1
param tags = {
  Environment: 'Production'
  Project: 'RAG-SharePoint'
  ManagedBy: 'azd'
  CostCenter: 'Operations'
  Criticality: 'High'
}
