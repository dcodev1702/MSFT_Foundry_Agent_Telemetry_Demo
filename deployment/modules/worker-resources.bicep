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

@description('Bot client ID used for proactive messaging')
param botClientId string

@description('Entra Tenant ID')
param tenantId string

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

// ── Resource Names ────────────────────────────────────────────
var acrName            = 'zolabworkeracr${suffix}'
var storageAccountName = 'zolabworkerst${suffix}'
var queueName          = 'botjobs'
var blobContainerName  = 'botstate'
var aciName            = 'zolab-worker-aci-${suffix}'
var workerVnetName     = 'zolab-worker-vnet-${suffix}'
var containerAppsSubnetName = 'snet-containerapps'
var workerSubnetName = 'snet-worker-aci'
var privateEndpointSubnetName = 'snet-storage-private-endpoints'
var blobPrivateDnsZoneName = 'privatelink.blob.core.windows.net'
var queuePrivateDnsZoneName = 'privatelink.queue.core.windows.net'

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
    publicNetworkAccess: enablePrivateStorageAccess ? 'Disabled' : 'Enabled'
    allowSharedKeyAccess: false  // Entra ID / RBAC only
  }
}

resource workerVnet 'Microsoft.Network/virtualNetworks@2024-05-01' = if (enablePrivateStorageAccess) {
  name: workerVnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        workerVnetAddressPrefix
      ]
    }
  }
}

resource containerAppsSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = if (enablePrivateStorageAccess) {
  parent: workerVnet
  name: containerAppsSubnetName
  properties: {
    addressPrefix: containerAppsSubnetAddressPrefix
    delegations: [
      {
        name: 'containerapps-delegation'
        properties: {
          serviceName: 'Microsoft.App/environments'
        }
      }
    ]
  }
}

resource workerSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = if (enablePrivateStorageAccess) {
  parent: workerVnet
  name: workerSubnetName
  properties: {
    addressPrefix: workerSubnetAddressPrefix
    delegations: [
      {
        name: 'aci-delegation'
        properties: {
          serviceName: 'Microsoft.ContainerInstance/containerGroups'
        }
      }
    ]
  }
}

resource privateEndpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = if (enablePrivateStorageAccess) {
  parent: workerVnet
  name: privateEndpointSubnetName
  properties: {
    addressPrefix: privateEndpointSubnetAddressPrefix
    privateEndpointNetworkPolicies: 'Disabled'
  }
}

resource blobPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (enablePrivateStorageAccess) {
  name: blobPrivateDnsZoneName
  location: 'global'
}

resource queuePrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (enablePrivateStorageAccess) {
  name: queuePrivateDnsZoneName
  location: 'global'
}

resource blobDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (enablePrivateStorageAccess) {
  parent: blobPrivateDnsZone
  name: '${workerVnetName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: workerVnet.id
    }
  }
}

resource queueDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (enablePrivateStorageAccess) {
  parent: queuePrivateDnsZone
  name: '${workerVnetName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: workerVnet.id
    }
  }
}

resource blobPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = if (enablePrivateStorageAccess) {
  name: '${storageAccountName}-blob-pe'
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: '${storageAccountName}-blob-connection'
        properties: {
          privateLinkServiceId: storageAccount.id
          groupIds: [
            'blob'
          ]
        }
      }
    ]
  }
}

resource blobPrivateEndpointDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = if (enablePrivateStorageAccess) {
  parent: blobPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'blob-zone'
        properties: {
          privateDnsZoneId: blobPrivateDnsZone.id
        }
      }
    ]
  }
}

resource queuePrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = if (enablePrivateStorageAccess) {
  name: '${storageAccountName}-queue-pe'
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: '${storageAccountName}-queue-connection'
        properties: {
          privateLinkServiceId: storageAccount.id
          groupIds: [
            'queue'
          ]
        }
      }
    ]
  }
}

resource queuePrivateEndpointDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = if (enablePrivateStorageAccess) {
  parent: queuePrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'queue-zone'
        properties: {
          privateDnsZoneId: queuePrivateDnsZone.id
        }
      }
    ]
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
    subnetIds: enablePrivateStorageAccess ? [
      {
        id: workerSubnet.id
      }
    ] : []
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
          environmentVariables: concat(
            [
              {
                name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE'
                value: 'UserManagedIdentity'
              }
              {
                name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID'
                value: botClientId
              }
              {
                name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID'
                value: tenantId
              }
            ],
            [
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
          )
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
output workerVnetName string = enablePrivateStorageAccess ? workerVnet.name : ''
output containerAppsInfrastructureSubnetId string = enablePrivateStorageAccess ? containerAppsSubnet.id : ''
output workerSubnetId string = enablePrivateStorageAccess ? workerSubnet.id : ''
output privateEndpointSubnetId string = enablePrivateStorageAccess ? privateEndpointSubnet.id : ''
