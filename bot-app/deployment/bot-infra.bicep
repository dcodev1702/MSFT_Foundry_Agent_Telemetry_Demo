// ════════════════════════════════════════════════════════════════
// bot-infra.bicep — Standalone Bot infrastructure
//
// Subscription-scoped deployment that creates:
//   - A resource group for bot resources
//   - Azure Container Registry (Basic)
//   - User-Assigned Managed Identity (with Graph permissions)
//   - Container Apps Environment (logs → DIBSecCom LAW in Security sub)
//   - Container App (bot web server, pulls from ACR via UAMI)
//   - Azure Bot Service (F0, UserAssignedMSI) + Teams Channel
//
// Usage:
//   az deployment sub create \
//     --location eastus2 \
//     --template-file bot-infra.bicep \
//     --parameters suffix=botprd tenantId=<guid>
//
// NOTE: Uses Azure Container Apps (Microsoft.App) instead of
//       App Service due to Microsoft.Web quota restrictions.
// ════════════════════════════════════════════════════════════════
targetScope = 'subscription'

// ── Parameters ────────────────────────────────────────────────

@description('Azure region for all bot resources')
param location string = 'eastus2'

@description('6-char alphanumeric suffix for resource naming')
param suffix string

@description('Entra Tenant ID')
param tenantId string

@description('Resource group name for bot infrastructure')
param botResourceGroupName string = 'zolab-bot-${suffix}'

@description('DIBSecCom Log Analytics Workspace customer ID (Security sub)')
param logAnalyticsCustomerId string

@secure()
@description('DIBSecCom Log Analytics Workspace shared key (Security sub)')
param logAnalyticsSharedKey string

@description('Bot container image tag to deploy from ACR')
param botImageTag string = 'latest'

// ── Resource Group ────────────────────────────────────────────

resource botRg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: botResourceGroupName
  location: location
}

// ── Bot Resources (resource-group scope module) ───────────────

module botResources 'modules/bot-resources.bicep' = {
  scope: botRg
  name: 'bot-resources-${suffix}'
  params: {
    location: location
    suffix: suffix
    tenantId: tenantId
    logAnalyticsCustomerId: logAnalyticsCustomerId
    logAnalyticsSharedKey: logAnalyticsSharedKey
    botImageTag: botImageTag
  }
}

// ── Outputs ───────────────────────────────────────────────────

output resourceGroupName string = botRg.name
output containerAppName string = botResources.outputs.containerAppName
output containerAppFqdn string = botResources.outputs.containerAppFqdn
output containerAppUrl string = botResources.outputs.containerAppUrl
output managedIdentityPrincipalId string = botResources.outputs.managedIdentityPrincipalId
output managedIdentityClientId string = botResources.outputs.managedIdentityClientId
output managedIdentityResourceId string = botResources.outputs.managedIdentityResourceId
output acrLoginServer string = botResources.outputs.acrLoginServer
output acrName string = botResources.outputs.acrName
output botServiceName string = botResources.outputs.botServiceName
output messagingEndpoint string = botResources.outputs.messagingEndpoint
