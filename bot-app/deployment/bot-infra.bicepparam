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
