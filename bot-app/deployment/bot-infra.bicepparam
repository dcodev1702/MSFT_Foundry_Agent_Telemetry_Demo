// ════════════════════════════════════════════════════════════════
// bot-infra.bicepparam — Parameters for Bot infrastructure
//
// Usage:
//   az deployment sub create \
//     --location eastus2 \
//     --parameters bot-infra.bicepparam
//
// NOTE: suffix, botAppId, and tenantId MUST be supplied.
//       Override them via CLI --parameters or replace placeholders.
// ════════════════════════════════════════════════════════════════
using './bot-infra.bicep'

// ── Required (override at deploy time) ────────────────────────
param suffix   = '<REQUIRED>'
param botAppId = '<REQUIRED>'
param tenantId = '<REQUIRED>'

// ── Optional (defaults are fine for dev/pilot) ────────────────
param location           = 'eastus2'
param appServicePlanSku  = 'B1'
param pythonVersion      = '3.11'
