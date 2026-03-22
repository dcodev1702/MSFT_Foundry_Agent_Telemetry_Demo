// ════════════════════════════════════════════════════════════════
// worker-infra.bicepparam — Parameters for Worker infrastructure
//
// Usage:
//   az deployment sub create \
//     --location eastus2 \
//     --parameters worker-infra.bicepparam
// ════════════════════════════════════════════════════════════════
using './worker-infra.bicep'

// ── Required (override at deploy time) ────────────────────────
param suffix   = 'botprd'

// ── Optional ─────────────────────────────────────────────────
param location = 'eastus2'
param workerCpu = 2
param workerMemoryInGb = 4
param workerImageTag = 'latest'
param enablePrivateStorageAccess = true
param workerVnetAddressPrefix = '10.42.0.0/24'
param containerAppsSubnetAddressPrefix = '10.42.0.0/27'
param workerSubnetAddressPrefix = '10.42.0.32/28'
param privateEndpointSubnetAddressPrefix = '10.42.0.48/28'
