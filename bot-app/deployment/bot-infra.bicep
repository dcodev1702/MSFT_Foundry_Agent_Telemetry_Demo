// ════════════════════════════════════════════════════════════════
// bot-infra.bicep — Standalone Bot infrastructure
//
// Subscription-scoped deployment that creates:
//   - A resource group for bot resources
//   - App Service Plan (Linux, B1)
//   - App Service (Python 3.11, M365 Agents SDK)
//   - System-assigned Managed Identity with RBAC
//
// Usage:
//   az deployment sub create \
//     --location eastus2 \
//     --template-file bot-infra.bicep \
//     --parameters bot-infra.bicepparam
//
// NOTE: This is a STANDALONE deployment, separate from the
//       Foundry resources in deployment/main.bicep.
// ════════════════════════════════════════════════════════════════
targetScope = 'subscription'

// ── Parameters ────────────────────────────────────────────────

@description('Azure region for all bot resources')
param location string = 'eastus2'

@description('6-char alphanumeric suffix for resource naming')
param suffix string

@description('Bot App Registration Client ID (from Entra ID)')
param botAppId string

@description('Entra Tenant ID')
param tenantId string

@description('Resource group name for bot infrastructure')
param botResourceGroupName string = 'zolab-bot-${suffix}'

@description('App Service Plan SKU (B1 for dev/pilot, S1+ for production)')
param appServicePlanSku string = 'B1'

@description('Python runtime version for App Service')
param pythonVersion string = '3.11'

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
    botAppId: botAppId
    tenantId: tenantId
    appServicePlanSku: appServicePlanSku
    pythonVersion: pythonVersion
  }
}

// ── Outputs ───────────────────────────────────────────────────

output resourceGroupName string = botRg.name
output appServiceName string = botResources.outputs.appServiceName
output appServiceUrl string = botResources.outputs.appServiceUrl
output managedIdentityPrincipalId string = botResources.outputs.managedIdentityPrincipalId
