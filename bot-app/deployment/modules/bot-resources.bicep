// ════════════════════════════════════════════════════════════════
// bot-resources.bicep — Container App + ACR + Managed Identity
//
// Deployed at resource-group scope by bot-infra.bicep.
// Uses Azure Container Apps (Microsoft.App) instead of App Service
// due to Microsoft.Web quota restrictions on Internal subscriptions.
// ════════════════════════════════════════════════════════════════

@description('Azure region for all resources')
param location string

@description('6-char alphanumeric suffix')
param suffix string

@description('Entra Tenant ID')
param tenantId string

@description('Log Analytics Workspace customer (workspace) ID — DIBSecCom in Security sub')
param logAnalyticsCustomerId string

@secure()
@description('Log Analytics Workspace shared key — DIBSecCom in Security sub')
param logAnalyticsSharedKey string

@description('Bot container image tag to deploy from ACR')
param botImageTag string = 'latest'

// ── Resource Names ────────────────────────────────────────────
var managedIdentityName = 'zolab-bot-mi-${suffix}'
var acrName             = 'zolabbotacr${suffix}'
var botServiceName      = 'zolab-bot-${suffix}'
var containerEnvName    = 'zolab-bot-env-${suffix}'
var containerAppName    = 'zolab-bot-ca-${suffix}'

// ── Built-in RBAC Role Definition IDs ─────────────────────────
var roles = {
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
  acrPull:     '7f951dda-4ed3-4680-a7ca-43fe172d538d'
}

// ════════════════════════════════════════════════════════════════
//  RESOURCES
// ════════════════════════════════════════════════════════════════

// ── User-Assigned Managed Identity ──────────────────────────────
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

// ── Container Apps Environment ──────────────────────────────────
// Logs go to DIBSecCom LAW in the Security subscription (cross-sub)
resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalyticsSharedKey
      }
    }
  }
}

// ── Container App (Bot Web Server) ──────────────────────────────
resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${botManagedIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: botManagedIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'zolab-bot'
          image: '${acr.properties.loginServer}/zolab-bot:${botImageTag}'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat(
            [
              {
                name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE'
                value: 'UserManagedIdentity'
              }
              {
                name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID'
                value: botManagedIdentity.properties.clientId
              }
              {
                name: 'AZURE_MANAGED_IDENTITY_NAME'
                value: botManagedIdentity.name
              }
              {
                name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID'
                value: tenantId
              }
            ],
            [
              {
                name: 'AZURE_CLIENT_ID'
                value: botManagedIdentity.properties.clientId
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
                name: 'AZURE_SUBSCRIPTION_ID'
                value: subscription().subscriptionId
              }
              {
                name: 'WORKER_ENABLED'
                value: 'false'
              }
              {
                name: 'HEARTBEAT_ENABLED'
                value: 'true'
              }
            ]
          )
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
        rules: [
          {
            name: 'http-scaling'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
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
    displayName: 'Bot-The-Builder'
    iconUrl: 'https://${containerApp.properties.configuration.ingress.fqdn}/bot-icon-${botImageTag}.png'
    endpoint: 'https://${containerApp.properties.configuration.ingress.fqdn}/api/messages'
    msaAppId: botManagedIdentity.properties.clientId
    msaAppTenantId: tenantId
    msaAppType: 'UserAssignedMSI'
    msaAppMSIResourceId: botManagedIdentity.id
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

output containerAppName string = containerApp.name
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output containerAppUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output managedIdentityPrincipalId string = botManagedIdentity.properties.principalId
output managedIdentityClientId string = botManagedIdentity.properties.clientId
output managedIdentityResourceId string = botManagedIdentity.id
output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output botServiceName string = azureBot.name
output messagingEndpoint string = 'https://${containerApp.properties.configuration.ingress.fqdn}/api/messages'
