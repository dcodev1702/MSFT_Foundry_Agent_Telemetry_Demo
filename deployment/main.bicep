// ════════════════════════════════════════════════════════════════
// main.bicep — Subscription-scoped entry point
// Creates the zolab-ai-<suffix> resource group and deploys all
// AI Foundry resources + RBAC via the resources module.
// ════════════════════════════════════════════════════════════════
targetScope = 'subscription'

@description('Object ID of the zolab-ai-dev Entra ID security group')
param aiDevGroupObjectId string

@description('Subscription ID of the Security subscription (hosts DIBSecCom LAW)')
param securitySubscriptionId string

@description('Location for all resources')
param location string = 'eastus2'

// Generate a deterministic 6-char alphanumeric suffix from the subscription
var suffix = take(uniqueString(subscription().subscriptionId, 'zolab-ai'), 6)
var resourceGroupName = 'zolab-ai-${suffix}'

resource rg 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: resourceGroupName
  location: location
}

module resources 'modules/resources.bicep' = {
  name: 'foundry-resources-deployment'
  scope: rg
  params: {
    location: location
    suffix: suffix
    aiDevGroupObjectId: aiDevGroupObjectId
    logAnalyticsWorkspaceId: '/subscriptions/${securitySubscriptionId}/resourceGroups/Sentinel/providers/Microsoft.OperationalInsights/workspaces/DIBSecCom'
  }
}

output resourceGroupName string = rg.name
output suffix string = suffix
output storageAccountName string = resources.outputs.storageAccountName
output keyVaultName string = resources.outputs.keyVaultName
output appInsightsName string = resources.outputs.appInsightsName
output aiFoundryName string = resources.outputs.aiFoundryName
output aiProjectName string = resources.outputs.aiProjectName
