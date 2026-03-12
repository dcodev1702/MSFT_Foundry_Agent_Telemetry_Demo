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

@description('Bot App Registration Client ID')
param botAppId string

@description('Entra Tenant ID')
param tenantId string

@secure()
@description('Bot App Registration Client Secret (set empty string initially, update post-deploy)')
param botAppSecret string = ''

@description('Log Analytics Workspace customer (workspace) ID — DIBSecCom in Security sub')
param logAnalyticsCustomerId string

@secure()
@description('Log Analytics Workspace shared key — DIBSecCom in Security sub')
param logAnalyticsSharedKey string

@description('Log Analytics Workspace resource ID — DIBSecCom in Security sub')
param logAnalyticsWorkspaceResourceId string

@description('Object ID of the shared operator group to grant Key Vault secret access')
param operatorGroupPrincipalId string = ''

@description('Bot app registration display name')
param botAppRegistrationName string = 'unknown-app-registration'

@description('Bot container image tag to deploy from ACR')
param botImageTag string = 'latest'

// ── Resource Names ────────────────────────────────────────────
var managedIdentityName = 'zolab-bot-mi-${suffix}'
var acrName             = 'zolabbotacr${suffix}'
var botServiceName      = 'zolab-bot-${suffix}'
var containerEnvName    = 'zolab-bot-env-${suffix}'
var containerAppName    = 'zolab-bot-ca-${suffix}'
var keyVaultName        = 'zolabbotkv${suffix}'
var botSecretName       = 'bot-app-client-secret'
var botSecretKeyVaultUrl = 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/${botSecretName}'

// ── Built-in RBAC Role Definition IDs ─────────────────────────
var roles = {
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
  acrPull:     '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
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

// ── Key Vault ──────────────────────────────────────────────────
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
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource keyVaultDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${keyVaultName}-audit'
  scope: keyVault
  properties: {
    workspaceId: logAnalyticsWorkspaceResourceId
    logAnalyticsDestinationType: 'AzureDiagnostics'
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
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

resource botManagedIdentityKeyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, botManagedIdentity.name, roles.keyVaultSecretsUser)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
    principalId: botManagedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource operatorGroupKeyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorGroupPrincipalId)) {
  scope: keyVault
  name: guid(keyVault.id, operatorGroupPrincipalId, roles.keyVaultSecretsUser)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
    principalId: operatorGroupPrincipalId
    principalType: 'Group'
  }
}

resource botAppSecretResource 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = if (!empty(botAppSecret)) {
  parent: keyVault
  name: botSecretName
  properties: {
    value: botAppSecret
  }
}

// ── Container App (Bot Web Server) ──────────────────────────────
resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  dependsOn: [
    botManagedIdentityKeyVaultSecretsUser
  ]
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
      secrets: [
        {
          name: 'bot-app-secret'
          keyVaultUrl: botSecretKeyVaultUrl
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
                name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID'
                value: botAppId
              }
              {
                name: 'BOT_APP_REGISTRATION_NAME'
                value: botAppRegistrationName
              }
              {
                name: 'AZURE_MANAGED_IDENTITY_NAME'
                value: botManagedIdentity.name
              }
              {
                name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID'
                value: tenantId
              }
              {
                name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET'
                secretRef: 'bot-app-secret'
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

output containerAppName string = containerApp.name
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output containerAppUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output managedIdentityPrincipalId string = botManagedIdentity.properties.principalId
output managedIdentityClientId string = botManagedIdentity.properties.clientId
output managedIdentityResourceId string = botManagedIdentity.id
output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output keyVaultName string = keyVault.name
output keyVaultSecretUri string = botSecretKeyVaultUrl
output botServiceName string = azureBot.name
output messagingEndpoint string = 'https://${containerApp.properties.configuration.ingress.fqdn}/api/messages'
