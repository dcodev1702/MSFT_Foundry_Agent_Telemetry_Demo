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
param logAnalyticsCustomerId string = ''

@secure()
@description('Log Analytics Workspace shared key — DIBSecCom in Security sub')
param logAnalyticsSharedKey string = ''

@description('Bot container image tag to deploy from ACR')
param botImageTag string = 'latest'

@description('Deployment name exposed to the bot runtime for its long-lived LLM deployment')
param weatherLlmModel string = 'gpt-5.3-chat'

@description('Azure OpenAI API version for grounded weather narration')
param weatherLlmApiVersion string = '2024-10-21'

@description('Stable Azure OpenAI model name for the bot-owned weather deployment')
param weatherLlmModelName string = 'gpt-5.3-chat'

@description('Stable Azure OpenAI model version for the bot-owned weather deployment')
param weatherLlmModelVersion string = '2026-03-03'

@description('Model format for the bot-owned weather deployment')
param weatherLlmModelFormat string = 'OpenAI'

@description('SKU name for the bot-owned weather deployment')
param weatherLlmSkuName string = 'GlobalStandard'

@description('SKU capacity for the bot-owned weather deployment')
param weatherLlmSkuCapacity int = 50

@description('Optional override for the Container Apps environment name')
param containerEnvName string = ''

@description('Optional override for the Container App name')
param containerAppName string = ''

@description('Heartbeat broadcast interval for the bot web app in seconds')
param heartbeatIntervalSeconds int = 14400

@description('Enable custom VNet integration for the Container Apps environment')
param enablePrivateContainerAppsNetworking bool = false

@description('Resource ID of the delegated infrastructure subnet for the Container Apps environment')
param containerAppsInfrastructureSubnetResourceId string = ''

// ── Resource Names ────────────────────────────────────────────
var managedIdentityName = 'zolab-bot-mi-${suffix}'
var acrName             = 'zolabbotacr${suffix}'
var botServiceName      = 'zolab-bot-${suffix}'
var workerStorageAccountName = 'zolabworkerst${suffix}'
var resolvedContainerEnvName = empty(containerEnvName) ? 'zolab-bot-env-${suffix}' : containerEnvName
var resolvedContainerAppName = empty(containerAppName) ? 'zolab-bot-ca-${suffix}' : containerAppName
var botLlmAccountName   = 'zolab-bot-llm-${suffix}'

// ── Built-in RBAC Role Definition IDs ─────────────────────────
var roles = {
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
  acrPull:     '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  azureAIUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d'
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
  name: resolvedContainerEnvName
  location: location
  properties: union(empty(logAnalyticsCustomerId) || empty(logAnalyticsSharedKey) ? {} : {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalyticsSharedKey
      }
    }
  }, enablePrivateContainerAppsNetworking ? {
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    vnetConfiguration: {
      infrastructureSubnetId: containerAppsInfrastructureSubnetResourceId
      internal: false
    }
  } : {})
}

// ── Stable AI Services account for bot-owned LLM features ─────
resource botLlmAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: botLlmAccountName
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: botLlmAccountName
    publicNetworkAccess: 'Enabled'
  }
}

resource botLlmDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: botLlmAccount
  name: weatherLlmModel
  sku: {
    name: weatherLlmSkuName
    capacity: weatherLlmSkuCapacity
  }
  properties: {
    model: {
      format: weatherLlmModelFormat
      name: weatherLlmModelName
      version: weatherLlmModelVersion
    }
  }
}

resource botMIAzureAIUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: botLlmAccount
  name: guid(botLlmAccount.id, botManagedIdentity.name, roles.azureAIUser)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.azureAIUser)
    principalId: botManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── Container App (Bot Web Server) ──────────────────────────────
resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: resolvedContainerAppName
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
                value: workerStorageAccountName
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
              {
                name: 'HEARTBEAT_INTERVAL_SECONDS'
                value: string(heartbeatIntervalSeconds)
              }
              {
                name: 'MSFT_LEARN_MCP_URL'
                value: 'https://learn.microsoft.com/api/mcp'
              }
              {
                name: 'MSFT_LEARN_MCP_TIMEOUT_SECONDS'
                value: '20'
              }
              {
                name: 'WEATHER_LLM_ENABLED'
                value: 'true'
              }
              {
                name: 'WEATHER_LLM_AZURE_OPENAI_ENDPOINT'
                value: botLlmAccount.properties.endpoint
              }
              {
                name: 'WEATHER_LLM_MODEL'
                value: weatherLlmModel
              }
              {
                name: 'WEATHER_LLM_API_VERSION'
                value: weatherLlmApiVersion
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
output botLlmAccountName string = botLlmAccount.name
output botLlmEndpoint string = botLlmAccount.properties.endpoint
output botLlmDeploymentName string = botLlmDeployment.name
