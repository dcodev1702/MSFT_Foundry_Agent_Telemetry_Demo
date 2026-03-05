// ════════════════════════════════════════════════════════════════
// resources.bicep — All AI Foundry resources and RBAC assignments
// Deployed at resource-group scope by main.bicep.
// ════════════════════════════════════════════════════════════════

@description('Azure region for all resources')
param location string

@description('6-char alphanumeric suffix appended to every resource name')
param suffix string

@description('Object ID of the zolab-ai-dev Entra security group')
param aiDevGroupObjectId string

@description('Full resource ID of the Log Analytics Workspace (DIBSecCom in Security sub)')
param logAnalyticsWorkspaceId string

// ── Resource Names ──
var storageAccountName = 'zolabaifndrysa${suffix}'
var keyVaultName = 'zolabaifndrykv${suffix}'
var appInsightsName = 'zolabaifndryai${suffix}'
var aiFoundryName = 'zolabai-foundry-${suffix}'
var aiProjectName = 'zolabai-fndry-proj-${suffix}'

// ── Built-in RBAC Role Definition IDs ──
var roles = {
  azureAIDeveloper:          '64702f94-c441-49e6-a78b-ef80e0188fee'
  azureAIUser:               '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  storageBlobDataContributor:'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  keyVaultSecretsOfficer:    'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
  keyVaultCryptoOfficer:     '14b46e9e-c2b7-41b4-b07b-48a6ebf60603'
  keyVaultContributor:       'f25e0fa2-a7c8-4377-a976-54943a77a395'
  contributor:               'b24988ac-6180-42a0-ab88-20f7382dd24c'
}

// ════════════════════════════════════════════════════════════════
//  RESOURCES
// ════════════════════════════════════════════════════════════════

// ── Storage Account ──
resource storageAccount 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    allowCrossTenantReplication: false
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
    }
    encryption: {
      services: {
        blob: { enabled: true, keyType: 'Account' }
        file: { enabled: true, keyType: 'Account' }
      }
      keySource: 'Microsoft.Storage'
    }
  }
}

// ── Key Vault ──
resource keyVault 'Microsoft.KeyVault/vaults@2024-04-01-preview' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enabledForDeployment: true
    enabledForTemplateDeployment: true
    enabledForDiskEncryption: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: true
    enableRbacAuthorization: true
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

// ── Application Insights (telemetry → DIBSecCom LAW in Security sub) ──
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    IngestionMode: 'LogAnalytics'
    WorkspaceResourceId: logAnalyticsWorkspaceId
    RetentionInDays: 90
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// ── AI Foundry Account (Azure AI Services) ──
resource aiFoundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: aiFoundryName
  location: location
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: aiFoundryName
    publicNetworkAccess: 'Enabled'
    allowProjectManagement: true
  }
}

// ── AI Foundry Project ──
resource aiFoundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: aiFoundry
  name: aiProjectName
  location: location
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

// ════════════════════════════════════════════════════════════════
//  DIAGNOSTIC SETTINGS
// ════════════════════════════════════════════════════════════════

// ── Key Vault Diagnostic Settings (allLogs → DIBSecCom LAW) ──
resource kvDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${keyVaultName}-audit'
  scope: keyVault
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logAnalyticsDestinationType: 'AzureDiagnostics'
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
      {
        categoryGroup: 'audit'
        enabled: false
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: false
      }
    ]
  }
}

// ── Blob Storage Diagnostic Settings (allLogs → DIBSecCom LAW) ──
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2024-01-01' existing = {
  parent: storageAccount
  name: 'default'
}

resource blobDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${storageAccountName}-blob-audit'
  scope: blobService
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
      {
        categoryGroup: 'audit'
        enabled: false
      }
    ]
    metrics: [
      {
        category: 'Capacity'
        enabled: false
      }
      {
        category: 'Transaction'
        enabled: false
      }
    ]
  }
}

// ════════════════════════════════════════════════════════════════
// ════════════════════════════════════════════════════════════════

resource aiDevRoleAIDeveloper 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, aiDevGroupObjectId, roles.azureAIDeveloper)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.azureAIDeveloper)
    principalId: aiDevGroupObjectId
    principalType: 'Group'
  }
}

resource aiDevRoleAIUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, aiDevGroupObjectId, roles.azureAIUser)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.azureAIUser)
    principalId: aiDevGroupObjectId
    principalType: 'Group'
  }
}

resource aiDevRoleStorageBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, aiDevGroupObjectId, roles.storageBlobDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
    principalId: aiDevGroupObjectId
    principalType: 'Group'
  }
}

resource aiDevRoleKVSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, aiDevGroupObjectId, roles.keyVaultSecretsOfficer)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsOfficer)
    principalId: aiDevGroupObjectId
    principalType: 'Group'
  }
}

resource aiDevRoleKVCrypto 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, aiDevGroupObjectId, roles.keyVaultCryptoOfficer)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultCryptoOfficer)
    principalId: aiDevGroupObjectId
    principalType: 'Group'
  }
}

// ════════════════════════════════════════════════════════════════
//  RBAC – AI Foundry managed identity
// ════════════════════════════════════════════════════════════════

resource foundryRoleKVSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, aiFoundry.name, roles.keyVaultSecretsOfficer)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsOfficer)
    principalId: aiFoundry.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource foundryRoleKVContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, aiFoundry.name, roles.keyVaultContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultContributor)
    principalId: aiFoundry.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource foundryRoleStorageBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, aiFoundry.name, roles.storageBlobDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
    principalId: aiFoundry.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource foundryRoleContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, aiFoundry.name, roles.contributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
    principalId: aiFoundry.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ════════════════════════════════════════════════════════════════
//  OUTPUTS
// ════════════════════════════════════════════════════════════════

output storageAccountName string = storageAccount.name
output keyVaultName string = keyVault.name
output appInsightsName string = appInsights.name
output aiFoundryName string = aiFoundry.name
output aiProjectName string = aiFoundryProject.name
output aiFoundryPrincipalId string = aiFoundry.identity.principalId
