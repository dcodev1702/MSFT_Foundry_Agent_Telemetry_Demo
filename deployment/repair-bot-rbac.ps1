#Requires -Version 7.0
<#
.SYNOPSIS
  Re-applies the bot/worker managed-identity RBAC and restarts the back-end worker
  ACI after the MCAPSGov-AutomationApp governance sweep strips the role assignments.

.DESCRIPTION
  Background
  ─────────
  The 'MCAPSGov-AutomationApp' service principal
  (appId eadea216-1d5c-4a4b-beaf-4f145e6b1cb4) periodically DELETES every role
  assignment on the shared bot User-Assigned Managed Identity
  (zolab-bot-mi-<suffix>). It last swept on 2026-05-22 ~10:17 UTC.

  When that happens:
    • Front-end Teams bot loses Storage data-plane access  -> AuthorizationPermissionMismatch
    • Worker ACI can no longer pull its image              -> stuck Failed / "Waiting to run"
    • Weather LLM narration silently falls back to a template
    • Foundry builds (which create new resource groups) fail

  This script restores the full role-assignment footprint defined in the
  deployment Bicep (bot-resources.bicep + worker-resources.bicep) and restarts
  the worker ACI. Role assignments are made by role-definition GUID so a tenant
  role rename (e.g. "Azure AI User" -> "Foundry User") does not break it.
  Re-running is safe: 'az role assignment create' is idempotent.

  Restored by default (the declarative IaC footprint)
  ───────────────────────────────────────────────────
    Bot stack  (RG zolab-bot-<suffix>):
      • AcrPull                          on bot ACR     zolabbotacr<suffix>
      • Foundry User (was Azure AI User) on bot LLM     zolab-bot-llm-<suffix>
      • Contributor                      on bot RG      zolab-bot-<suffix>
    Worker stack (RG zolab-worker-<suffix>):
      • AcrPull                          on worker ACR  zolabworkeracr<suffix>
      • Storage Queue Data Contributor   on worker stg  zolabworkerst<suffix>
      • Storage Blob Data Contributor    on worker stg  zolabworkerst<suffix>

  Optional (-IncludeSubscriptionRoles, OFF by default)
  ────────────────────────────────────────────────────
      • Contributor + User Access Administrator at zolab SUBSCRIPTION scope.
      • Reader at Security SUBSCRIPTION scope.
      • User Access Administrator on the DIBSecCom LAW workspace.
      • zolab-ai-dev Reader on active zolab-ai-* build resource groups.
      • zolab-ai-dev Log Analytics Reader on the DIBSecCom LAW workspace.
        These let the worker run Foundry environment *builds* (which create new
        resource groups and their own role assignments, including cross-sub LAW
        RBAC). They are NOT in the per-RG Bicep — they were granted out-of-band
        during original setup and are inferred from the deploy-foundry-env.ps1
        preflight/build-status requirements. Enable only if Foundry builds or
        build-status checks fail after the default recovery. These are
        security-sensitive grants — review before using.

.PARAMETER Suffix
  Environment suffix used in all resource names. Default: botprd

.PARAMETER SubscriptionId
  Target subscription ID. Default: the zolab subscription.

.PARAMETER SecuritySubscriptionId
  Security subscription ID that hosts the DIBSecCom Log Analytics workspace.

.PARAMETER AiDevGroupDisplayName
  Entra security group used by Foundry builds for notebook/App Insights access.

.PARAMETER IncludeSubscriptionRoles
  Also grant the out-of-band worker Foundry-build roles at zolab subscription,
  Security subscription, DIBSecCom workspace, and active build resource-group
  scopes. OFF by default.

.PARAMETER SkipWorkerRestart
  Do not restart the worker ACI (only re-apply RBAC).

.PARAMETER PropagationSeconds
  Seconds to wait for RBAC propagation before restarting the worker ACI.
  Default: 90. Set 0 to restart immediately.

.EXAMPLE
  pwsh ./deployment/repair-bot-rbac.ps1
  Restore the 6 documented assignments, wait for propagation, restart the worker.

.EXAMPLE
  pwsh ./deployment/repair-bot-rbac.ps1 -WhatIf
  Show exactly what would change without making any changes.

.EXAMPLE
  pwsh ./deployment/repair-bot-rbac.ps1 -IncludeSubscriptionRoles
  Also restore the subscription-scoped worker Foundry-build roles.

