// ════════════════════════════════════════════════════════════════
// law-rbac.bicep — Assigns Log Analytics Reader on DIBSecCom
// workspace to the zolab-ai-dev group.  Targets the Security
// subscription (separate from the main Foundry deployment).
// ════════════════════════════════════════════════════════════════
targetScope = 'subscription'

@description('Object ID of the zolab-ai-dev Entra security group')
param aiDevGroupObjectId string

@description('Resource group containing the Log Analytics Workspace')
param lawResourceGroup string = 'Sentinel'

@description('Name of the Log Analytics Workspace')
param lawName string = 'DIBSecCom'

// Log Analytics Reader built-in role
var logAnalyticsReaderRoleId = '73c42c96-874c-492b-b04d-ab87d138a893'

module lawRbac 'modules/law-rbac.bicep' = {
  name: 'law-rbac-zolab-ai-dev'
  scope: resourceGroup(lawResourceGroup)
  params: {
    lawName: lawName
    principalId: aiDevGroupObjectId
    roleDefinitionId: logAnalyticsReaderRoleId
  }
}
