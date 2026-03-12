// ════════════════════════════════════════════════════════════════
// worker-resources.bicep — ACR + Storage (Queue/Blob) + ACI
//
// Deployed at resource-group scope by worker-infra.bicep.
// Uses Azure Queue Storage for job dispatch and Azure Blob Storage
// for conversation state.  All auth is via Entra ID / RBAC — no
// storage account keys.
// ════════════════════════════════════════════════════════════════

@description('Azure region for all resources')
param location string

@description('6-char alphanumeric suffix')
param suffix string

@description('Bot App Registration Client ID (for proactive messaging)')
param botAppId string

@description('Entra Tenant ID')
param tenantId string

@secure()
@description('Bot App Registration Client Secret (for proactive messaging)')
param botAppSecret string

@description('Resource ID of the existing User-Assigned Managed Identity')
param managedIdentityResourceId string

@description('Principal ID of the existing User-Assigned Managed Identity')
param managedIdentityPrincipalId string

@description('Client ID of the existing User-Assigned Managed Identity')
param managedIdentityClientId string

@description('CPU requested for the worker container instance')
param workerCpu int

@description('Memory in GiB requested for the worker container instance')
param workerMemoryInGb int

@description('Worker container image tag to deploy from the worker ACR')
param workerImageTag string = 'latest'

// ── Resource Names ────────────────────────────────────────────
var acrName            = 'zolabworkeracr${suffix}'
var storageAccountName = 'zolabworkerst${suffix}'
var queueName          = 'botjobs'
var blobContainerName  = 'botstate'
var aciName            = 'zolab-worker-aci-${suffix}'

// ── Built-in RBAC Role Definition IDs ─────────────────────────
var roles = {
  acrPull:                 '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  storageQueueDataContrib: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  storageBlobDataContrib:  'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
}

// ════════════════════════════════════════════════════════════════
//  RESOURCES
// ════════════════════════════════════════════════════════════════

// ── Azure Container Registry (Basic) ────────────────────────────
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false  // UAMI used for ACI image pulls — no admin keys needed
  }
}

// ── RBAC: UAMI → AcrPull on ACR ────────────────────────────────
resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, 'uami', roles.acrPull)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ── Storage Account ───────────────────────────────────────────
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    allowSharedKeyAccess: false  // Entra ID / RBAC only
  }
}

// ── Queue Service + Queue ─────────────────────────────────────
resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource jobQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueService
  name: queueName
}

// ── Blob Service + Container ──────────────────────────────────
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource stateContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: blobContainerName
}

// ── RBAC: UAMI → Storage Queue Data Contributor ─────────────
resource storageQueueRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, 'uami', roles.storageQueueDataContrib)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageQueueDataContrib)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ── RBAC: UAMI → Storage Blob Data Contributor ──────────────
resource storageBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, 'uami', roles.storageBlobDataContrib)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContrib)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ── Azure Container Instance ────────────────────────────────────
resource aci 'Microsoft.ContainerInstance/containerGroups@2023-05-01' = {
  name: aciName
  location: location
  dependsOn: [
    jobQueue
    stateContainer
    acrPullRole
    storageQueueRole
    storageBlobRole
  ]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityResourceId}': {}
    }
  }
  properties: {
    osType: 'Linux'
    restartPolicy: 'Always'
    imageRegistryCredentials: [
      {
        server: acr.properties.loginServer
        identity: managedIdentityResourceId
      }
    ]
    containers: [
      {
        name: 'worker'
        properties: {
          image: '${acr.properties.loginServer}/zolab-worker:${workerImageTag}'
          resources: {
            requests: {
              cpu: workerCpu
              memoryInGB: json(string(workerMemoryInGb))
            }
          }
          environmentVariables: [
            {
              name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID'
              value: botAppId
            }
            {
              name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID'
              value: tenantId
            }
            {
              name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET'
              secureValue: botAppSecret
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: managedIdentityClientId
            }
            {
              name: 'AZURE_STORAGE_ACCOUNT'
              value: storageAccountName
            }
            {
              name: 'AZURE_QUEUE_NAME'
              value: queueName
            }
            {
              name: 'AZURE_BLOB_CONTAINER'
              value: blobContainerName
            }
          ]
        }
      }
    ]
  }
}

// ════════════════════════════════════════════════════════════════
//  OUTPUTS
// ════════════════════════════════════════════════════════════════

output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output storageAccountName string = storageAccount.name
output queueName string = queueName
output blobContainerName string = blobContainerName
output aciName string = aci.name