.NOTES
  Durable fix: request a governance exemption for the zolab-bot-<suffix> and
  zolab-worker-<suffix> resource groups so MCAPSGov-AutomationApp stops removing
  these assignments. This script is the stop-gap until that exemption is in place.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [string]$Suffix = 'botprd',
    [string]$SubscriptionId = '08fdc492-f5aa-4601-84ae-03a37449c2ba',
    [string]$SecuritySubscriptionId = '192ad012-896e-4f14-8525-c37a2a9640f9',
    [string]$LawResourceGroup = 'Sentinel',
    [string]$LawWorkspaceName = 'DIBSecCom',
    [string]$AiDevGroupDisplayName = 'zolab-ai-dev',
    [switch]$IncludeSubscriptionRoles,
    [switch]$SkipWorkerRestart,
    [ValidateRange(0, 600)]
    [int]$PropagationSeconds = 90
)

$ErrorActionPreference = 'Stop'

# ── Resource names (derived from suffix) ──────────────────────────
$botRg     = "zolab-bot-$Suffix"
$workerRg  = "zolab-worker-$Suffix"
$miName    = "zolab-bot-mi-$Suffix"
$botAcr    = "zolabbotacr$Suffix"
$botLlm    = "zolab-bot-llm-$Suffix"
$workerAcr = "zolabworkeracr$Suffix"
$workerStg = "zolabworkerst$Suffix"
$aciName   = "zolab-worker-aci-$Suffix"

# ── Built-in role definition GUIDs (assign by ID — rename-proof) ──
$ROLE = @{
    AcrPull                     = '7f951dda-4ed3-4680-a7ca-43fe172d538d'  # AcrPull
    FoundryUser                 = '53ca6127-db72-4b80-b1b0-d745d6d5456d'  # Foundry User (was "Azure AI User")
    Contributor                 = 'b24988ac-6180-42a0-ab88-20f7382dd24c'  # Contributor
    Reader                      = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'  # Reader
    LogAnalyticsReader          = '73c42c96-874c-492b-b04d-ab87d138a893'  # Log Analytics Reader
    StorageQueueDataContributor = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'  # Storage Queue Data Contributor
    StorageBlobDataContributor  = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'  # Storage Blob Data Contributor
    UserAccessAdministrator     = '18d7d88d-d35e-4fb5-a5c3-7773c20a72d9'  # User Access Administrator
}

function Write-Banner {
    param([string]$Text)
    Write-Host ""
    Write-Host "── $Text " -ForegroundColor Cyan -NoNewline
    Write-Host ("─" * [Math]::Max(0, 60 - $Text.Length)) -ForegroundColor Cyan
}

