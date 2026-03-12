// ════════════════════════════════════════════════════════════════
// worker-infra.bicepparam — Parameters for Worker infrastructure
//
// Usage:
//   az deployment sub create \
//     --location eastus2 \
//     --parameters worker-infra.bicepparam \
//     --parameters botAppSecret='<secret>'
//
// NOTE: botAppSecret is optional; leave it unset to use managed identity.
// ════════════════════════════════════════════════════════════════
using './worker-infra.bicep'

// ── Required (override at deploy time) ────────────────────────
param suffix   = 'botprd'
param botAppId = 'ed77d99f-074b-4ef6-9fbc-55bfeb7b5aef'
param tenantId = 'b22dee98-83da-4207-b9ab-5ba931866f44'

// ── Existing UAMI (created in zolab-bot-botprd RG) ──────────
param managedIdentityResourceId  = '/subscriptions/08fdc492-f5aa-4601-84ae-03a37449c2ba/resourcegroups/zolab-bot-botprd/providers/Microsoft.ManagedIdentity/userAssignedIdentities/zolab-bot-mi-botprd'
param managedIdentityPrincipalId = 'e9a17b6f-74e3-44f4-ae3e-14dd48d5c251'
param managedIdentityClientId    = '59bffc04-c429-4580-9833-8ce88c088877'

// ── Optional ─────────────────────────────────────────────────
param location = 'eastus2'
param workerCpu = 2
param workerMemoryInGb = 4
param workerImageTag = 'latest'
