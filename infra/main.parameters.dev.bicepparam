using './main.bicep'

param environmentName = 'dev'
param location = readEnvironmentVariable('AZURE_LOCATION', 'eastus')
param openAiAccountName = readEnvironmentVariable('AZURE_OPENAI_ACCOUNT_NAME')
param openAiResourceGroupName = readEnvironmentVariable('AZURE_OPENAI_RESOURCE_GROUP')
param embeddingDeploymentName = readEnvironmentVariable('OPENAI_EMBEDDING_DEPLOYMENT_NAME', 'text-embedding-3-large')
param chatDeploymentName = readEnvironmentVariable('OPENAI_CHAT_DEPLOYMENT_NAME')
param sharePointTenantId = readEnvironmentVariable('SHAREPOINT_TENANT_ID')
param sharePointAppClientId = readEnvironmentVariable('SHAREPOINT_APP_CLIENT_ID')
param sharePointDriveId = readEnvironmentVariable('SHAREPOINT_ASSIGNED_DRIVE_ID')
param sharePointSiteUrl = readEnvironmentVariable('SHAREPOINT_SITE_URL', '')
param ingestionSourceId = readEnvironmentVariable('INGESTION_SOURCE_ID')
param sharePointCertificateSecretName = readEnvironmentVariable(
  'SHAREPOINT_CERTIFICATE_SECRET_NAME',
  'sharepoint-app-cert'
)
param adminApiClientId = readEnvironmentVariable('ADMIN_API_CLIENT_ID')
param webhookClientState = readEnvironmentVariable('WEBHOOK_CLIENT_STATE', 'dev-webhook-secret-change-me')
param graphServicePrincipalId = readEnvironmentVariable('GRAPH_SERVICE_PRINCIPAL_ID')
param cosmosDbMode = 'serverless'
param deployAks = false
param storageRedundancy = 'LRS'
param useDocumentIntelligenceFreeTier = false
param useLanguageFreeTier = false
param applicationInsightsDailyCapGb = 5
param tags = {
  Environment: 'Development'
  Project: 'RAG-SharePoint'
  ManagedBy: 'azd'
  CostCenter: 'Engineering'
}
