// ════════════════════════════════════════════════════════════════
// worker-infra.bicep — Standalone Worker infrastructure
//
// Subscription-scoped deployment that creates:
//   - A resource group for worker resources
//   - Azure Container Registry (Basic)
//   - Storage Account + Queue + Blob container
//   - Azure Container Instance (long-running PowerShell worker)
//
// Usage:
//   az deployment sub create \
//     --location eastus2 \
//     --template-file worker-infra.bicep \
//     --parameters worker-infra.bicepparam \
//     --parameters botAppSecret='<secret>'
//
// NOTE: This is INDEPENDENT of the Bot App Service deployment.
//       The worker handles PowerShell/Bicep execution only.
//       Bot ↔ Worker communication is via Azure Queue + Blob Storage.
// ════════════════════════════════════════════════════════════════
targetScope = 'subscription'

// ── Parameters ────────────────────────────────────────────────

@description('Azure region for all worker resources')
param location string = 'eastus2'

@description('6-char alphanumeric suffix for resource naming')
param suffix string

@description('Bot App Registration Client ID (for proactive messaging)')
param botAppId string

@description('Entra Tenant ID')
param tenantId string

@secure()
@description('Bot App Registration Client Secret (for proactive messaging)')
param botAppSecret string

@description('Resource group name for worker infrastructure')
param workerResourceGroupName string = 'zolab-worker-${suffix}'

@description('Resource ID of the existing User-Assigned Managed Identity')
param managedIdentityResourceId string

@description('Principal ID of the existing User-Assigned Managed Identity')
param managedIdentityPrincipalId string

@description('Client ID of the existing User-Assigned Managed Identity')
param managedIdentityClientId string

// ── Resource Group ────────────────────────────────────────────

resource workerRg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: workerResourceGroupName
  location: location
}

// ── Worker Resources (resource-group scope module) ────────────

module workerResources 'modules/worker-resources.bicep' = {
  scope: workerRg
  name: 'worker-resources-${suffix}'
  params: {
    location: location
    suffix: suffix
    botAppId: botAppId
    tenantId: tenantId
    botAppSecret: botAppSecret
    managedIdentityResourceId: managedIdentityResourceId
    managedIdentityPrincipalId: managedIdentityPrincipalId
    managedIdentityClientId: managedIdentityClientId
  }
}

// ── Outputs ───────────────────────────────────────────────────

output resourceGroupName string = workerRg.name
output acrLoginServer string = workerResources.outputs.acrLoginServer
output acrName string = workerResources.outputs.acrName
output storageAccountName string = workerResources.outputs.storageAccountName
output queueName string = workerResources.outputs.queueName
output blobContainerName string = workerResources.outputs.blobContainerName
output aciName string = workerResources.outputs.aciName
