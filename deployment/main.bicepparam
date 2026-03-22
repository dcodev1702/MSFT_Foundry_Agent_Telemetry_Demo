// ════════════════════════════════════════════════════════════════
// main.bicepparam — Central configuration for all Bicep values
// Single source of truth for naming, SKUs, retention, and LAW refs.
//
// Usage:
//   az deployment sub create --location eastus2 --parameters main.bicepparam
//
// NOTE: params marked <DEPLOY_SCRIPT> are set dynamically by
//       deploy-foundry-env.ps1 — override them here for manual runs
//       or leave them for the script to supply via --parameters.
// ════════════════════════════════════════════════════════════════
using './main.bicep'

// ── Identity & Subscription (supplied by deploy script) ──
param aiDevGroupObjectId    = '<DEPLOY_SCRIPT>'
param securitySubscriptionId = '<DEPLOY_SCRIPT>'
param suffix                = '<DEPLOY_SCRIPT>'

// ── AI Model (supplied by deploy script) ──
param aiModelDeploymentName = '<DEPLOY_SCRIPT>'
param aiModelName           = '<DEPLOY_SCRIPT>'
param aiModelFormat         = '<DEPLOY_SCRIPT>'
param aiModelVersion        = '<DEPLOY_SCRIPT>'
param aiModelSkuName        = '<DEPLOY_SCRIPT>'
param aiModelSkuCapacity    = 250

// ── Environment ──
param location              = 'eastus2'

// ── Naming ──
param resourceGroupPrefix   = 'zolab-ai'
param namePrefix            = 'zolabai'

// ── Log Analytics Workspace (cross-subscription) ──
param lawWorkspaceName      = 'DIBSecCom'

// ── Storage Account ──
param storageSkuName        = 'Standard_LRS'
param storageAccessTier     = 'Hot'

// ── Key Vault ──
param kvSoftDeleteRetentionDays = 7

// ── Application Insights ──
param appInsightsRetentionDays = 90

// ── AI Foundry (Cognitive Services) ──
param aiServicesSkuName     = 'S0'
