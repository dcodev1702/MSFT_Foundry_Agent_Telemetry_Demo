// ════════════════════════════════════════════════════════════════
// bot-resources.bicep — App Service + ACR + Managed Identity
//
// Deployed at resource-group scope by bot-infra.bicep.
// Uses container deployment (Docker) instead of code deployment.
// ════════════════════════════════════════════════════════════════

@description('Azure region for all resources')
param location string

@description('6-char alphanumeric suffix')
param suffix string

@description('Bot App Registration Client ID')
param botAppId string

@description('Entra Tenant ID')
param tenantId string

@description('App Service Plan SKU')
param appServicePlanSku string

@description('Deploy App Service Plan + App Service (set false when B1 quota not yet approved)')
param deployAppService bool = true

// ── Resource Names ────────────────────────────────────────────
var appServicePlanName  = 'zolab-bot-plan-${suffix}'
var appServiceName      = 'zolab-bot-app-${suffix}'
var managedIdentityName = 'zolab-bot-mi-${suffix}'
var acrName             = 'zolabbotacr${suffix}'  // ACR names must be alphanumeric
var botServiceName      = 'zolab-bot-${suffix}'
var messagingEndpoint   = 'https://${appServiceName}.azurewebsites.net/api/messages'

// ── Built-in RBAC Role Definition IDs ─────────────────────────
var roles = {
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
  acrPull:     '7f951dda-4ed3-4680-a7ca-43fe172d538d'
}

// ════════════════════════════════════════════════════════════════
//  RESOURCES
// ════════════════════════════════════════════════════════════════

// ── User-Assigned Managed Identity ──────────────────────────────
// Created ahead of time with Graph permissions pre-granted.
// Bicep ensures it exists; Graph role assignments are out-of-band.
resource botManagedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: managedIdentityName
  location: location
}

// ── Azure Container Registry (Basic) ────────────────────────────
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

// ── RBAC: UAMI → AcrPull on ACR ────────────────────────────────
resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, botManagedIdentity.name, roles.acrPull)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: botManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── App Service Plan (Linux) ──────────────────────────────────
resource appServicePlan 'Microsoft.Web/serverfarms@2024-04-01' = if (deployAppService) {
  name: appServicePlanName
  location: location
  kind: 'linux'
  sku: {
    name: appServicePlanSku
  }
  properties: {
    reserved: true  // Required for Linux
  }
}

// ── App Service (Container + User-Assigned Managed Identity) ──
resource appService 'Microsoft.Web/sites@2024-04-01' = if (deployAppService) {
  name: appServiceName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${botManagedIdentity.id}': {}
    }
  }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'DOCKER|${acr.properties.loginServer}/zolab-bot:latest'
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      alwaysOn: true
      acrUseManagedIdentityCreds: true
      acrUserManagedIdentityID: botManagedIdentity.properties.clientId
      appSettings: [
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
          value: ''  // Set post-deployment via az webapp config appsettings
        }
        {
          name: 'WEBSITES_PORT'
          value: '8000'
        }
        {
          // UAMI client ID — used by PowerShell: Connect-MgGraph -Identity -ClientId <value>
          name: 'AZURE_CLIENT_ID'
          value: botManagedIdentity.properties.clientId
        }
        {
          name: 'DOCKER_REGISTRY_SERVER_URL'
          value: 'https://${acr.properties.loginServer}'
        }
        {
          name: 'AZURE_STORAGE_ACCOUNT'
          value: 'zolabworkerstbotprd'
        }
        {
          name: 'AZURE_QUEUE_NAME'
          value: 'botjobs'
        }
        {
          name: 'AZURE_BLOB_CONTAINER'
          value: 'botstate'
        }
        {
          name: 'WORKER_ENABLED'
          value: 'false'  // ACI handles worker execution
        }
      ]
    }
  }
}

// ── RBAC: Managed Identity → Contributor on own RG ────────────
resource botMIContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, botManagedIdentity.name, roles.contributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
    principalId: botManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── Azure Bot Service ─────────────────────────────────────────
resource azureBot 'Microsoft.BotService/botServices@2023-09-15-preview' = {
  name: botServiceName
  location: 'global'
  kind: 'azurebot'
  sku: {
    name: 'F0'
  }
  properties: {
    displayName: 'Bot the Builder'
    endpoint: messagingEndpoint
    msaAppId: botAppId
    msaAppTenantId: tenantId
    msaAppType: 'SingleTenant'
  }
}

// ── Teams Channel ─────────────────────────────────────────────
resource teamsChannel 'Microsoft.BotService/botServices/channels@2023-09-15-preview' = {
  parent: azureBot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      isEnabled: true
    }
  }
}

// ════════════════════════════════════════════════════════════════
//  OUTPUTS
// ════════════════════════════════════════════════════════════════

output appServiceName string = appServiceName
output appServiceUrl string = 'https://${appServiceName}.azurewebsites.net'
output managedIdentityPrincipalId string = botManagedIdentity.properties.principalId
output managedIdentityClientId string = botManagedIdentity.properties.clientId
output managedIdentityResourceId string = botManagedIdentity.id
output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output botServiceName string = azureBot.name
output messagingEndpoint string = messagingEndpoint
