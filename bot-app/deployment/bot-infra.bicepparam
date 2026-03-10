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
param suffix   = 'botprd'
param botAppId = 'ed77d99f-074b-4ef6-9fbc-55bfeb7b5aef'
param tenantId = 'b22dee98-83da-4207-b9ab-5ba931866f44'

// ── Optional (defaults are fine for dev/pilot) ────────────────
param location           = 'eastus2'
param appServicePlanSku  = 'B1'