function Grant-RoleAssignment {
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [string]$PrincipalId,
        [string]$RoleId,
        [string]$Scope,
        [string]$Label,
        [ValidateSet('ServicePrincipal', 'Group')]
        [string]$PrincipalType = 'ServicePrincipal'
    )

      $existingCount = az role assignment list `
        --assignee-object-id $PrincipalId `
        --scope $Scope `
        --query "[?contains(roleDefinitionId, '$RoleId')] | length(@)" `
        -o tsv 2>$null
      if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($existingCount) -and [int]$existingCount -gt 0) {
        Write-Host "  [ensured] $Label" -ForegroundColor Green
        return $true
      }

    if (-not $PSCmdlet.ShouldProcess($Scope, "Grant '$Label'")) {
        return $true
    }
    $null = az role assignment create `
        --assignee-object-id $PrincipalId `
      --assignee-principal-type $PrincipalType `
        --role $RoleId `
        --scope $Scope `
        --output none 2>&1
    if ($LASTEXITCODE -ne 0) {
      $existingCount = az role assignment list `
        --assignee-object-id $PrincipalId `
        --scope $Scope `
        --query "[?contains(roleDefinitionId, '$RoleId')] | length(@)" `
        -o tsv 2>$null
      if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($existingCount) -and [int]$existingCount -gt 0) {
        Write-Host "  [ensured] $Label" -ForegroundColor Green
        return $true
      }

        Write-Warning "FAILED: $Label"
        return $false
    }
    Write-Host "  [ensured] $Label" -ForegroundColor Green
    return $true
}

# ── Preconditions ─────────────────────────────────────────────────
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI ('az') was not found on PATH. Install it and run 'az login' first."
}

Write-Banner "Targeting subscription"
az account set --subscription $SubscriptionId
if ($LASTEXITCODE -ne 0) {
    throw "Could not select subscription '$SubscriptionId'. Run 'az login' and confirm access."
}
$acct = az account show --query "{name:name, id:id, user:user.name}" -o json | ConvertFrom-Json
Write-Host "  $($acct.name)  ($($acct.id))  as $($acct.user)"

# ── Resolve the managed identity principalId (handles MI recreation) ──
Write-Banner "Resolving bot managed identity"
$principalId = (az identity show --name $miName --resource-group $botRg --query principalId -o tsv 2>$null)
if ([string]::IsNullOrWhiteSpace($principalId)) {
    throw "Could not resolve managed identity '$miName' in resource group '$botRg'. " +
          "Confirm the suffix ('$Suffix') and that the identity exists."
}
$clientId = (az identity show --name $miName --resource-group $botRg --query clientId -o tsv 2>$null)
Write-Host "  $miName"
Write-Host "    principalId : $principalId"
Write-Host "    clientId    : $clientId"

$aiDevGroupObjectId = $null
$foundryBuildResourceGroupNames = @()
if ($IncludeSubscriptionRoles) {
  Write-Banner "Resolving AI dev group and active builds"
  $aiDevGroupObjectId = (az ad group show --group $AiDevGroupDisplayName --query id -o tsv 2>$null)
  if ([string]::IsNullOrWhiteSpace($aiDevGroupObjectId)) {
    throw "Could not resolve Entra group '$AiDevGroupDisplayName'. Confirm it exists and that the current account can read Microsoft Graph directory objects."
  }

  $buildRgText = az group list --subscription $SubscriptionId --query "[?starts_with(name, 'zolab-ai-')].name" -o tsv 2>$null
  if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($buildRgText)) {
    $foundryBuildResourceGroupNames = @(
      $buildRgText -split "`r?`n" |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Sort-Object -Unique
    )
  }

  Write-Host "  $AiDevGroupDisplayName"
  Write-Host "    objectId : $aiDevGroupObjectId"
  if ($foundryBuildResourceGroupNames.Count -gt 0) {
    Write-Host "    active builds : $($foundryBuildResourceGroupNames -join ', ')"
  } else {
    Write-Host "    active builds : none found"
  }
}

# ── Scope strings ─────────────────────────────────────────────────
$subScope       = "/subscriptions/$SubscriptionId"
$securitySubScope = "/subscriptions/$SecuritySubscriptionId"
$botRgScope     = "$subScope/resourceGroups/$botRg"
$workerRgScope  = "$subScope/resourceGroups/$workerRg"
$botAcrScope    = "$botRgScope/providers/Microsoft.ContainerRegistry/registries/$botAcr"
$botLlmScope    = "$botRgScope/providers/Microsoft.CognitiveServices/accounts/$botLlm"
$workerAcrScope = "$workerRgScope/providers/Microsoft.ContainerRegistry/registries/$workerAcr"
$workerStgScope = "$workerRgScope/providers/Microsoft.Storage/storageAccounts/$workerStg"
$lawWorkspaceScope = "$securitySubScope/resourceGroups/$LawResourceGroup/providers/Microsoft.OperationalInsights/workspaces/$LawWorkspaceName"

# ── Assignment plan ───────────────────────────────────────────────
$assignments = @(
    @{ Id = $ROLE.AcrPull;                     Scope = $botAcrScope;    Label = "AcrPull -> bot ACR ($botAcr)" }
    @{ Id = $ROLE.FoundryUser;                 Scope = $botLlmScope;    Label = "Foundry User -> bot LLM ($botLlm)" }
    @{ Id = $ROLE.Contributor;                 Scope = $botRgScope;     Label = "Contributor -> bot RG ($botRg)" }
    @{ Id = $ROLE.AcrPull;                     Scope = $workerAcrScope; Label = "AcrPull -> worker ACR ($workerAcr)" }
    @{ Id = $ROLE.StorageQueueDataContributor; Scope = $workerStgScope; Label = "Storage Queue Data Contributor -> worker storage ($workerStg)" }
    @{ Id = $ROLE.StorageBlobDataContributor;  Scope = $workerStgScope; Label = "Storage Blob Data Contributor -> worker storage ($workerStg)" }
)

if ($IncludeSubscriptionRoles) {
    $assignments += @{ Id = $ROLE.Contributor;             Scope = $subScope; Label = "Contributor -> SUBSCRIPTION (worker Foundry builds)" }
    $assignments += @{ Id = $ROLE.UserAccessAdministrator; Scope = $subScope; Label = "User Access Administrator -> SUBSCRIPTION (worker Foundry builds)" }
  $assignments += @{ Id = $ROLE.Reader;                  Scope = $securitySubScope; Label = "Reader -> Security SUBSCRIPTION (worker subscription discovery)" }
  $assignments += @{ Id = $ROLE.UserAccessAdministrator; Scope = $lawWorkspaceScope; Label = "User Access Administrator -> $LawWorkspaceName LAW workspace (worker cross-sub LAW RBAC)" }
  $assignments += @{ Id = $ROLE.LogAnalyticsReader;      Scope = $lawWorkspaceScope; Label = "Log Analytics Reader -> $LawWorkspaceName LAW workspace ($AiDevGroupDisplayName)"; PrincipalId = $aiDevGroupObjectId; PrincipalType = 'Group' }

  foreach ($buildResourceGroupName in $foundryBuildResourceGroupNames) {
    $assignments += @{
      Id            = $ROLE.Reader
      Scope         = "$subScope/resourceGroups/$buildResourceGroupName"
      Label         = "Reader -> build RG $buildResourceGroupName ($AiDevGroupDisplayName)"
      PrincipalId   = $aiDevGroupObjectId
      PrincipalType = 'Group'
    }
  }
}

# ── Apply assignments ─────────────────────────────────────────────
Write-Banner "Restoring role assignments"
$failures = 0
foreach ($a in $assignments) {
  $targetPrincipalId = if ($a.PrincipalId) { $a.PrincipalId } else { $principalId }
  $targetPrincipalType = if ($a.PrincipalType) { $a.PrincipalType } else { 'ServicePrincipal' }
  if (-not (Grant-RoleAssignment -PrincipalId $targetPrincipalId -PrincipalType $targetPrincipalType -RoleId $a.Id -Scope $a.Scope -Label $a.Label)) {
        $failures++
    }
}
if ($failures -gt 0) {
    Write-Warning "$failures role assignment(s) failed. Re-run after confirming you have Owner / User Access Administrator on the affected scopes."
}

# ── Restart the worker ACI ────────────────────────────────────────
if (-not $SkipWorkerRestart) {
    if ($PropagationSeconds -gt 0 -and -not $WhatIfPreference) {
        Write-Banner "Waiting ${PropagationSeconds}s for RBAC propagation"
        Start-Sleep -Seconds $PropagationSeconds
    }
    Write-Banner "Restarting worker ACI"
    if ($PSCmdlet.ShouldProcess($aciName, "Restart worker ACI")) {
        $null = az container restart --resource-group $workerRg --name $aciName --output none 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Worker ACI restart failed. If it is in a 'Failed' provisioning state, recreate it via worker-infra.bicep:"
            Write-Warning "  az deployment sub create --location eastus2 --template-file deployment/worker-infra.bicep --parameters deployment/worker-infra.bicepparam"
        }
        else {
            Write-Host "  [ok] Restart issued for $aciName" -ForegroundColor Green
        }
    }
}
else {
    Write-Host "`n(Skipping worker ACI restart — '-SkipWorkerRestart' was supplied.)" -ForegroundColor DarkGray
}

