# Deploy (or clean up) Azure AI Foundry Environment
# Pre-req: Az PowerShell, Microsoft.Graph PowerShell, Azure CLI with Bicep
# Usage:
#   .\deploy-foundry-env.ps1              # deploy all resources + RBAC
#   .\deploy-foundry-env.ps1 -Cleanup     # tear down resources + RBAC (keeps Entra group)
param(
    [switch]$Cleanup
)

$ErrorActionPreference = "Stop"

# ── Configuration ──
$subscriptionId         = "08fdc492-f5aa-4601-84ae-03a37449c2ba"   # zolab
$securitySubscriptionId = "192ad012-896e-4f14-8525-c37a2a9640f9"   # Security (hosts DIBSecCom LAW)
$location               = "eastus2"
$groupDisplayName       = "zolab-ai-dev"

# ── 1. Ensure Microsoft.Graph.Groups module ──
if (-not (Get-Module -ListAvailable -Name Microsoft.Graph.Groups)) {
    Write-Host "Installing Microsoft.Graph.Groups module..."
    Install-Module Microsoft.Graph.Groups -Scope CurrentUser -Force
}
Import-Module Microsoft.Graph.Groups

# ── 2. Connect to Microsoft Graph (if needed) ──
$ctx = Get-MgContext
if (-not $ctx) {
    Write-Host "Connecting to Microsoft Graph..."
    Connect-MgGraph -Scopes "Group.ReadWrite.All"
}

# ── 3. Set Azure subscription context (both Az PowerShell and az CLI) ──
Set-AzContext -SubscriptionId $subscriptionId | Out-Null
az account set --subscription $subscriptionId 2>&1 | Out-Null
Write-Host "Subscription set to zolab ($subscriptionId)"

