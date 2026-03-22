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
//     --parameters worker-infra.bicepparam
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

@description('Bot client ID used for proactive messaging (UAMI client ID after MI cutover)')
param botClientId string = ''

@description('Entra Tenant ID')
param tenantId string = tenant().tenantId

@description('Resource group name for worker infrastructure')
param workerResourceGroupName string = 'zolab-worker-${suffix}'

@description('Subscription ID that hosts the existing bot User-Assigned Managed Identity')
param managedIdentitySubscriptionId string = subscription().subscriptionId

@description('Resource group that hosts the existing bot User-Assigned Managed Identity')
param managedIdentityResourceGroupName string = 'zolab-bot-${suffix}'

@description('Name of the existing bot User-Assigned Managed Identity')
param managedIdentityName string = 'zolab-bot-mi-${suffix}'

@description('CPU requested for the worker container instance')
param workerCpu int = 2

@description('Memory in GiB requested for the worker container instance')
param workerMemoryInGb int = 4

@description('Worker container image tag to deploy from the worker ACR')
param workerImageTag string = 'latest'

@description('Public bot hostname used when the worker publishes anonymous build-info download links')
param botFqdn string = ''

@description('Enable private connectivity from the worker and bot to the shared storage account')
param enablePrivateStorageAccess bool = false

@description('Address space for the shared worker virtual network when private storage access is enabled')
param workerVnetAddressPrefix string = '10.42.0.0/24'

@description('Subnet prefix reserved for the Container Apps environment infrastructure')
param containerAppsSubnetAddressPrefix string = '10.42.0.0/27'

@description('Subnet prefix reserved for the worker container group')
param workerSubnetAddressPrefix string = '10.42.0.32/28'

@description('Subnet prefix reserved for storage private endpoints')
param privateEndpointSubnetAddressPrefix string = '10.42.0.48/28'

resource botManagedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  scope: resourceGroup(managedIdentitySubscriptionId, managedIdentityResourceGroupName)
  name: managedIdentityName
}

var resolvedBotClientId = empty(botClientId) ? botManagedIdentity.properties.clientId : botClientId
var resolvedManagedIdentityResourceId = botManagedIdentity.id
var resolvedManagedIdentityPrincipalId = botManagedIdentity.properties.principalId
var resolvedManagedIdentityClientId = botManagedIdentity.properties.clientId

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
    botClientId: resolvedBotClientId
    tenantId: tenantId
    managedIdentityResourceId: resolvedManagedIdentityResourceId
    managedIdentityPrincipalId: resolvedManagedIdentityPrincipalId
    managedIdentityClientId: resolvedManagedIdentityClientId
    workerCpu: workerCpu
    workerMemoryInGb: workerMemoryInGb
    workerImageTag: workerImageTag
    botFqdn: botFqdn
    enablePrivateStorageAccess: enablePrivateStorageAccess
    workerVnetAddressPrefix: workerVnetAddressPrefix
    containerAppsSubnetAddressPrefix: containerAppsSubnetAddressPrefix
    workerSubnetAddressPrefix: workerSubnetAddressPrefix
    privateEndpointSubnetAddressPrefix: privateEndpointSubnetAddressPrefix
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
output workerVnetName string = workerResources.outputs.workerVnetName
output containerAppsInfrastructureSubnetId string = workerResources.outputs.containerAppsInfrastructureSubnetId
output workerSubnetId string = workerResources.outputs.workerSubnetId
output privateEndpointSubnetId string = workerResources.outputs.privateEndpointSubnetId
