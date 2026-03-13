// ════════════════════════════════════════════════════════════════
// bot-infra.bicepparam — Parameters for Bot infrastructure
//
// Usage:
//   az deployment sub create \
//     --location eastus2 \
//     --parameters bot-infra.bicepparam
//
// NOTE: logAnalyticsCustomerId and logAnalyticsSharedKey MUST be supplied.
//       Override them via CLI --parameters or replace placeholders.
// ════════════════════════════════════════════════════════════════
using './bot-infra.bicep'

// ── Required ───────────────────────────────────────────────────
param suffix = 'botprd'
param tenantId = 'b22dee98-83da-4207-b9ab-5ba931866f44'
param logAnalyticsCustomerId = '<dibseccom-customer-id>'
param logAnalyticsSharedKey = '<dibseccom-shared-key>'

// ── Optional (defaults are fine for dev/pilot) ────────────────
param location = 'eastus2'
param botResourceGroupName = 'zolab-bot-botprd'
param botImageTag = 'latest'
param containerEnvName = 'zolab-bot-env-botprd-vnet'
param containerAppName = 'zolab-bot-ca-botprd-vnet'
param enablePrivateContainerAppsNetworking = true
param containerAppsInfrastructureSubnetResourceId = '/subscriptions/08fdc492-f5aa-4601-84ae-03a37449c2ba/resourceGroups/zolab-worker-botprd/providers/Microsoft.Network/virtualNetworks/zolab-worker-vnet-botprd/subnets/snet-containerapps'