# ════════════════════════════════════════════════════════════════
#  CLEANUP MODE
# ════════════════════════════════════════════════════════════════
if ($Cleanup) {
    Write-Host ""
    Write-Host "=== CLEANUP MODE ==="

    # ── Resolve the zolab-ai-dev group ──
    $group = Get-MgGroup -Filter "displayName eq '$groupDisplayName'" -ErrorAction SilentlyContinue
    if ($group) {
        $groupObjectId = $group.Id
        Write-Host "Found Entra group '$groupDisplayName' — ObjectId: $groupObjectId"

        # ── Remove all RBAC role assignments for the group across the subscription ──
        Write-Host "Removing RBAC role assignments for '$groupDisplayName'..."
        $assignments = Get-AzRoleAssignment -ObjectId $groupObjectId -ErrorAction SilentlyContinue
        foreach ($a in $assignments) {
            Write-Host "  Removing: $($a.RoleDefinitionName) @ $($a.Scope)"
            Remove-AzRoleAssignment -ObjectId $groupObjectId `
                -RoleDefinitionName $a.RoleDefinitionName `
                -Scope $a.Scope `
                -ErrorAction SilentlyContinue
        }
        if (-not $assignments) { Write-Host "  No role assignments found." }
    } else {
        Write-Host "Entra group '$groupDisplayName' not found — skipping RBAC cleanup."
    }

    # ── Remove resource groups matching zolab-ai-<suffix> (not the original zolab-ai) ──
    $rgList = Get-AzResourceGroup | Where-Object { $_.ResourceGroupName -match '^zolab-ai-.{4,}$' }
    foreach ($rg in $rgList) {
        Write-Host "Deleting resource group '$($rg.ResourceGroupName)'..."
        # Purge Cognitive Services to avoid soft-delete conflicts on redeploy
        $cogAccounts = Get-AzResource -ResourceGroupName $rg.ResourceGroupName `
            -ResourceType "Microsoft.CognitiveServices/accounts" -ErrorAction SilentlyContinue
        foreach ($cog in $cogAccounts) {
            Write-Host "  Will purge Cognitive Services account '$($cog.Name)' after RG deletion."
        }
        Remove-AzResourceGroup -Name $rg.ResourceGroupName -Force
        # Purge soft-deleted Cognitive Services accounts
        foreach ($cog in $cogAccounts) {
            Write-Host "  Purging soft-deleted account '$($cog.Name)'..."
            az cognitiveservices account purge `
                --name $cog.Name `
                --resource-group $rg.ResourceGroupName `
                --location $location `
                --subscription $subscriptionId 2>&1 | Out-Null
        }
    }
    if (-not $rgList) { Write-Host "No zolab-ai-<suffix> resource groups found." }

    # ── Remove subscription-level deployment records (zolab) ──
    $deployments = Get-AzSubscriptionDeployment | Where-Object { $_.DeploymentName -like 'foundry-ai-env*' }
    foreach ($d in $deployments) {
        Write-Host "Removing deployment record '$($d.DeploymentName)'..."
        Remove-AzSubscriptionDeployment -Name $d.DeploymentName -ErrorAction SilentlyContinue
    }

    # ── Remove LAW RBAC role assignment + deployment record from Security subscription ──
    $securitySubId = $securitySubscriptionId
    Write-Host "Cleaning up LAW RBAC in Security subscription..."
    Set-AzContext -SubscriptionId $securitySubId | Out-Null

    # Remove the actual role assignment on DIBSecCom workspace
    if ($group) {
        $lawScope = "/subscriptions/$securitySubId/resourceGroups/Sentinel/providers/Microsoft.OperationalInsights/workspaces/DIBSecCom"
        $lawAssignments = Get-AzRoleAssignment -ObjectId $groupObjectId -Scope $lawScope -ErrorAction SilentlyContinue
        foreach ($a in $lawAssignments) {
            Write-Host "  Removing: $($a.RoleDefinitionName) @ $($a.Scope)"
            Remove-AzRoleAssignment -ObjectId $groupObjectId `
                -RoleDefinitionName $a.RoleDefinitionName `
                -Scope $a.Scope `
                -ErrorAction SilentlyContinue
        }
        if (-not $lawAssignments) { Write-Host "  No LAW role assignments found." }
    }

    # Remove deployment records
    $lawDeploys = Get-AzSubscriptionDeployment | Where-Object { $_.DeploymentName -like 'law-rbac*' }
    foreach ($d in $lawDeploys) {
        Write-Host "Removing deployment record '$($d.DeploymentName)'..."
        Remove-AzSubscriptionDeployment -Name $d.DeploymentName -ErrorAction SilentlyContinue
    }

    # Restore zolab context
    Set-AzContext -SubscriptionId $subscriptionId | Out-Null

    Write-Host ""
    Write-Host "=== Cleanup complete ==="
    exit 0
}

# ════════════════════════════════════════════════════════════════
#  DEPLOY MODE
# ════════════════════════════════════════════════════════════════

# ── Create or retrieve the zolab-ai-dev Entra security group ──
$existingGroup = Get-MgGroup -Filter "displayName eq '$groupDisplayName'" -ErrorAction SilentlyContinue
if ($existingGroup) {
    Write-Host "Entra group '$groupDisplayName' already exists — ObjectId: $($existingGroup.Id)"
    $groupObjectId = $existingGroup.Id
} else {
    Write-Host "Creating Entra security group '$groupDisplayName'..."
    $newGroup = New-MgGroup `
        -DisplayName $groupDisplayName `
        -MailEnabled:$false `
        -SecurityEnabled:$true `
        -MailNickname "zolab-ai-dev"
    $groupObjectId = $newGroup.Id
    Write-Host "Created group '$groupDisplayName' — ObjectId: $groupObjectId"
}

# ── Deploy Bicep (subscription-scoped via az cli) ──
Write-Host ""
Write-Host "Deploying AI Foundry environment..."
$deployOutput = az deployment sub create `
    --location $location `
    --template-file "$PSScriptRoot\main.bicep" `
    --name "foundry-ai-env-deployment" `
    --parameters aiDevGroupObjectId=$groupObjectId `
    --output json 2>&1

# Separate warnings/stderr from JSON output
$rawOutput = $deployOutput -join "`n"
# Extract the JSON object by finding the first { and last }
$jsonStart = $rawOutput.IndexOf('{')
$jsonEnd = $rawOutput.LastIndexOf('}')
if ($jsonStart -ge 0 -and $jsonEnd -gt $jsonStart) {
    $jsonString = $rawOutput.Substring($jsonStart, $jsonEnd - $jsonStart + 1)
} else {
    Write-Error "No JSON found in deployment output:`n$rawOutput"
    exit 1
}

if ($LASTEXITCODE -ne 0) {
    Write-Error "Deployment failed:`n$rawOutput"
    exit 1
}

$result = $jsonString | ConvertFrom-Json

Write-Host ""
Write-Host "=== Deployment Result ==="
Write-Host "ProvisioningState : $($result.properties.provisioningState)"
Write-Host "Resource Group    : $($result.properties.outputs.resourceGroupName.value)"
Write-Host "Suffix            : $($result.properties.outputs.suffix.value)"
Write-Host "Storage Account   : $($result.properties.outputs.storageAccountName.value)"
Write-Host "Key Vault         : $($result.properties.outputs.keyVaultName.value)"
Write-Host "App Insights      : $($result.properties.outputs.appInsightsName.value)"
Write-Host "AI Foundry        : $($result.properties.outputs.aiFoundryName.value)"
Write-Host "AI Project        : $($result.properties.outputs.aiProjectName.value)"
Write-Host ""
Write-Host "Portal link:"
Write-Host "https://portal.azure.com/#@/resource/subscriptions/$subscriptionId/resourceGroups/$($result.properties.outputs.resourceGroupName.value)/overview"

# ── Deploy LAW RBAC to Security subscription (Log Analytics Reader on DIBSecCom) ──
$securitySubId = $securitySubscriptionId
Write-Host ""
Write-Host "Deploying Log Analytics Reader RBAC to Security subscription..."
az account set --subscription $securitySubId 2>&1 | Out-Null

$lawOutput = az deployment sub create `
    --location $location `
    --template-file "$PSScriptRoot\law-rbac.bicep" `
    --name "law-rbac-zolab-ai-dev" `
    --parameters aiDevGroupObjectId=$groupObjectId `
    --output json 2>&1

# Capture exit code before running any other commands
$lawExitCode = $LASTEXITCODE

$lawJson = $lawOutput | Where-Object { $_ -notmatch '^WARNING:|^BCP\d|\.bicep\(' }
$lawWarnings = $lawOutput | Where-Object { $_ -match '^WARNING:|^BCP\d|\.bicep\(' }
if ($lawWarnings) { $lawWarnings | ForEach-Object { Write-Host $_ } }

# Restore zolab subscription context
az account set --subscription $subscriptionId 2>&1 | Out-Null

if ($lawExitCode -ne 0) {
    Write-Error "LAW RBAC deployment failed:`n$($lawOutput -join "`n")"
    exit 1
}

Write-Host "  Log Analytics Reader assigned to '$groupDisplayName' on DIBSecCom workspace."