# ── Verify ────────────────────────────────────────────────────────
if (-not $WhatIfPreference) {
    Write-Banner "Current managed-identity role assignments"
    az role assignment list --assignee $principalId --all `
        --query "[].{role:roleDefinitionName, scope:scope}" -o table

    if ($IncludeSubscriptionRoles -and $aiDevGroupObjectId) {
      Write-Banner "Current AI dev group role assignments"
      $verificationScopes = @($lawWorkspaceScope) + @(
        $foundryBuildResourceGroupNames | ForEach-Object { "$subScope/resourceGroups/$_" }
      )
      foreach ($scope in $verificationScopes) {
        az role assignment list --assignee-object-id $aiDevGroupObjectId --scope $scope `
          --query "[].{role:roleDefinitionName, scope:scope}" -o table
      }
    }

    if (-not $SkipWorkerRestart) {
        Write-Banner "Worker ACI state"
        az container show --resource-group $workerRg --name $aciName `
            --query "{provisioning:provisioningState, state:containers[0].instanceView.currentState.state, detail:containers[0].instanceView.currentState.detailStatus}" `
            -o table
    }
}

Write-Host ""
if ($failures -eq 0) {
    Write-Host "Recovery complete. Allow 1–2 min for data-plane RBAC to take full effect." -ForegroundColor Green
}
else {
    Write-Host "Recovery finished with $failures failure(s) — see warnings above." -ForegroundColor Yellow
}
Write-Host "Reminder: this recurs after each MCAPSGov sweep. Pursue a governance exemption for a durable fix." -ForegroundColor DarkGray
