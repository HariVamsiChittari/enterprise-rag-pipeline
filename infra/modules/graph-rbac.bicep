extension 'br:mcr.microsoft.com/bicep/extensions/microsoftgraph/v1.0:1.0.0'

// Graph app roles for the shared managed identity (used by ACA retrieval for ACL group resolution).

@description('Managed identity principal ID')
param principalId string

@description('Microsoft Graph service principal object ID (tenant-specific)')
param graphServicePrincipalId string

var graphAppRoles = {
  groupMemberReadAll: '98830695-27a2-44f7-8c18-0c3ebc9698f6'
  userReadAll: 'df021288-bdef-4463-88db-98f22de89214'
}

resource graphGroupMemberReadAll 'Microsoft.Graph/appRoleAssignedTo@v1.0' = {
  appRoleId: graphAppRoles.groupMemberReadAll
  principalId: principalId
  resourceId: graphServicePrincipalId
}

resource graphUserReadAll 'Microsoft.Graph/appRoleAssignedTo@v1.0' = {
  appRoleId: graphAppRoles.userReadAll
  principalId: principalId
  resourceId: graphServicePrincipalId
}
