// ════════════════════════════════════════════════════════════════
// bot-resources.bicep — App Service + Managed Identity for the bot
//
// Deployed at resource-group scope by bot-infra.bicep.
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

@description('Python runtime version')
param pythonVersion string

// ── Resource Names ────────────────────────────────────────────
var appServicePlanName = 'zolab-bot-plan-${suffix}'
var appServiceName     = 'zolab-bot-app-${suffix}'

// ── Built-in RBAC Role Definition IDs ─────────────────────────
var roles = {
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
}

// ════════════════════════════════════════════════════════════════
//  RESOURCES
// ════════════════════════════════════════════════════════════════

// ── App Service Plan (Linux) ──────────────────────────────────
resource appServicePlan 'Microsoft.Web/serverfarms@2024-04-01' = {
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

// ── App Service (Python + Managed Identity) ───────────────────
resource appService 'Microsoft.Web/sites@2024-04-01' = {
  name: appServiceName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|${pythonVersion}'
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      alwaysOn: true
      appCommandLine: 'python -m aiohttp.web -H 0.0.0.0 -P 8000 app:create_app'
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
          value: ''  // Empty — uses Managed Identity
        }
        {
          name: 'PORT'
          value: '8000'
        }
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
        {
          name: 'WEBSITE_RUN_FROM_PACKAGE'
          value: '0'
        }
      ]
    }
  }
}

// ── RBAC: Managed Identity → Contributor on own RG ────────────
resource botMIContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, appService.name, roles.contributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
    principalId: appService.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ════════════════════════════════════════════════════════════════
//  OUTPUTS
// ════════════════════════════════════════════════════════════════

output appServiceName string = appService.name
output appServiceUrl string = 'https://${appService.properties.defaultHostName}'
output managedIdentityPrincipalId string = appService.identity.principalId
