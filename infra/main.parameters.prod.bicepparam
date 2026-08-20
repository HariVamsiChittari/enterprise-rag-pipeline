// STATUS: Scaffolding only. Not currently deployed against any environment.
// Before using this file for a real production deployment, apply the
// hardening checklist in docs/PRODUCTION_READINESS.md.
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
param sharePointSiteUrl = readEnvironmentVariable('SHAREPOINT_SITE_URL', '')
param ingestionSourceId = readEnvironmentVariable('INGESTION_SOURCE_ID')
param sharePointCertificateSecretName = readEnvironmentVariable(
  'SHAREPOINT_CERTIFICATE_SECRET_NAME',
  'sharepoint-app-cert'
)
param adminApiClientId = readEnvironmentVariable('ADMIN_API_CLIENT_ID')
param webhookClientState = readEnvironmentVariable('WEBHOOK_CLIENT_STATE')
// Restrict which Entra apps can call the API. Populate with the frontend client
// ID(s) for a hardened deployment. Leave empty to allow any tenant app.
param allowedApplicationClientIds = [
  readEnvironmentVariable('FRONTEND_CLIENT_ID', '')
]
param graphServicePrincipalId = readEnvironmentVariable('GRAPH_SERVICE_PRINCIPAL_ID')
param cosmosDbMode = 'provisioned'
param deployAks = true
param cosmosMetadataAutoscaleMaxRUs = 4000
param cosmosSearchChunksAutoscaleMaxRUs = 4000
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
