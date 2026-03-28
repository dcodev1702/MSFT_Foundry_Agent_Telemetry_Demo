# Deploy (or clean up) Azure AI Foundry Environment
# Pre-req: Az PowerShell, Microsoft.Graph PowerShell, Azure CLI with Bicep
# Usage:
#   pwsh ./deploy-foundry-env.ps1              # deploy all resources + RBAC
#   pwsh ./deploy-foundry-env.ps1 -UseTeamsChatFlow
#   pwsh ./deploy-foundry-env.ps1 -ListBuilds
#   pwsh ./deploy-foundry-env.ps1 -BuildStatusResourceGroup zolab-ai-abc123
#   pwsh ./deploy-foundry-env.ps1 -Cleanup -CleanupResourceGroup zolab-ai-abc123
#   pwsh ./deploy-foundry-env.ps1 -Cleanup -CleanupResourceGroup zolab-ai-abc123 -PreviewCleanup
#   pwsh ./deploy-foundry-env.ps1 -Cleanup     # tear down resources + RBAC (keeps Entra group)
param(
    [switch]$Cleanup,
    [string]$CleanupResourceGroup,
    [switch]$PreviewCleanup,
    [switch]$ListBuilds,
    [string]$BuildStatusResourceGroup,
    [switch]$UseTeamsChatFlow,
    [int]$TeamsChatSelectionTimeoutMinutes = 10,
    [string]$TeamsChatTopic = 'Microsoft Foundry Deployments',
    [string]$SelectedAiModel,   # Non-interactive model selection (bypasses PromptForChoice)
    [string]$RequestedBy,
    [string]$RequestedByObjectId,
    [string]$LawResourceGroup = 'Sentinel',
    [string]$LawWorkspaceName = 'DIBSecCom',
    [ValidateRange(5, 1440)]
    [int]$OrphanedBuildThresholdMinutes = 30,
    [ValidateRange(60, 7200)]
    [int]$DeploymentAzCliTimeoutSeconds = 1800,
    [ValidateRange(10, 900)]
    [int]$PostDeployAzCliTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'teams-chat.ps1')
. (Join-Path $PSScriptRoot 'foundry-identity.helpers.ps1')
. (Join-Path $PSScriptRoot 'foundry-teardown.helpers.ps1')

if ($CleanupResourceGroup) {
    $Cleanup = $true
}

if ($BuildStatusResourceGroup -and $Cleanup) {
    throw "Build status mode cannot be combined with cleanup mode."
}

if ($ListBuilds -and ($Cleanup -or $BuildStatusResourceGroup)) {
    throw "List builds mode cannot be combined with cleanup or build status mode."
}

if ($PreviewCleanup -and -not $Cleanup) {
    throw "Preview cleanup mode must be combined with cleanup mode."
}

if ($PreviewCleanup -and -not $CleanupResourceGroup) {
    throw "Preview cleanup mode currently supports only targeted cleanup. Specify -CleanupResourceGroup."
}

function Test-ManagedIdentityBootstrapAvailable {
    if ([string]::IsNullOrWhiteSpace($env:AZURE_CLIENT_ID)) {
        return $false
    }

    if (Test-Path '/.dockerenv') {
        return $true
    }

    $managedIdentitySignals = @(
        'IDENTITY_ENDPOINT',
        'IDENTITY_API_VERSION',
        'IDENTITY_HEADER',
        'MSI_ENDPOINT',
        'IMDS_ENDPOINT',
        'Fabric_ApplicationName',
        'Fabric_ServiceName',
        'CONTAINER_APP_NAME',
        'CONTAINER_APP_REVISION',
        'CONTAINER_GROUP_NAME',
        'WEBSITE_SITE_NAME',
        'WEBSITE_INSTANCE_ID'
    )

    foreach ($signal in $managedIdentitySignals) {
        if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($signal))) {
            return $true
        }
    }

    return $false
}

function Connect-AzWithManagedIdentityRetry {
    param(
        [Parameter(Mandatory)]
        [string]$ClientId,

        [int]$MaxAttempts = 5,

        [int]$InitialDelaySeconds = 2
    )

    $attempt = 1
    $delaySeconds = $InitialDelaySeconds
    $lastError = $null

    while ($attempt -le $MaxAttempts) {
        try {
            Connect-AzAccount -Identity -AccountId $ClientId -ErrorAction Stop -WarningAction SilentlyContinue | Out-Null
            return
        } catch {
            $lastError = $_
            if ($attempt -ge $MaxAttempts) {
                break
            }

            Write-Warning "Managed identity sign-in attempt $attempt/$MaxAttempts failed: $($_.Exception.Message)"
            Start-Sleep -Seconds $delaySeconds
            $attempt += 1
            $delaySeconds = [Math]::Min($delaySeconds * 2, 15)
        }
    }

    throw $lastError
}

# ── Auto-authenticate with Managed Identity when running in ACI/Container Apps ──
if (-not (Get-AzContext -ErrorAction SilentlyContinue)) {
    $miClientId = $env:AZURE_CLIENT_ID
    if ($miClientId -and (Test-ManagedIdentityBootstrapAvailable)) {
        Write-Host "No Az session — connecting via managed identity (AZURE_CLIENT_ID=$miClientId)..."
        Connect-AzWithManagedIdentityRetry -ClientId $miClientId
        Write-Host "Authenticated via managed identity."

        # Also authenticate Azure CLI with managed identity
        # NOTE: newer az CLI versions require --client-id (--username is deprecated)
        & az login --identity --client-id $miClientId --output none 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Azure CLI authenticated via managed identity."
        } else {
            Write-Host "WARNING: Azure CLI managed identity auth failed — retrying with legacy --username flag..."
            & az login --identity --username $miClientId --output none 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "Azure CLI authenticated via managed identity (legacy flag)."
            } else {
                Write-Host "WARNING: Azure CLI managed identity auth failed (non-fatal for list/status operations)."
            }
        }
    } elseif ($miClientId) {
        Write-Host "AZURE_CLIENT_ID is set, but no Azure managed-identity host markers were detected. Skipping managed identity bootstrap and using the local operator auth flow instead."
    }
}

# ── Configuration ──
$subscriptionId         = (Get-AzSubscription -SubscriptionName "zolab").Id
$securitySubscriptionId = (Get-AzSubscription -SubscriptionName "Security").Id
$location               = "eastus2"
$groupDisplayName       = "zolab-ai-dev"
$defaultModelCapacity   = 250

function Get-AzureCliContext {
    $cliContextJson = & az account show --query "{account:user.name,tenantId:tenantId,subscriptionId:id}" --output json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $cliContextJson) {
        return $null
    }

    $cliContextJson | ConvertFrom-Json
}

function Set-AzureSession {
    param(
        [Parameter(Mandatory)]
        [string]$SubscriptionId,

        [string]$ExpectedAccount,

        [switch]$SkipAzCliValidation
    )

    $targetSubscription = Get-AzSubscription -SubscriptionId $SubscriptionId -ErrorAction Stop
    $azContext = Get-AzContext -ErrorAction SilentlyContinue
    $needsConnect = -not $azContext -or -not $azContext.Account -or -not $azContext.Subscription

    if (-not $needsConnect) {
        $needsConnect = ($azContext.Subscription.Id -ne $SubscriptionId)
    }

    if (-not $needsConnect -and $ExpectedAccount) {
        $needsConnect = ($azContext.Account.Id -ine $ExpectedAccount)
    }

    if (-not $needsConnect -and $azContext.Tenant -and $azContext.Tenant.Id) {
        $needsConnect = ($azContext.Tenant.Id -ne $targetSubscription.TenantId)
    }

    if ($needsConnect) {
        Write-Host "Refreshing Azure PowerShell context for subscription '$($targetSubscription.Name)'..."
        $connectParams = @{
            Tenant       = $targetSubscription.TenantId
            Subscription = $SubscriptionId
            ErrorAction  = 'Stop'
        }
        if ($ExpectedAccount) {
            $connectParams.AccountId = $ExpectedAccount
        }

        Connect-AzAccount @connectParams | Out-Null
    }

    $azContext = Set-AzContext -SubscriptionId $SubscriptionId -ErrorAction Stop

    if ($ExpectedAccount -and $azContext.Account.Id -ine $ExpectedAccount) {
        throw "Azure PowerShell is signed in as '$($azContext.Account.Id)', but Teams chat flow requires '$ExpectedAccount'. Reauthenticate that account and restart the listener."
    }

    $cliAccount = $null
    if (-not $SkipAzCliValidation) {
        $cliContext = Get-AzureCliContext
        if (-not $cliContext) {
            throw "Azure CLI is not authenticated. Run 'az login --tenant $($targetSubscription.TenantId)' and restart the Teams listener."
        }

        if ($ExpectedAccount -and $cliContext.account -and $cliContext.account -ine $ExpectedAccount) {
            throw "Azure CLI is signed in as '$($cliContext.account)', but Teams chat flow requires '$ExpectedAccount'. Run 'az login --tenant $($targetSubscription.TenantId)' with that account and restart the listener."
        }

        & az account set --subscription $SubscriptionId 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to set Azure CLI subscription context to '$SubscriptionId'."
        }
        $cliAccount = $cliContext.account
    } else {
        # Best-effort az CLI setup (non-fatal for read-only operations)
        $cliContext = Get-AzureCliContext
        if ($cliContext) {
            & az account set --subscription $SubscriptionId 2>&1 | Out-Null
            $cliAccount = $cliContext.account
        } else {
            Write-Host "Azure CLI not authenticated (skipped — not required for this operation)."
        }
    }

    [pscustomobject]@{
        SubscriptionId    = $SubscriptionId
        SubscriptionName  = $targetSubscription.Name
        TenantId          = $targetSubscription.TenantId
        PowerShellAccount = $azContext.Account.Id
        CliAccount        = $cliAccount
    }
}

function Get-CurrentAzureRoleCheckAssignee {
    $miClientId = $env:AZURE_CLIENT_ID
    if (-not [string]::IsNullOrWhiteSpace($miClientId)) {
        return $miClientId
    }

    $azContext = Get-AzContext -ErrorAction SilentlyContinue
    if ($azContext -and $azContext.Account -and -not [string]::IsNullOrWhiteSpace($azContext.Account.Id)) {
        return $azContext.Account.Id
    }

    $null
}

function Get-AzureRoleNamesForAssignee {
    param(
        [Parameter(Mandatory)]
        [string]$Assignee,

        [Parameter(Mandatory)]
        [string]$Scope
    )

    $roleNamesJson = & az role assignment list `
        --assignee $Assignee `
        --scope $Scope `
        --query '[].roleDefinitionName' `
        --output json 2>$null

    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($roleNamesJson)) {
        return @()
    }

    try {
        $roleNames = @($roleNamesJson | ConvertFrom-Json)
        if ($roleNames.Count -gt 0) {
            return $roleNames
        }
    } catch {
        # Fall through to Az PowerShell lookup below.
    }

    try {
        $azRoleAssignments = if ($Assignee -match '@') {
            Get-AzRoleAssignment -Scope $Scope -SignInName $Assignee -ErrorAction Stop
        } else {
            Get-AzRoleAssignment -Scope $Scope -ObjectId $Assignee -ErrorAction Stop
        }

        return @(
            $azRoleAssignments |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_.RoleDefinitionName) } |
                Select-Object -ExpandProperty RoleDefinitionName -Unique
        )
    } catch {
        return @()
    }
}

function Test-AzureRoleAssignmentWriteAccess {
    param(
        [Parameter(Mandatory)]
        [string]$Assignee,

        [Parameter(Mandatory)]
        [string[]]$Scopes
    )

    foreach ($scope in $Scopes) {
        if ([string]::IsNullOrWhiteSpace($scope)) {
            continue
        }

        $roleNames = @(Get-AzureRoleNamesForAssignee -Assignee $Assignee -Scope $scope)
        if ($roleNames -contains 'Owner' -or $roleNames -contains 'User Access Administrator') {
            return $true
        }
    }

    $false
}

function Assert-FoundryDeploymentAuthorization {
    param(
        [Parameter(Mandatory)]
        [string]$WorkloadSubscriptionId,

        [Parameter(Mandatory)]
        [string]$SecuritySubscriptionId,

        [Parameter(Mandatory)]
        [string]$LawResourceGroup,

        [Parameter(Mandatory)]
        [string]$LawWorkspaceName
    )

    $assignee = Get-CurrentAzureRoleCheckAssignee
    if (-not $assignee) {
        Write-Host 'Skipping deployment authorization preflight because the current Azure principal could not be resolved.'
        return
    }

    $workloadSubscriptionScope = "/subscriptions/$WorkloadSubscriptionId"
    $securitySubscriptionScope = "/subscriptions/$SecuritySubscriptionId"
    $lawWorkspaceScope = "/subscriptions/$SecuritySubscriptionId/resourceGroups/$LawResourceGroup/providers/Microsoft.OperationalInsights/workspaces/$LawWorkspaceName"

    if (-not (Test-AzureRoleAssignmentWriteAccess -Assignee $assignee -Scopes @($workloadSubscriptionScope))) {
        throw (@(
            "Azure principal '$assignee' does not have permission to create RBAC role assignments in the workload subscription."
            "This deployment creates Microsoft.Authorization/roleAssignments resources for the zolab-ai-dev group and the Foundry managed identity."
            "Contributor is not sufficient because it excludes Microsoft.Authorization/*/Write."
            "Grant 'User Access Administrator' or 'Owner' at $workloadSubscriptionScope and retry."
        ) -join ' ')
    }

    if (-not (Test-AzureRoleAssignmentWriteAccess -Assignee $assignee -Scopes @($lawWorkspaceScope, $securitySubscriptionScope))) {
        throw (@(
            "Azure principal '$assignee' does not have permission to manage RBAC on the $LawWorkspaceName Log Analytics workspace in the Security subscription."
            "The deployment later assigns 'Log Analytics Reader' to the zolab-ai-dev group on $lawWorkspaceScope."
            "Grant 'User Access Administrator' or 'Owner' on the workspace scope or Security subscription and retry."
        ) -join ' ')
    }
}

function Get-GraphUserObjectId {
    param(
        [string]$Account
    )

    if (-not $Account) {
        return $null
    }

    $graphUser = Get-MgUser -UserId $Account -ErrorAction SilentlyContinue
    if (-not $graphUser) {
        return $null
    }

    $graphUser.Id
}

function Test-GraphGroupMembership {
    param(
        [string]$GroupId,
        [string]$DirectoryObjectId
    )

    if (-not $GroupId -or -not $DirectoryObjectId) {
        return $false
    }

    $memberIds = Get-MgGroupMember -GroupId $GroupId -All -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty Id

    $memberIds -contains $DirectoryObjectId
}

function Start-TeamsProgressNotifier {
    param(
        [string]$ChatId,
        [string]$Message,
        [string]$TeamsChatHelperPath,
        [int]$IntervalSeconds = 60
    )

    if (-not $ChatId -or -not $Message) {
        return $null
    }

    if (-not (Test-Path -LiteralPath $TeamsChatHelperPath)) {
        throw "Teams chat helper script was not found at '$TeamsChatHelperPath'."
    }

    $notifierId = [guid]::NewGuid().ToString('N')
    $tempRoot = [System.IO.Path]::GetTempPath()
    $stopFilePath = Join-Path $tempRoot "foundry-progress-$notifierId.stop"
    $scriptPath = Join-Path $tempRoot "foundry-progress-$notifierId.ps1"
    $stdoutPath = Join-Path $tempRoot "foundry-progress-$notifierId.log"
    $stderrPath = Join-Path $tempRoot "foundry-progress-$notifierId.err.log"
    $utf8Bom = [System.Text.UTF8Encoding]::new($true)
    $escapedTeamsChatHelperPath = $TeamsChatHelperPath.Replace("'", "''")
    $escapedChatId = $ChatId.Replace("'", "''")
    $escapedMessage = $Message.Replace("'", "''")
    $escapedStopFilePath = $stopFilePath.Replace("'", "''")

    $scriptContent = @"
`$ErrorActionPreference = 'Stop'

`$TeamsChatHelperPath = '$escapedTeamsChatHelperPath'
`$ChatId = '$escapedChatId'
`$Message = '$escapedMessage'
`$IntervalSeconds = $IntervalSeconds
`$StopFilePath = '$escapedStopFilePath'

. `$TeamsChatHelperPath

Import-Module Microsoft.Graph.Authentication
Import-Module Microsoft.Graph.Teams

`$requiredGraphScopes = @(
    'User.Read'
    'Chat.Create'
    'Chat.ReadWrite'
    'ChatMessage.Send'
)

    [void](Connect-FoundryGraphIfNeeded -Scopes `$requiredGraphScopes)

    while (-not (Test-Path -LiteralPath `$StopFilePath)) {
        Start-Sleep -Seconds `$IntervalSeconds
    if (Test-Path -LiteralPath `$StopFilePath) {
        break
    }

    [void](Send-FoundryTeamsChatMessage -ChatId `$ChatId -Message `$Message)
}
"@

    [System.IO.File]::WriteAllText($scriptPath, $scriptContent, $utf8Bom)

    $pwshPath = Get-FoundryPowerShellPath
    $pwshArguments = @(
        '-NoLogo',
        '-NoProfile'
    )
    if ($IsWindows) {
        $pwshArguments += @('-ExecutionPolicy', 'Bypass')
    }
    $pwshArguments += @('-File', $scriptPath)

    $startProcessParams = @{
        FilePath               = $pwshPath
        ArgumentList           = $pwshArguments
        RedirectStandardOutput = $stdoutPath
        RedirectStandardError  = $stderrPath
        PassThru               = $true
    }
    if ($IsWindows) {
        $startProcessParams.WindowStyle = 'Hidden'
    }

    $process = Start-Process @startProcessParams

    [pscustomobject]@{
        Process      = $process
        StopFilePath = $stopFilePath
        ScriptPath   = $scriptPath
        StdOutPath   = $stdoutPath
        StdErrPath   = $stderrPath
    }
}

function Stop-TeamsProgressNotifier {
    param(
        $Notifier
    )

    if (-not $Notifier) {
        return
    }

    try {
        if ($Notifier.StopFilePath) {
            Set-Content -LiteralPath $Notifier.StopFilePath -Value 'stop' -Encoding ascii
        }

        $process = $Notifier.Process
        if ($process) {
            $process.Refresh()
            if (-not $process.HasExited) {
                if (-not $process.WaitForExit(5000)) {
                    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
                }
            }
        }
    } finally {
        foreach ($path in @(
            $Notifier.StopFilePath,
            $Notifier.ScriptPath,
            $Notifier.StdOutPath,
            $Notifier.StdErrPath
        )) {
            if ($path -and (Test-Path -LiteralPath $path)) {
                Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Write-BuildStatus {
    param(
        [Parameter(Mandatory)]
        [string]$ResourceGroupName,

        [Parameter(Mandatory)]
        [string]$StorageAccountName,

        [Parameter(Mandatory)]
        [string]$KeyVaultName,

        [Parameter(Mandatory)]
        [string]$AppInsightsName,

        [Parameter(Mandatory)]
        [string]$AiFoundryName,

        [Parameter(Mandatory)]
        [string]$AiProjectName,

        [Parameter(Mandatory)]
        [string]$GenAiModelDisplay,

        [Parameter(Mandatory)]
        [string]$BuildInfoStatus,

        [Parameter(Mandatory)]
        [string]$FoundryProjectEndpoint,

        [Parameter(Mandatory)]
        [string]$AzureOpenAIEndpoint,

        [Parameter(Mandatory)]
        [string]$AppInsightsConnectionStatus,

        [Parameter(Mandatory)]
        [string]$AppInsightsAccessStatus,

        [Parameter(Mandatory)]
        [string]$LawRbacStatus,

        [Parameter(Mandatory)]
        [string]$UserStatus
    )

    $rows = @(
        @{ Item = '☁️ Resource Group'; Status = "✅ $ResourceGroupName" },
        @{ Item = '🗄️ Storage'; Status = $StorageAccountName },
        @{ Item = '🔐 Key Vault'; Status = $KeyVaultName },
        @{ Item = '📊 App Insights'; Status = $AppInsightsName },
        @{ Item = '🤖 AI Foundry'; Status = $AiFoundryName },
        @{ Item = '🏢 AI Project'; Status = $AiProjectName },
        @{ Item = '🧠 Model'; Status = $GenAiModelDisplay },
        @{ Item = '📝 Build Info'; Status = $BuildInfoStatus },
        @{ Item = '🔗 App Insights Connection'; Status = $AppInsightsConnectionStatus },
        @{ Item = '👁️ App Insights Access'; Status = $AppInsightsAccessStatus },
        @{ Item = '📡 LAW RBAC'; Status = $LawRbacStatus },
        @{ Item = '👤 User'; Status = $UserStatus },
        @{ Item = '🔌 Foundry Project Endpoint'; Status = $FoundryProjectEndpoint },
        @{ Item = '🤖 Azure OpenAI Endpoint'; Status = $AzureOpenAIEndpoint }
    )

    $itemWidth = [Math]::Max(29, (($rows | ForEach-Object { $_.Item.Length } | Measure-Object -Maximum).Maximum + 2))
    $statusWidth = [Math]::Max(78, (($rows | ForEach-Object { $_.Status.Length } | Measure-Object -Maximum).Maximum + 2))

    $lines = @(
        ''
        '● ☁️🎉🚀 Fresh build — all green!'
        ''
        ("┌" + ("─" * $itemWidth) + "┬" + ("─" * $statusWidth) + "┐")
        ("│" + " Item".PadRight($itemWidth) + "│" + " Status".PadRight($statusWidth) + "│")
        ("├" + ("─" * $itemWidth) + "┼" + ("─" * $statusWidth) + "┤")
    )

    foreach ($row in $rows) {
        $lines += ("│" + (" " + $row.Item).PadRight($itemWidth) + "│" + (" " + $row.Status).PadRight($statusWidth) + "│")
    }

    $lines += ("└" + ("─" * $itemWidth) + "┴" + ("─" * $statusWidth) + "┘")
    $lines += ''
    $lines += 'Ready for the notebook! 🎯'

    $lines
}

function Write-TeardownStatus {
    param(
        [Parameter(Mandatory)]
        [string]$ScopeStatus,

        [Parameter(Mandatory)]
        [string]$ResourceGroupStatus,

        [Parameter(Mandatory)]
        [string]$RbacStatus,

        [Parameter(Mandatory)]
        [string]$CognitiveServicesStatus,

        [Parameter(Mandatory)]
        [string]$DeploymentRecordStatus,

        [Parameter(Mandatory)]
        [string]$BuildInfoStatus,

        [string]$SecurityStatus = 'Not applicable',

        [string]$UserStatus = 'Not applicable',

        [switch]$PreviewMode
    )

    $rows = @(
        @{ Item = '🎯 Scope'; Status = $ScopeStatus },
        @{ Item = '☁️ Resource Group'; Status = $ResourceGroupStatus },
        @{ Item = '🔐 RBAC'; Status = $RbacStatus },
        @{ Item = '🧼 Cognitive Services'; Status = $CognitiveServicesStatus },
        @{ Item = '📋 Deployment Record'; Status = $DeploymentRecordStatus },
        @{ Item = '📝 Build Info'; Status = $BuildInfoStatus },
        @{ Item = '📡 Security Cleanup'; Status = $SecurityStatus },
        @{ Item = '👤 User'; Status = $UserStatus }
    )

    $itemWidth = [Math]::Max(29, (($rows | ForEach-Object { $_.Item.Length } | Measure-Object -Maximum).Maximum + 2))
    $statusWidth = [Math]::Max(78, (($rows | ForEach-Object { $_.Status.Length } | Measure-Object -Maximum).Maximum + 2))

    $lines = @(
        ''
        $(if ($PreviewMode) { '● 🔎 Teardown preview complete!' } else { '● 🧹🗑️ Teardown complete!' })
        ''
        ("┌" + ("─" * $itemWidth) + "┬" + ("─" * $statusWidth) + "┐")
        ("│" + " Item".PadRight($itemWidth) + "│" + " Status".PadRight($statusWidth) + "│")
        ("├" + ("─" * $itemWidth) + "┼" + ("─" * $statusWidth) + "┤")
    )

    foreach ($row in $rows) {
        $lines += ("│" + (" " + $row.Item).PadRight($itemWidth) + "│" + (" " + $row.Status).PadRight($statusWidth) + "│")
    }

    $lines += ("└" + ("─" * $itemWidth) + "┴" + ("─" * $statusWidth) + "┘")
    $lines += ''
    $lines += $(if ($PreviewMode) { 'No changes were made. ℹ️' } else { 'Environment cleanup finished. ✅' })

    $lines
}

function Write-BuildInfoJson {
    param(
        [Parameter(Mandatory)]
        [string]$OutputPath,

        [Parameter(Mandatory)]
        [string]$ResourceGroupName,

        [Parameter(Mandatory)]
        [string]$AppInsightsName,

        [Parameter(Mandatory)]
        [string]$FoundryProjectEndpoint,

        [Parameter(Mandatory)]
        [string]$AzureOpenAIEndpoint,

        [Parameter(Mandatory)]
        [string]$StorageAccountName,

        [Parameter(Mandatory)]
        [string]$KeyVaultName,

        [Parameter(Mandatory)]
        [string]$GenAiModel,

        [Parameter(Mandatory)]
        [string]$AiFoundryName,

        [Parameter(Mandatory)]
        [string]$AiProjectName,

        [Parameter(Mandatory)]
        [string]$RequestedBy,

        [string]$RequestedByObjectId
    )

    $buildInfo = [ordered]@{
        rg                      = $ResourceGroupName
        appinsights             = $AppInsightsName
        foundry_project_endpoint = $FoundryProjectEndpoint
        azure_openai_endpoint   = $AzureOpenAIEndpoint
        storage_account         = $StorageAccountName
        key_vault               = $KeyVaultName
        genai_model             = $GenAiModel
        foundry_name            = $AiFoundryName
        foundry_project_name    = $AiProjectName
        requested_by            = $RequestedBy
    }

    if (-not [string]::IsNullOrWhiteSpace($RequestedByObjectId)) {
        $buildInfo.requested_by_object_id = $RequestedByObjectId
    }

    $buildInfo | ConvertTo-Json | Set-Content -Path $OutputPath -Encoding utf8
}

function Get-BuildInfoDirectory {
    Split-Path $PSScriptRoot -Parent
}

function Get-LegacyBuildInfoPath {
    Join-Path (Get-BuildInfoDirectory) 'build_info.json'
}

function Get-AzCliExecutablePath {
    if ($script:AzCliExecutablePath) {
        return $script:AzCliExecutablePath
    }

    $candidate = Get-Command az.cmd -ErrorAction SilentlyContinue
    if (-not $candidate) {
        $candidate = Get-Command az -ErrorAction SilentlyContinue
    }

    if (-not $candidate) {
        throw 'Azure CLI executable was not found on PATH.'
    }

    $script:AzCliExecutablePath = $candidate.Source
    $script:AzCliExecutablePath
}

function Invoke-AzCliCommand {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [int]$TimeoutSeconds = 120
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = Get-AzCliExecutablePath
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true

    foreach ($argument in $Arguments) {
        [void]$startInfo.ArgumentList.Add([string]$argument)
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo

    if (-not $process.Start()) {
        throw 'Failed to start Azure CLI process.'
    }

    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try {
            $process.Kill()
        } catch {
        }

        $commandText = "az $($Arguments -join ' ')"
        throw "Azure CLI command timed out after $TimeoutSeconds seconds: $commandText"
    }

    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $null = $process.WaitForExit()

    $combinedOutput = @($stdout, $stderr) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

    [pscustomobject]@{
        ExitCode   = $process.ExitCode
        StdOut     = $stdout
        StdErr     = $stderr
        OutputText = ($combinedOutput -join [Environment]::NewLine).Trim()
    }
}

function Invoke-PostDeployStep {
    param(
        [Parameter(Mandatory)]
        [string]$StepName,

        [Parameter(Mandatory)]
        [scriptblock]$Action
    )

    $startedAt = Get-Date
    Write-Host "[post-deploy] START $StepName"

    try {
        $result = & $Action
        $elapsedSeconds = [Math]::Round(((Get-Date) - $startedAt).TotalSeconds, 1)
        Write-Host "[post-deploy] OK $StepName (${elapsedSeconds}s)"
        return $result
    } catch {
        $elapsedSeconds = [Math]::Round(((Get-Date) - $startedAt).TotalSeconds, 1)
        $message = $_.Exception.Message
        Write-Warning "[post-deploy] FAIL $StepName after ${elapsedSeconds}s: $message"
        throw "Post-deploy step '$StepName' failed after ${elapsedSeconds}s: $message"
    }
}

function Get-BuildInfoPathForSuffix {
    param(
        [Parameter(Mandatory)]
        [string]$Suffix
    )

    Join-Path (Get-BuildInfoDirectory) "build_info-$Suffix.json"
}

function Sync-BuildInfoFromBlobIfAvailable {
    param(
        [Parameter(Mandatory)]
        [string]$Suffix
    )

    $storageAccountName = $env:AZURE_STORAGE_ACCOUNT
    $blobContainerName = $env:AZURE_BLOB_CONTAINER
    if ([string]::IsNullOrWhiteSpace($storageAccountName) -or [string]::IsNullOrWhiteSpace($blobContainerName)) {
        return $null
    }

    $targetPath = Get-BuildInfoPathForSuffix -Suffix $Suffix
    $blobName = "builds/build_info-$Suffix.json"

    $null = & az storage blob download `
        --auth-mode login `
        --account-name $storageAccountName `
        --container-name $blobContainerName `
        --name $blobName `
        --file $targetPath `
        --overwrite true `
        --only-show-errors 2>&1

    if ($LASTEXITCODE -ne 0) {
        if (Test-Path -LiteralPath $targetPath) {
            Remove-Item -LiteralPath $targetPath -Force -ErrorAction SilentlyContinue
        }
        return $null
    }

    if (Test-Path -LiteralPath $targetPath) {
        return $targetPath
    }

    $null
}

function Sync-BuildInfoToBlobIfAvailable {
    param(
        [Parameter(Mandatory)]
        [string]$BuildInfoPath,

        [int]$TimeoutSeconds = 120
    )

    $storageAccountName = $env:AZURE_STORAGE_ACCOUNT
    $blobContainerName = $env:AZURE_BLOB_CONTAINER
    if ([string]::IsNullOrWhiteSpace($storageAccountName) -or [string]::IsNullOrWhiteSpace($blobContainerName)) {
        return $null
    }

    if (-not (Test-Path -LiteralPath $BuildInfoPath)) {
        return $null
    }

    $blobName = "builds/$(Split-Path -Leaf $BuildInfoPath)"

    $uploadResult = Invoke-AzCliCommand -Arguments @(
        'storage', 'blob', 'upload',
        '--auth-mode', 'login',
        '--account-name', $storageAccountName,
        '--container-name', $blobContainerName,
        '--name', $blobName,
        '--file', $BuildInfoPath,
        '--overwrite', 'true',
        '--only-show-errors'
    ) -TimeoutSeconds $TimeoutSeconds

    if ($uploadResult.ExitCode -ne 0) {
        $message = $uploadResult.OutputText
        if ([string]::IsNullOrWhiteSpace($message)) {
            $message = 'upload failed'
        }
        throw "Failed to upload $(Split-Path -Leaf $BuildInfoPath) to blob storage: $message"
    }

    [pscustomobject]@{
        AccountName   = $storageAccountName
        ContainerName = $blobContainerName
        BlobName      = $blobName
    }
}

function Remove-BuildInfoBlobIfAvailable {
    param(
        [Parameter(Mandatory)]
        [string]$Suffix
    )

    $storageAccountName = $env:AZURE_STORAGE_ACCOUNT
    $blobContainerName = $env:AZURE_BLOB_CONTAINER
    if ([string]::IsNullOrWhiteSpace($storageAccountName) -or [string]::IsNullOrWhiteSpace($blobContainerName)) {
        return [pscustomobject]@{
            Removed = $false
            Found   = $false
            Status  = 'Blob cleanup skipped because storage environment is not configured ℹ️'
        }
    }

    $blobName = "builds/build_info-$Suffix.json"
    $existsOutput = & az storage blob exists `
        --auth-mode login `
        --account-name $storageAccountName `
        --container-name $blobContainerName `
        --name $blobName `
        --query exists `
        --output tsv `
        --only-show-errors 2>&1

    if ($LASTEXITCODE -ne 0) {
        $message = (($existsOutput -join ' ').Trim())
        if ([string]::IsNullOrWhiteSpace($message)) {
            $message = 'existence check failed'
        }
        throw "Failed to check blob cleanup state for '$blobName': $message"
    }

    $blobExists = [System.StringComparer]::OrdinalIgnoreCase.Equals((($existsOutput -join '').Trim()), 'true')
    if (-not $blobExists) {
        return [pscustomobject]@{
            Removed = $false
            Found   = $false
            Status  = "No blob build record named $blobName found ℹ️"
        }
    }

    $deleteOutput = & az storage blob delete `
        --auth-mode login `
        --account-name $storageAccountName `
        --container-name $blobContainerName `
        --name $blobName `
        --only-show-errors 2>&1

    if ($LASTEXITCODE -ne 0) {
        $message = (($deleteOutput -join ' ').Trim())
        if ([string]::IsNullOrWhiteSpace($message)) {
            $message = 'delete failed'
        }
        throw "Failed to delete blob build record '$blobName': $message"
    }

    [pscustomobject]@{
        Removed = $true
        Found   = $true
        Status  = "Removed blob build record $blobName ✅"
    }
}

function Get-BuildInfoPaths {
    $buildInfoPaths = @(
        Get-ChildItem -Path (Get-BuildInfoDirectory) -Filter 'build_info-*.json' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -ExpandProperty FullName
    )

    $legacyPath = Get-LegacyBuildInfoPath
    if (Test-Path -LiteralPath $legacyPath) {
        $buildInfoPaths += $legacyPath
    }

    $buildInfoPaths
}

function Get-LatestBuildInfoPath {
    $buildInfoPaths = Get-BuildInfoPaths
    if ($buildInfoPaths) {
        return $buildInfoPaths[0]
    }

    $null
}

function Import-BuildInfoFile {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Get-FoundryBuildMetadataStatus {
    param(
        [Parameter(Mandatory)]
        [string]$ResourceGroupName,

        [int]$OrphanedThresholdMinutes = 30
    )

    $buildInfoRecord = Get-BuildInfoRecordForResourceGroup -ResourceGroupName $ResourceGroupName
    $deploymentName = $null
    $deploymentTimestampUtc = $null
    $deploymentAgeMinutes = $null
    $orphanedCandidate = $false

    try {
        $suffix = Get-FoundryDeploymentSuffixFromResourceGroupName -ResourceGroupName $ResourceGroupName
        $deploymentName = "foundry-ai-env-$suffix"
        $deploymentRecord = Get-AzSubscriptionDeployment -Name $deploymentName -ErrorAction SilentlyContinue
        if ($deploymentRecord -and $deploymentRecord.Timestamp) {
            $deploymentTimestampUtc = ([datetimeoffset]$deploymentRecord.Timestamp).UtcDateTime
            $deploymentAgeMinutes = [Math]::Round(((Get-Date).ToUniversalTime() - $deploymentTimestampUtc).TotalMinutes, 1)
        }
    } catch {
    }

    if ($buildInfoRecord) {
        $statusLine = "build info: $(Split-Path -Leaf $buildInfoRecord.Path) ✅"
    } elseif ($null -ne $deploymentAgeMinutes) {
        if ($deploymentAgeMinutes -ge $OrphanedThresholdMinutes) {
            $orphanedCandidate = $true
            $statusLine = "build info missing for $deploymentAgeMinutes min (threshold: $OrphanedThresholdMinutes) — orphaned candidate ⚠️"
        } else {
            $statusLine = "build info file missing (${deploymentAgeMinutes} min old) ❌"
        }
    } else {
        $orphanedCandidate = $true
        $statusLine = 'build info file missing; deployment record unavailable — orphaned candidate ⚠️'
    }

    [pscustomobject]@{
        BuildInfoRecord       = $buildInfoRecord
        DeploymentName        = $deploymentName
        DeploymentTimestampUtc = $deploymentTimestampUtc
        DeploymentAgeMinutes  = $deploymentAgeMinutes
        OrphanedCandidate     = $orphanedCandidate
        StatusLine            = $statusLine
    }
}

function Get-FoundryManagedResourceGroups {
    Get-AzResourceGroup | Where-Object { $_.ResourceGroupName -match '^zolab-ai-.{4,}$' } | Sort-Object ResourceGroupName
}

function Get-FoundryBuildInventory {
    param(
        [string]$ExcludeResourceGroupName,

        [int]$OrphanedThresholdMinutes = 30
    )

    $inventory = foreach ($resourceGroup in Get-FoundryManagedResourceGroups) {
        if ($ExcludeResourceGroupName -and $resourceGroup.ResourceGroupName -ieq $ExcludeResourceGroupName) {
            continue
        }

        $buildMetadataStatus = Get-FoundryBuildMetadataStatus `
            -ResourceGroupName $resourceGroup.ResourceGroupName `
            -OrphanedThresholdMinutes $OrphanedThresholdMinutes
        $buildInfoRecord = $buildMetadataStatus.BuildInfoRecord
        $requestedBy = $null
        if ($buildInfoRecord -and $buildInfoRecord.Data.PSObject.Properties.Name -contains 'requested_by') {
            $requestedBy = [string]$buildInfoRecord.Data.requested_by
            if ([string]::IsNullOrWhiteSpace($requestedBy)) {
                $requestedBy = $null
            }
        }

        [pscustomobject]@{
            ResourceGroupName = $resourceGroup.ResourceGroupName
            BuildInfoPath     = if ($buildInfoRecord) { $buildInfoRecord.Path } else { $null }
            BuildInfoStatus   = $buildMetadataStatus.StatusLine
            ModelName         = if ($buildInfoRecord) { [string]$buildInfoRecord.Data.genai_model } else { $null }
            DeploymentName    = $buildMetadataStatus.DeploymentName
            DeploymentAgeMinutes = $buildMetadataStatus.DeploymentAgeMinutes
            OrphanedCandidate = $buildMetadataStatus.OrphanedCandidate
            RequestedBy       = $requestedBy
            OwnershipKnown    = -not [string]::IsNullOrWhiteSpace($requestedBy)
        }
    }

    @($inventory)
}

function Remove-FoundryManagedResourceGroupAccess {
    param(
        [Parameter(Mandatory)]
        [string]$GroupObjectId,

        [Parameter(Mandatory)]
        [string]$ResourceGroupScope,

        [Parameter(Mandatory)]
        [string]$GroupDisplayName,

        [switch]$PreviewOnly
    )

    $scopedAssignments = @(
        Get-AzRoleAssignment -ObjectId $GroupObjectId -Scope $ResourceGroupScope -ErrorAction SilentlyContinue |
            Where-Object { $_.Scope -ieq $ResourceGroupScope }
    )
    $assignmentPlan = Get-FoundryManagedResourceGroupAssignmentPlan `
        -Assignments $scopedAssignments `
        -ResourceGroupScope $ResourceGroupScope
    $managedAssignments = @($assignmentPlan.ManagedAssignments)
    $preservedAssignments = @($assignmentPlan.PreservedAssignments)

    foreach ($assignment in $managedAssignments) {
        Write-Host "  Managed role $($(if ($PreviewOnly) { 'planned' } else { 'matched' })): $($assignment.RoleDefinitionName) @ $($assignment.Scope)"
    }
    foreach ($assignment in $preservedAssignments) {
        Write-Host "  Preserving non-managed role: $($assignment.RoleDefinitionName) @ $($assignment.Scope)"
    }

    if ($PreviewOnly) {
        $status = if ($managedAssignments) {
            "Would remove $($managedAssignments.Count) managed scoped assignments from $GroupDisplayName ℹ️"
        } else {
            "No managed scoped assignments found for $GroupDisplayName ℹ️"
        }

        if ($preservedAssignments) {
            $status += "; would preserve $($preservedAssignments.Count) non-managed scoped assignment(s) ℹ️"
        }

        return [pscustomobject]@{
            ManagedAssignments   = $managedAssignments
            PreservedAssignments = $preservedAssignments
            Status               = $status
        }
    }

    foreach ($assignment in $managedAssignments) {
        Write-Host "  Removing managed role: $($assignment.RoleDefinitionName) @ $($assignment.Scope)"
        Remove-AzRoleAssignment -ObjectId $GroupObjectId `
            -RoleDefinitionName $assignment.RoleDefinitionName `
            -Scope $assignment.Scope `
            -ErrorAction SilentlyContinue
    }

    $status = if ($managedAssignments) {
        "Removed $($managedAssignments.Count) managed scoped assignments from $GroupDisplayName ✅"
    } else {
        "No managed scoped assignments found for $GroupDisplayName ℹ️"
    }

    if ($preservedAssignments) {
        $status += "; preserved $($preservedAssignments.Count) non-managed scoped assignment(s) ℹ️"
    }

    [pscustomobject]@{
        ManagedAssignments   = $managedAssignments
        PreservedAssignments = $preservedAssignments
        Status               = $status
    }
}

function Get-FoundryResourceGroupCleanupPreview {
    param(
        [Parameter(Mandatory)]
        [string]$ResourceGroupName
    )

    $cogAccounts = @(
        Get-AzResource -ResourceGroupName $ResourceGroupName `
            -ResourceType 'Microsoft.CognitiveServices/accounts' -ErrorAction SilentlyContinue
    )

    [pscustomobject]@{
        ResourceGroupStatus = "Would delete $ResourceGroupName ℹ️"
        CognitiveServicesStatus = if ($cogAccounts) {
            "Would purge $($cogAccounts.Name -join ', ') after resource-group deletion ℹ️"
        } else {
            'No Cognitive Services purge work would be required ℹ️'
        }
    }
}

function Get-BuildInfoRemovalPreview {
    param(
        [Parameter(Mandatory)]
        [string]$ResourceGroupName
    )

    $buildInfoPath = Get-BuildInfoPathForResourceGroup -ResourceGroupName $ResourceGroupName
    if (-not $buildInfoPath) {
        return "No build_info file found for $ResourceGroupName ℹ️"
    }

    "Would remove $(Split-Path -Leaf $buildInfoPath) for $ResourceGroupName ℹ️"
}

function Get-FoundryBuildListLines {
    param(
        [Parameter(Mandatory)]
        [string]$SubscriptionId,

        [int]$OrphanedThresholdMinutes = 30
    )

    Set-AzContext -SubscriptionId $SubscriptionId | Out-Null

    $inventory = @(Get-FoundryBuildInventory -OrphanedThresholdMinutes $OrphanedThresholdMinutes)
    $buildInfoPaths = @(Get-BuildInfoPaths)
    $matchedBuildInfoPaths = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)

    $lines = @(
        ''
        '● 📚 Foundry builds'
        ''
    )

    if ($inventory.Count -eq 0) {
        $lines += 'No active managed resource groups found ℹ️'
    } else {
        $lines += 'Active resource groups:'
        for ($i = 0; $i -lt $inventory.Count; $i++) {
            $build = $inventory[$i]
            if ($build.BuildInfoPath) {
                [void]$matchedBuildInfoPaths.Add($build.BuildInfoPath)
            }

            if (-not [string]::IsNullOrWhiteSpace($build.ModelName)) {
                $lines += "$($i + 1). $($build.ResourceGroupName) — model: $($build.ModelName) — $($build.BuildInfoStatus)"
            } else {
                $lines += "$($i + 1). $($build.ResourceGroupName) — $($build.BuildInfoStatus)"
            }
        }
    }

    $orphanedBuilds = @($inventory | Where-Object { $_.OrphanedCandidate })
    if ($orphanedBuilds.Count -gt 0) {
        $lines += ''
        $lines += "Orphaned build candidates (threshold: ${OrphanedThresholdMinutes} min):"
        foreach ($build in $orphanedBuilds) {
            $ageText = if ($null -ne $build.DeploymentAgeMinutes) {
                "$($build.DeploymentAgeMinutes) min old"
            } else {
                'deployment age unavailable'
            }
            $lines += "- $($build.ResourceGroupName) — $ageText — consider targeted cleanup ⚠️"
        }
    }

    $orphanedBuildInfoPaths = @($buildInfoPaths | Where-Object { -not $matchedBuildInfoPaths.Contains($_) })
    if ($orphanedBuildInfoPaths.Count -gt 0) {
        $lines += ''
        $lines += 'Orphaned build info files:'
        foreach ($path in $orphanedBuildInfoPaths) {
            try {
                $buildInfo = Import-BuildInfoFile -Path $path
                $lines += "- $(Split-Path -Leaf $path) — resource group: $($buildInfo.rg) ⚠️"
            } catch {
                $lines += "- $(Split-Path -Leaf $path) — unreadable ⚠️"
            }
        }
    }

    $lines
}

function Get-AllowedAiModelChoices {
    @(
        "gpt-4.1-mini"
        "gpt-5.3"
        "gpt-5.4"
        "grok-4-1-fast-reasoning"
    )
}

function Select-AiModelSelection {
    param(
        [Parameter(Mandatory)]
        [string[]]$Choices
    )

    if (-not $Choices) {
        throw "No AI model choices are available."
    }

    $options = for ($i = 0; $i -lt $Choices.Count; $i++) {
        [System.Management.Automation.Host.ChoiceDescription]::new(
            "&$($i + 1) $($Choices[$i])",
            "Deploy $($Choices[$i])"
        )
    }

    $selectionIndex = $Host.UI.PromptForChoice(
        "Select AI model",
        "Choose one of the allowed AI models for deployment.",
        $options,
        0
    )

    $Choices[$selectionIndex]
}

function Get-LocationModelCatalog {
    param(
        [Parameter(Mandatory)]
        [string]$Location,

        [Parameter(Mandatory)]
        [string]$SubscriptionId
    )

    if (
        $script:locationModelCatalog -and
        $script:locationModelCatalogLocation -eq $Location -and
        $script:locationModelCatalogSubscriptionId -eq $SubscriptionId
    ) {
        return $script:locationModelCatalog
    }

    $catalogJson = az cognitiveservices model list `
        --location $Location `
        --subscription $SubscriptionId `
        --only-show-errors `
        --output json 2>&1

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to list available AI models in $($Location):`n$($catalogJson -join "`n")"
    }

    $script:locationModelCatalog = $catalogJson | ConvertFrom-Json
    $script:locationModelCatalogLocation = $Location
    $script:locationModelCatalogSubscriptionId = $SubscriptionId

    $script:locationModelCatalog
}

function Get-AiModelMatchPattern {
    param(
        [Parameter(Mandatory)]
        [string]$ModelChoice
    )

    switch ($ModelChoice) {
        "gpt-4.1-mini" { '^gpt-4\.1-mini$' }
        "gpt-5.3" { '^gpt-5\.3($|-)' }
        "gpt-5.4" { '^gpt-5\.4($|-)' }
        "grok-4-1-fast-reasoning" { '^grok-4-1-fast-reasoning$' }
        default { throw "Unsupported AI model choice '$ModelChoice'." }
    }
}

function Get-AiModelFormat {
    param(
        [Parameter(Mandatory)]
        [string]$ModelChoice
    )

    switch ($ModelChoice) {
        "grok-4-1-fast-reasoning" { 'xAI' }
        default { 'OpenAI' }
    }
}

function Get-AiModelCandidate {
    param(
        [Parameter(Mandatory)]
        [string]$ModelChoice,

        [Parameter(Mandatory)]
        [string]$Location,

        [Parameter(Mandatory)]
        [string]$SubscriptionId
    )

    $matchPattern = Get-AiModelMatchPattern -ModelChoice $ModelChoice
    $modelFormat = Get-AiModelFormat -ModelChoice $ModelChoice
    $catalog = Get-LocationModelCatalog -Location $Location -SubscriptionId $SubscriptionId

    $catalog |
        Where-Object {
            $_.kind -eq 'AIServices' -and
            $_.model.format -eq $modelFormat -and
            $_.lifecycleStatus -ne 'Deprecated' -and
            $_.model.name -match $matchPattern
        } |
        Sort-Object `
            @{ Expression = { if ($_.model.name -eq $ModelChoice) { 0 } else { 1 } } }, `
            @{ Expression = { $_.model.version }; Descending = $true } |
        Select-Object -First 1
}

function Get-AiModelCapacityOptions {
    param(
        [Parameter(Mandatory)]
        [string]$Location,

        [Parameter(Mandatory)]
        [string]$SubscriptionId,

        [Parameter(Mandatory)]
        [string]$ModelFormat,

        [Parameter(Mandatory)]
        [string]$ModelName,

        [Parameter(Mandatory)]
        [string]$ModelVersion
    )

    $armToken = az account get-access-token `
        --resource https://management.azure.com `
        --query accessToken `
        --output tsv `
        --only-show-errors 2>&1

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to acquire Azure Resource Manager token:`n$($armToken -join "`n")"
    }

    $headers = @{
        Authorization = "Bearer $($armToken -join '')"
    }

    $url = "https://management.azure.com/subscriptions/$SubscriptionId/providers/Microsoft.CognitiveServices/locations/$Location/modelCapacities?api-version=2024-10-01&modelFormat=$([uri]::EscapeDataString($ModelFormat))&modelName=$([uri]::EscapeDataString($ModelName))&modelVersion=$([uri]::EscapeDataString($ModelVersion))"
    $response = Invoke-RestMethod -Method Get -Uri $url -Headers $headers

    @($response.value)
}

function Select-AiModelSku {
    param(
        [Parameter(Mandatory)]
        [object[]]$CapacityOptions,

        [Parameter(Mandatory)]
        [int]$DefaultCapacity
    )

    $availableOptions = @(
        $CapacityOptions |
            Where-Object { $_.properties.availableCapacity -gt 0 }
    )

    if (-not $availableOptions) {
        return $null
    }

    $preferredSkuNames = @("GlobalStandard", "Standard")
    $chosenOption = $null

    foreach ($preferredSkuName in $preferredSkuNames) {
        $chosenOption = $availableOptions |
            Where-Object { $_.properties.skuName -eq $preferredSkuName } |
            Sort-Object @{ Expression = { $_.properties.availableCapacity }; Descending = $true } |
            Select-Object -First 1

        if ($chosenOption) {
            break
        }
    }

    if (-not $chosenOption) {
        $chosenOption = $availableOptions |
            Sort-Object @{ Expression = { $_.properties.availableCapacity }; Descending = $true } |
            Select-Object -First 1
    }

    $deployCapacity = [Math]::Max(
        1,
        [Math]::Min($DefaultCapacity, [int][Math]::Floor($chosenOption.properties.availableCapacity))
    )

    [pscustomobject]@{
        SkuName   = $chosenOption.properties.skuName
        Capacity  = $deployCapacity
        Available = [int][Math]::Floor($chosenOption.properties.availableCapacity)
    }
}

function Resolve-AiModelSpecification {
    param(
        [Parameter(Mandatory)]
        [string]$ModelChoice,

        [Parameter(Mandatory)]
        [string]$Location,

        [Parameter(Mandatory)]
        [string]$SubscriptionId,

        [Parameter(Mandatory)]
        [int]$DefaultCapacity
    )

    $candidate = Get-AiModelCandidate `
        -ModelChoice $ModelChoice `
        -Location $Location `
        -SubscriptionId $SubscriptionId

    if (-not $candidate) {
        return $null
    }

    $modelFormat = Get-AiModelFormat -ModelChoice $ModelChoice

    $capacitySelection = Select-AiModelSku `
        -CapacityOptions (Get-AiModelCapacityOptions `
            -Location $Location `
            -SubscriptionId $SubscriptionId `
            -ModelFormat $modelFormat `
            -ModelName $candidate.model.name `
            -ModelVersion $candidate.model.version) `
        -DefaultCapacity $DefaultCapacity

    if (-not $capacitySelection) {
        return $null
    }

    [pscustomobject]@{
        RequestedChoice = $ModelChoice
        DeploymentName  = $ModelChoice
        ModelFormat     = $modelFormat
        ModelName       = $candidate.model.name
        ModelVersion    = $candidate.model.version
        SkuName         = $capacitySelection.SkuName
        SkuCapacity     = $capacitySelection.Capacity
        AvailableCapacity = $capacitySelection.Available
        LifecycleStatus = $candidate.lifecycleStatus
    }
}

function Select-DeployableAiModel {
    param(
        [Parameter(Mandatory)]
        [string[]]$AllowedChoices,

        [Parameter(Mandatory)]
        [string]$Location,

        [Parameter(Mandatory)]
        [string]$SubscriptionId,

        [Parameter(Mandatory)]
        [int]$DefaultCapacity
    )

    $remainingChoices = [System.Collections.Generic.List[string]]::new()
    foreach ($choice in $AllowedChoices) {
        [void]$remainingChoices.Add($choice)
    }

    $currentChoice = $null

    while ($remainingChoices.Count -gt 0) {
        if (-not $currentChoice) {
            $currentChoice = Select-AiModelSelection -Choices $remainingChoices.ToArray()
        }

        Write-Host "Checking AI model availability for '$currentChoice' in $Location..."

        $modelSpec = Resolve-AiModelSpecification `
            -ModelChoice $currentChoice `
            -Location $Location `
            -SubscriptionId $SubscriptionId `
            -DefaultCapacity $DefaultCapacity

        if ($modelSpec) {
            return $modelSpec
        }

        Write-Warning "'$currentChoice' is not currently deployable in $Location. Choose another model."
        [void]$remainingChoices.Remove($currentChoice)
        $currentChoice = $null
    }

    throw "None of the allowed AI models are currently deployable in $Location."
}

function Select-DeployableAiModelFromTeamsChat {
    param(
        [Parameter(Mandatory)]
        [string[]]$AllowedChoices,

        [Parameter(Mandatory)]
        [string]$Location,

        [Parameter(Mandatory)]
        [string]$SubscriptionId,

        [Parameter(Mandatory)]
        [int]$DefaultCapacity,

        [Parameter(Mandatory)]
        [string]$ChatId,

        [Parameter(Mandatory)]
        [int]$TimeoutMinutes
    )

    $remainingChoices = [System.Collections.Generic.List[string]]::new()
    foreach ($choice in $AllowedChoices) {
        [void]$remainingChoices.Add($choice)
    }

    while ($remainingChoices.Count -gt 0) {
        $choiceLines = for ($i = 0; $i -lt $remainingChoices.Count; $i++) {
            "$($i + 1). $($remainingChoices[$i])"
        }

        $promptMessage = @(
            "Microsoft Foundry deployment is waiting for your model selection."
            ""
            "Reply with the number or exact model name:"
            $choiceLines
            ""
            "This request expires in $TimeoutMinutes minutes."
        ) -join "`n"

        $prompt = Send-FoundryTeamsChatMessage -ChatId $ChatId -Message $promptMessage

        $selection = Wait-FoundryTeamsChatResponse `
            -ChatId $ChatId `
            -AllowedChoices $remainingChoices.ToArray() `
            -PromptCreatedDateTime $prompt.CreatedDateTime `
            -PromptMessageId $prompt.Id `
            -TimeoutMinutes $TimeoutMinutes

        $currentChoice = $selection.Choice
        Write-Host "Checking AI model availability for '$currentChoice' in $Location..."

        $modelSpec = Resolve-AiModelSpecification `
            -ModelChoice $currentChoice `
            -Location $Location `
            -SubscriptionId $SubscriptionId `
            -DefaultCapacity $DefaultCapacity

        if ($modelSpec) {
            return $modelSpec
        }

        Write-Warning "'$currentChoice' is not currently deployable in $Location. Waiting for another Teams response."

        [void](Send-FoundryTeamsChatMessage -ChatId $ChatId -Message (
            @(
                "'$currentChoice' is not currently deployable in $Location."
                "Reply again with one of the remaining options:"
                ""
                ($choiceLines | Where-Object { $_ -notmatch [regex]::Escape($currentChoice) })
            ) -join "`n"
        ))

        [void]$remainingChoices.Remove($currentChoice)
    }

    throw "None of the allowed AI models are currently deployable in $Location."
}

function Get-FoundryDeploymentSuffixFromResourceGroupName {
    param(
        [Parameter(Mandatory)]
        [string]$ResourceGroupName
    )

    if ($ResourceGroupName -notmatch '^zolab-ai-(.+)$') {
        throw "Resource group '$ResourceGroupName' does not match the expected zolab-ai-<suffix> naming pattern."
    }

    $matches[1]
}

function Get-BuildInfoPathForResourceGroup {
    param(
        [Parameter(Mandatory)]
        [string]$ResourceGroupName
    )

    $suffix = Get-FoundryDeploymentSuffixFromResourceGroupName -ResourceGroupName $ResourceGroupName
    $suffixPath = Get-BuildInfoPathForSuffix -Suffix $suffix
    if (Test-Path -LiteralPath $suffixPath) {
        return $suffixPath
    }

    $downloadedPath = Sync-BuildInfoFromBlobIfAvailable -Suffix $suffix
    if ($downloadedPath) {
        return $downloadedPath
    }

    foreach ($path in Get-BuildInfoPaths) {
        try {
            $buildInfo = Import-BuildInfoFile -Path $path
            if ($buildInfo.rg -eq $ResourceGroupName) {
                return $path
            }
        } catch {
            continue
        }
    }

    $null
}

function Get-BuildInfoRecordForResourceGroup {
    param(
        [Parameter(Mandatory)]
        [string]$ResourceGroupName
    )

    $path = Get-BuildInfoPathForResourceGroup -ResourceGroupName $ResourceGroupName
    if (-not $path) {
        return $null
    }

    [pscustomobject]@{
        Path = $path
        Data = Import-BuildInfoFile -Path $path
    }
}

function Get-FoundryBuildStatusLines {
    param(
        [Parameter(Mandatory)]
        [string]$ResourceGroupName,

        [Parameter(Mandatory)]
        [string]$GroupDisplayName,

        [Parameter(Mandatory)]
        [string]$SubscriptionId,

        [Parameter(Mandatory)]
        [string]$SecuritySubscriptionId,

        [Parameter(Mandatory)]
        [string]$LawResourceGroup,

        [Parameter(Mandatory)]
        [string]$LawWorkspaceName,

        [string]$CurrentUserAccount,

        [int]$OrphanedThresholdMinutes = 30
    )

    $resourceGroup = Get-AzResourceGroup -Name $ResourceGroupName -ErrorAction SilentlyContinue
    if (-not $resourceGroup) {
        throw "Resource group '$ResourceGroupName' was not found in subscription '$SubscriptionId'."
    }

    $buildMetadataStatus = Get-FoundryBuildMetadataStatus `
        -ResourceGroupName $ResourceGroupName `
        -OrphanedThresholdMinutes $OrphanedThresholdMinutes
    $buildInfoRecord = $buildMetadataStatus.BuildInfoRecord
    if (-not $buildInfoRecord) {
        $hint = if ($buildMetadataStatus.OrphanedCandidate) {
            " The deployment is flagged as an orphaned candidate. Consider targeted cleanup for '$ResourceGroupName'."
        } elseif ($null -ne $buildMetadataStatus.DeploymentAgeMinutes) {
            " Build metadata is still missing $($buildMetadataStatus.DeploymentAgeMinutes) minutes after deployment creation."
        } else {
            ''
        }

        throw "No build_info-<suffix>.json file was found for '$ResourceGroupName'.$hint"
    }

    $buildInfo = $buildInfoRecord.Data
    $buildInfoFileName = Split-Path -Leaf $buildInfoRecord.Path

    $storageExists = [bool](Get-AzStorageAccount -ResourceGroupName $ResourceGroupName -Name $buildInfo.storage_account -ErrorAction SilentlyContinue)
    $keyVaultExists = [bool](Get-AzResource -ResourceGroupName $ResourceGroupName -ResourceType 'Microsoft.KeyVault/vaults' -Name $buildInfo.key_vault -ErrorAction SilentlyContinue)
    $appInsightsExists = [bool](Get-AzResource -ResourceGroupName $ResourceGroupName -ResourceType 'Microsoft.Insights/components' -Name $buildInfo.appinsights -ErrorAction SilentlyContinue)
    $foundryExists = [bool](Get-AzResource -ResourceGroupName $ResourceGroupName -ResourceType 'Microsoft.CognitiveServices/accounts' -Name $buildInfo.foundry_name -ErrorAction SilentlyContinue)

    $projectExists = $false
    $projectResourceId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName/providers/Microsoft.CognitiveServices/accounts/$($buildInfo.foundry_name)/projects/$($buildInfo.foundry_project_name)"
    $projectResponse = Invoke-AzRestMethod -Path "$($projectResourceId)?api-version=2025-06-01" -Method GET -ErrorAction SilentlyContinue
    if ($projectResponse -and $projectResponse.StatusCode -eq 200) {
        $projectExists = $true
    }

    $appInsightsConnectionStatus = 'App Insights connection not found ❌'
    if ($foundryExists -and $projectExists) {
        $connectionId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName/providers/Microsoft.CognitiveServices/accounts/$($buildInfo.foundry_name)/projects/$($buildInfo.foundry_project_name)/connections/$($buildInfo.foundry_name)-appinsights"
        $connectionResponse = Invoke-AzRestMethod -Path "$($connectionId)?api-version=2025-06-01" -Method GET -ErrorAction SilentlyContinue
        if ($connectionResponse -and $connectionResponse.StatusCode -eq 200) {
            $connectionData = $connectionResponse.Content | ConvertFrom-Json
            $isSharedToAll = $connectionData.properties.isSharedToAll
            $appInsightsConnectionStatus = if ($isSharedToAll -eq $true) {
                'Shared to all projects ✅'
            } else {
                'This project only ✅'
            }
        }
    }

    $lawRbacStatus = "Entra group '$GroupDisplayName' not found ❌"
    $appInsightsAccessStatus = "Reader missing on resource group ❌"
    if ($CurrentUserAccount) {
        $userStatus = "$CurrentUserAccount membership could not be checked ❌"
    } else {
        $userStatus = "User membership check skipped (managed identity context)"
    }
    $group = Get-MgGroup -Filter "displayName eq '$GroupDisplayName'" -ErrorAction SilentlyContinue
    if ($group) {
        if ($CurrentUserAccount) {
            $statusUserId = Get-GraphUserObjectId -Account $CurrentUserAccount
            if ($statusUserId) {
                $isMember = Test-GraphGroupMembership -GroupId $group.Id -DirectoryObjectId $statusUserId
                $userStatus = if ($isMember) {
                    "$CurrentUserAccount added to $GroupDisplayName ✅"
                } else {
                    "$CurrentUserAccount is not a member of $GroupDisplayName ❌"
                }
            }
        }

        $resourceGroupScope = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName"
        $rgReaderAssignments = Get-AzRoleAssignment -ObjectId $group.Id -Scope $resourceGroupScope -RoleDefinitionName 'Reader' -ErrorAction SilentlyContinue
        $appInsightsAccessStatus = if ($rgReaderAssignments) {
            'Reader on resource group ✅'
        } else {
            'Reader missing on resource group ❌'
        }

        $lawScope = "/subscriptions/$SecuritySubscriptionId/resourceGroups/$LawResourceGroup/providers/Microsoft.OperationalInsights/workspaces/$LawWorkspaceName"
        Set-AzContext -SubscriptionId $SecuritySubscriptionId | Out-Null
        try {
            $lawAssignments = Get-AzRoleAssignment -ObjectId $group.Id -Scope $lawScope -ErrorAction SilentlyContinue
        } finally {
            Set-AzContext -SubscriptionId $SubscriptionId | Out-Null
        }

        $lawRbacStatus = if ($lawAssignments) {
            "Log Analytics Reader on $LawWorkspaceName ✅"
        } else {
            "Log Analytics Reader missing on $LawWorkspaceName ❌"
        }
    }

    $storageAccountStatus = if ($storageExists) {
        "$($buildInfo.storage_account) ✅"
    } else {
        "$($buildInfo.storage_account) ❌"
    }
    $keyVaultStatus = if ($keyVaultExists) {
        "$($buildInfo.key_vault) ✅"
    } else {
        "$($buildInfo.key_vault) ❌"
    }
    $appInsightsStatus = if ($appInsightsExists) {
        "$($buildInfo.appinsights) ✅"
    } else {
        "$($buildInfo.appinsights) ❌"
    }
    $foundryStatus = if ($foundryExists) {
        "$($buildInfo.foundry_name) ✅"
    } else {
        "$($buildInfo.foundry_name) ❌"
    }
    $projectStatus = if ($projectExists) {
        "$($buildInfo.foundry_project_name) ✅"
    } else {
        "$($buildInfo.foundry_project_name) ❌"
    }

    Write-BuildStatus `
        -ResourceGroupName $ResourceGroupName `
        -StorageAccountName $storageAccountStatus `
        -KeyVaultName $keyVaultStatus `
        -AppInsightsName $appInsightsStatus `
        -AiFoundryName $foundryStatus `
        -AiProjectName $projectStatus `
        -GenAiModelDisplay $buildInfo.genai_model `
        -BuildInfoStatus "$buildInfoFileName ✅" `
        -FoundryProjectEndpoint $buildInfo.foundry_project_endpoint `
        -AzureOpenAIEndpoint $buildInfo.azure_openai_endpoint `
        -AppInsightsConnectionStatus $appInsightsConnectionStatus `
        -AppInsightsAccessStatus $appInsightsAccessStatus `
        -LawRbacStatus $lawRbacStatus `
        -UserStatus $userStatus
}

function Remove-FoundryLawWorkspaceAccess {
    param(
        [Parameter(Mandatory)]
        [string]$SecuritySubscriptionId,

        [Parameter(Mandatory)]
        [string]$WorkloadSubscriptionId,

        [Parameter(Mandatory)]
        [string]$GroupObjectId,

        [Parameter(Mandatory)]
        [string]$LawResourceGroup,

        [Parameter(Mandatory)]
        [string]$LawWorkspaceName,

        [switch]$PreviewOnly
    )

    $lawScope = "/subscriptions/$SecuritySubscriptionId/resourceGroups/$LawResourceGroup/providers/Microsoft.OperationalInsights/workspaces/$LawWorkspaceName"
    Write-Host "Cleaning up LAW RBAC in Security subscription..."
    Set-AzContext -SubscriptionId $SecuritySubscriptionId | Out-Null

    try {
        $lawAssignments = Get-AzRoleAssignment -ObjectId $GroupObjectId -Scope $lawScope -ErrorAction SilentlyContinue
        $lawDeployments = Get-AzSubscriptionDeployment | Where-Object { $_.DeploymentName -like 'law-rbac*' }

        if (-not $PreviewOnly) {
            foreach ($assignment in $lawAssignments) {
                Write-Host "  Removing: $($assignment.RoleDefinitionName) @ $($assignment.Scope)"
                Remove-AzRoleAssignment -ObjectId $GroupObjectId `
                    -RoleDefinitionName $assignment.RoleDefinitionName `
                    -Scope $assignment.Scope `
                    -ErrorAction SilentlyContinue
            }

            foreach ($deployment in $lawDeployments) {
                Write-Host "Removing deployment record '$($deployment.DeploymentName)'..."
                Remove-AzSubscriptionDeployment -Name $deployment.DeploymentName -ErrorAction SilentlyContinue
            }
        }
    } finally {
        Set-AzContext -SubscriptionId $WorkloadSubscriptionId | Out-Null
    }

    $verb = if ($PreviewOnly) { 'Would remove' } else { 'Removed' }

    [pscustomobject]@{
        AssignmentCount = @($lawAssignments).Count
        DeploymentCount = @($lawDeployments).Count
        Status          = @(
            if ($lawAssignments) { "$verb $($lawAssignments.Count) LAW role assignments $($(if ($PreviewOnly) { 'ℹ️' } else { '✅' }))" } else { "No LAW role assignments found ℹ️" }
            if ($lawDeployments) { "$verb $($lawDeployments.Count) LAW deployment records $($(if ($PreviewOnly) { 'ℹ️' } else { '✅' }))" } else { "No LAW deployment records found ℹ️" }
        ) -join '; '
    }
}

function Invoke-CognitiveServicesAccountPurge {
    param(
        [Parameter(Mandatory)]
        [string]$AccountName,

        [Parameter(Mandatory)]
        [string]$ResourceGroupName,

        [Parameter(Mandatory)]
        [string]$Location,

        [Parameter(Mandatory)]
        [string]$SubscriptionId,

        [int]$MaxAttempts = 12,

        [int]$RetryDelaySeconds = 10
    )

    $lastErrorMessage = $null

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        $deletedAccountCountOutput = az cognitiveservices account list-deleted `
            --subscription $SubscriptionId `
            --query "[?name=='$AccountName' && location=='$Location'] | length(@)" `
            -o tsv 2>&1
        $deletedAccountCountExitCode = $LASTEXITCODE

        $deletedAccountCount = 0
        if ($deletedAccountCountExitCode -eq 0) {
            [void][int]::TryParse((($deletedAccountCountOutput -join '').Trim()), [ref]$deletedAccountCount)
        } else {
            $lastErrorMessage = "Failed to query soft-deleted Cognitive Services accounts: $($deletedAccountCountOutput -join ' ')"
        }

        if ($deletedAccountCount -lt 1) {
            if ($attempt -lt $MaxAttempts) {
                Write-Host "  Waiting for soft-deleted account '$AccountName' to become purgeable (attempt $attempt/$MaxAttempts)..."
                Start-Sleep -Seconds $RetryDelaySeconds
                continue
            }

            $status = if ($lastErrorMessage) {
                "Soft-deleted account $AccountName was not purgeable after $MaxAttempts attempts ⚠️ ($lastErrorMessage)"
            } else {
                "Soft-deleted account $AccountName was not purgeable after $MaxAttempts attempts ⚠️"
            }

            return [pscustomobject]@{
                Purged = $false
                Status = $status
            }
        }

        $purgeOutput = az cognitiveservices account purge `
            --name $AccountName `
            --resource-group $ResourceGroupName `
            --location $Location `
            --subscription $SubscriptionId 2>&1
        $purgeExitCode = $LASTEXITCODE

        if ($purgeExitCode -eq 0) {
            return [pscustomobject]@{
                Purged = $true
                Status = "Purged $AccountName ✅"
            }
        }

        $lastErrorMessage = ($purgeOutput -join ' ').Trim()
        if ($attempt -lt $MaxAttempts) {
            Write-Host "  Purge for '$AccountName' is not ready yet (attempt $attempt/$MaxAttempts). Retrying in $RetryDelaySeconds seconds..."
            Start-Sleep -Seconds $RetryDelaySeconds
        }
    }

    [pscustomobject]@{
        Purged = $false
        Status = "Failed to purge soft-deleted account $AccountName after $MaxAttempts attempts ⚠️ ($lastErrorMessage)"
    }
}

function Remove-FoundryDeletedCognitiveServicesAccount {
    param(
        [Parameter(Mandatory)]
        [string]$ResourceGroupName,

        [Parameter(Mandatory)]
        [string]$Location,

        [Parameter(Mandatory)]
        [string]$SubscriptionId
    )

    $buildInfoRecord = Get-BuildInfoRecordForResourceGroup -ResourceGroupName $ResourceGroupName
    if (-not $buildInfoRecord) {
        return [pscustomobject]@{
            Purged = $false
            Status = 'No Cognitive Services purge work required because the resource group is already deleted ℹ️'
        }
    }

    $foundryAccountName = [string]$buildInfoRecord.Data.foundry_name
    if ([string]::IsNullOrWhiteSpace($foundryAccountName)) {
        return [pscustomobject]@{
            Purged = $false
            Status = 'No Cognitive Services purge work required because the build info did not include a Foundry account name ℹ️'
        }
    }

    Write-Host "Attempting residual purge for soft-deleted account '$foundryAccountName'..."
    Invoke-CognitiveServicesAccountPurge `
        -AccountName $foundryAccountName `
        -ResourceGroupName $ResourceGroupName `
        -Location $Location `
        -SubscriptionId $SubscriptionId
}

function Remove-FoundryResourceGroup {
    param(
        [Parameter(Mandatory)]
        [string]$ResourceGroupName,

        [Parameter(Mandatory)]
        [string]$Location,

        [Parameter(Mandatory)]
        [string]$SubscriptionId
    )

    Write-Host "Deleting resource group '$ResourceGroupName'..."

    $cogAccounts = Get-AzResource -ResourceGroupName $ResourceGroupName `
        -ResourceType "Microsoft.CognitiveServices/accounts" -ErrorAction SilentlyContinue

    foreach ($cog in $cogAccounts) {
        Write-Host "  Will purge Cognitive Services account '$($cog.Name)' after RG deletion."
    }

    Remove-AzResourceGroup -Name $ResourceGroupName -Force | Out-Null

    $cognitiveServicesStatuses = @()
    foreach ($cog in $cogAccounts) {
        Write-Host "  Purging soft-deleted account '$($cog.Name)'..."
        $purgeResult = Invoke-CognitiveServicesAccountPurge `
            -AccountName $cog.Name `
            -ResourceGroupName $ResourceGroupName `
            -Location $Location `
            -SubscriptionId $SubscriptionId
        $cognitiveServicesStatuses += $purgeResult.Status
    }

    [pscustomobject]@{
        ResourceGroupStatus = "Deleted $ResourceGroupName ✅"
        CognitiveServicesStatus = if ($cogAccounts) {
            $cognitiveServicesStatuses -join '; '
        } else {
            "No Cognitive Services accounts found ℹ️"
        }
    }
}

function Remove-BuildInfoForResourceGroup {
    param(
        [Parameter(Mandatory)]
        [string]$ResourceGroupName
    )

    $suffix = Get-FoundryDeploymentSuffixFromResourceGroupName -ResourceGroupName $ResourceGroupName
    $buildInfoPath = Get-BuildInfoPathForResourceGroup -ResourceGroupName $ResourceGroupName
    $blobCleanup = Remove-BuildInfoBlobIfAvailable -Suffix $suffix

    $statusParts = @()
    if ($buildInfoPath) {
        Remove-Item -LiteralPath $buildInfoPath -Force
        Write-Host "Removed build info file '$buildInfoPath' for '$ResourceGroupName'."
        $statusParts += "Removed $(Split-Path -Leaf $buildInfoPath) for $ResourceGroupName ✅"
    } else {
        Write-Host "No local build_info file found for '$ResourceGroupName'."
        $statusParts += "No local build_info file found for $ResourceGroupName ℹ️"
    }

    $statusParts += $blobCleanup.Status
    $statusParts -join '; '
}

Write-Host "Resolved subscriptions:"
Write-Host "  zolab    : $subscriptionId"
Write-Host "  Security : $securitySubscriptionId"

# ── 1. Ensure Microsoft.Graph modules ──
if (-not (Get-Module -ListAvailable -Name Microsoft.Graph.Groups)) {
    Write-Host "Installing Microsoft.Graph.Groups module..."
    Install-Module Microsoft.Graph.Groups -Scope CurrentUser -Force
}

if ($UseTeamsChatFlow -and -not (Get-Module -ListAvailable -Name Microsoft.Graph.Teams)) {
    Write-Host "Installing Microsoft.Graph.Teams module..."
    Install-Module Microsoft.Graph.Teams -Scope CurrentUser -Force
}

Import-Module Microsoft.Graph.Groups
if ($UseTeamsChatFlow) {
    Import-Module Microsoft.Graph.Teams
}

# ── 2. Connect to Microsoft Graph (if needed) ──
$requiredGraphScopes = @(
    "Group.ReadWrite.All"
    "GroupMember.ReadWrite.All"
)
if ($UseTeamsChatFlow) {
    $requiredGraphScopes += @(
        "User.Read"
        "Chat.Create"
        "Chat.ReadWrite"
        "ChatMessage.Send"
    )
}

$ctx = Connect-FoundryGraphIfNeeded -Scopes $requiredGraphScopes

$teamsChatId = $null
$currentUser = $ctx
$requesterContext = Resolve-FoundryRequesterContext `
    -RequestedBy $RequestedBy `
    -RequestedByObjectId $RequestedByObjectId `
    -GraphContextAccount $currentUser.Account
$userId = $requesterContext.RequestedByObjectId
$requesterAccount = if ($requesterContext.RequestedBy) {
    $requesterContext.RequestedBy
} else {
    $requesterContext.RequestedByObjectId
}
$azureSession = $null

if ($UseTeamsChatFlow) {
    if ($requesterContext.RequestedBy -notmatch '@dibsecurity\.onmicrosoft\.com$') {
        throw "UseTeamsChatFlow requires a Microsoft Graph connection in the dibsecurity.onmicrosoft.com tenant."
    }

    if (-not $userId) {
        throw "Unable to resolve the Microsoft Graph user object for '$($requesterContext.RequestedBy)'."
    }

    $teamsChat = Get-OrCreate-FoundryTeamsChat -UserId $userId -Topic $TeamsChatTopic
    $teamsChatId = $teamsChat.Id
}

# ── 3. Set Azure subscription context (both Az PowerShell and az CLI) ──
# ListBuilds and BuildStatus are read-only — skip az CLI hard validation for those modes
$skipAzCli = ($ListBuilds -or $BuildStatusResourceGroup)
$azureSession = Set-AzureSession -SubscriptionId $subscriptionId -ExpectedAccount $(if ($UseTeamsChatFlow) { $requesterContext.RequestedBy } else { $null }) -SkipAzCliValidation:$skipAzCli
Write-Host "Subscription set to $($azureSession.SubscriptionName) ($subscriptionId)"
if ($UseTeamsChatFlow) {
    Write-Host "Azure account validated for Teams chat flow: $($azureSession.PowerShellAccount)"
}

if (-not $ListBuilds -and -not $BuildStatusResourceGroup) {
    Assert-FoundryDeploymentAuthorization -WorkloadSubscriptionId $subscriptionId -SecuritySubscriptionId $securitySubscriptionId -LawResourceGroup $LawResourceGroup -LawWorkspaceName $LawWorkspaceName
}

# ════════════════════════════════════════════════════════════════
#  CLEANUP MODE
# ════════════════════════════════════════════════════════════════
if ($Cleanup) {
    Write-Host ""
    try {
        if ($UseTeamsChatFlow -and $teamsChatId) {
            $cleanupStartMessage = if ($CleanupResourceGroup) {
                if ($PreviewCleanup) {
                    "Microsoft Foundry teardown preview started for '$CleanupResourceGroup'."
                } else {
                    "Microsoft Foundry teardown started for '$CleanupResourceGroup'."
                }
            } else {
                "Microsoft Foundry full teardown started."
            }
            [void](Send-FoundryTeamsChatMessage -ChatId $teamsChatId -Message $cleanupStartMessage)
        }

        $teardownProgressNotifier = $null
        try {
            if ($UseTeamsChatFlow -and $teamsChatId -and -not $PreviewCleanup) {
                $teardownProgressMessage = if ($CleanupResourceGroup) {
                    "🚧 Pls hold while we teardown: $CleanupResourceGroup 🚧"
                } else {
                    '🚧 Pls hold while we teardown managed Foundry resources 🚧'
                }
                $teardownProgressNotifier = Start-TeamsProgressNotifier `
                    -ChatId $teamsChatId `
                    -Message $teardownProgressMessage `
                    -TeamsChatHelperPath (Join-Path $PSScriptRoot 'teams-chat.ps1')
            }

            if ($CleanupResourceGroup) {
                if ($PreviewCleanup) {
                    Write-Host "=== TARGETED CLEANUP PREVIEW MODE ==="
                } else {
                    Write-Host "=== TARGETED CLEANUP MODE ==="
                }

                $targetResourceGroup = Get-AzResourceGroup -Name $CleanupResourceGroup -ErrorAction SilentlyContinue
                $resourceGroupExists = ($null -ne $targetResourceGroup)
                if (-not $resourceGroupExists) {
                    Write-Host "Resource group '$CleanupResourceGroup' is already absent; continuing residual cleanup."
                }

                $sharedAccessPlan = Get-TargetedTeardownSharedAccessPlan `
                    -TargetResourceGroupName $CleanupResourceGroup

                $group = Get-MgGroup -Filter "displayName eq '$groupDisplayName'" -ErrorAction SilentlyContinue
                if ($group) {
                    $groupObjectId = $group.Id
                    Write-Host "Found Entra group '$groupDisplayName' — ObjectId: $groupObjectId"

                    $resourceGroupScope = "/subscriptions/$subscriptionId/resourceGroups/$CleanupResourceGroup"
                    $rbacCleanupResult = Remove-FoundryManagedResourceGroupAccess `
                        -GroupObjectId $groupObjectId `
                        -ResourceGroupScope $resourceGroupScope `
                        -GroupDisplayName $groupDisplayName `
                        -PreviewOnly:$PreviewCleanup
                    $rbacStatus = $rbacCleanupResult.Status
                } else {
                    $rbacStatus = "Entra group '$groupDisplayName' not found; scoped RBAC cleanup skipped ℹ️"
                }

                if ($resourceGroupExists) {
                    if ($PreviewCleanup) {
                        $resourceGroupCleanup = Get-FoundryResourceGroupCleanupPreview -ResourceGroupName $CleanupResourceGroup
                    } else {
                        $resourceGroupCleanup = Remove-FoundryResourceGroup `
                            -ResourceGroupName $CleanupResourceGroup `
                            -Location $location `
                            -SubscriptionId $subscriptionId
                    }
                } else {
                    $residualPurgeStatus = if ($PreviewCleanup) {
                        'Would check for residual Cognitive Services purge work because the resource group is already deleted ℹ️'
                    } else {
                        (Remove-FoundryDeletedCognitiveServicesAccount `
                            -ResourceGroupName $CleanupResourceGroup `
                            -Location $location `
                            -SubscriptionId $subscriptionId).Status
                    }

                    $resourceGroupCleanup = [pscustomobject]@{
                        ResourceGroupStatus = "Resource group $CleanupResourceGroup already deleted ℹ️"
                        CognitiveServicesStatus = $residualPurgeStatus
                    }
                }

                $targetSuffix = Get-FoundryDeploymentSuffixFromResourceGroupName -ResourceGroupName $CleanupResourceGroup
                $deploymentName = "foundry-ai-env-$targetSuffix"
                $deployment = Get-AzSubscriptionDeployment -Name $deploymentName -ErrorAction SilentlyContinue
                $deploymentRecordStatus = if ($deployment) {
                    if ($PreviewCleanup) {
                        "Would remove $deploymentName ℹ️"
                    } else {
                        Write-Host "Removing deployment record '$deploymentName'..."
                        Remove-AzSubscriptionDeployment -Name $deploymentName -ErrorAction SilentlyContinue
                        "Removed $deploymentName ✅"
                    }
                } else {
                    "No deployment record named $deploymentName found ℹ️"
                }
                
                if ($PreviewCleanup) {
                    $buildInfoStatus = Get-BuildInfoRemovalPreview -ResourceGroupName $CleanupResourceGroup
                } else {
                    $buildInfoStatus = Remove-BuildInfoForResourceGroup `
                        -ResourceGroupName $CleanupResourceGroup
                }

                if ($group) {
                    if ($sharedAccessPlan.ShouldRetainLawRbac) {
                        $remainingBuildLabel = if ($sharedAccessPlan.RemainingBuildCount -eq 1) { 'build remains' } else { 'builds remain' }
                        $securityPrefix = if ($PreviewCleanup) { 'Would preserve' } else { 'Preserved' }
                        $securityStatus = "$securityPrefix LAW RBAC because $($sharedAccessPlan.RemainingBuildCount) managed $($remainingBuildLabel): $($sharedAccessPlan.RemainingBuildNames -join ', ') ℹ️"
                    } else {
                        $lawCleanupResult = Remove-FoundryLawWorkspaceAccess `
                            -SecuritySubscriptionId $securitySubscriptionId `
                            -WorkloadSubscriptionId $subscriptionId `
                            -GroupObjectId $groupObjectId `
                            -LawResourceGroup $LawResourceGroup `
                            -LawWorkspaceName $LawWorkspaceName `
                            -PreviewOnly:$PreviewCleanup
                        $securityStatus = $lawCleanupResult.Status
                    }

                    if ($sharedAccessPlan.ShouldRetainUserMembership) {
                        $remainingBuildLabel = if ($sharedAccessPlan.RemainingBuildCount -eq 1) { 'build remains' } else { 'builds remain' }
                        $userPrefix = if ($PreviewCleanup) { 'Would preserve' } else { 'Preserved' }
                        $userSuffix = if ($PreviewCleanup) { 'ℹ️' } else { '✅' }
                        if ($requesterAccount) {
                            $userStatus = "$userPrefix $requesterAccount in $groupDisplayName because $($sharedAccessPlan.RemainingBuildCount) managed $remainingBuildLabel $userSuffix"
                        } else {
                            $userStatus = "$userPrefix user membership in $groupDisplayName because $($sharedAccessPlan.RemainingBuildCount) managed $remainingBuildLabel $userSuffix"
                        }
                    } else {
                        if (-not $userId) {
                            $userStatus = 'User membership cleanup skipped because the requesting user could not be resolved ℹ️'
                        } else {
                            $requesterLabel = if ($requesterAccount) { $requesterAccount } else { $userId }
                            $isMember = Test-GraphGroupMembership -GroupId $groupObjectId -DirectoryObjectId $userId
                            if ($isMember) {
                                if ($PreviewCleanup) {
                                    $userStatus = "Would remove $requesterLabel from $groupDisplayName because no other managed builds remain ℹ️"
                                } else {
                                    Remove-MgGroupMemberByRef -GroupId $groupObjectId -DirectoryObjectId $userId
                                    $userStatus = "Removed $requesterLabel from $groupDisplayName because no other managed builds remain ✅"
                                }
                            } else {
                                $userStatus = "$requesterLabel was not a member of $groupDisplayName ℹ️"
                            }
                        }
                    }
                } else {
                    $securityStatus = "Entra group '$groupDisplayName' not found; shared LAW RBAC cleanup skipped ℹ️"
                    $userStatus = "Entra group '$groupDisplayName' not found ℹ️"
                }

                $teardownStatusLines = Write-TeardownStatus `
                    -ScopeStatus $(if ($PreviewCleanup) { "Preview teardown for $CleanupResourceGroup" } else { "Targeted teardown for $CleanupResourceGroup" }) `
                    -ResourceGroupStatus $resourceGroupCleanup.ResourceGroupStatus `
                    -RbacStatus $rbacStatus `
                    -CognitiveServicesStatus $resourceGroupCleanup.CognitiveServicesStatus `
                    -DeploymentRecordStatus $deploymentRecordStatus `
                    -BuildInfoStatus $buildInfoStatus `
                    -SecurityStatus $securityStatus `
                    -UserStatus $userStatus `
                    -PreviewMode:$PreviewCleanup
            } else {
                Write-Host "=== CLEANUP MODE ==="

                $rgList = @(Get-AzResourceGroup | Where-Object { $_.ResourceGroupName -match '^zolab-ai-.{4,}$' })
                $managedResourceGroupScopes = @(
                    $rgList |
                        ForEach-Object { "/subscriptions/$subscriptionId/resourceGroups/$($_.ResourceGroupName)" }
                )

                $group = Get-MgGroup -Filter "displayName eq '$groupDisplayName'" -ErrorAction SilentlyContinue
                if ($group) {
                    $groupObjectId = $group.Id
                    Write-Host "Found Entra group '$groupDisplayName' — ObjectId: $groupObjectId"

                    Write-Host "Removing managed Foundry RBAC role assignments for '$groupDisplayName'..."
                    $assignments = @(Get-AzRoleAssignment -ObjectId $groupObjectId -ErrorAction SilentlyContinue)
                    $assignmentPlan = Get-FoundryFullTeardownAssignmentPlan `
                        -Assignments $assignments `
                        -ManagedResourceGroupScopes $managedResourceGroupScopes
                    $managedAssignments = @($assignmentPlan.ManagedAssignments)
                    $preservedAssignments = @($assignmentPlan.PreservedAssignments)

                    foreach ($a in $managedAssignments) {
                        Write-Host "  Removing managed role: $($a.RoleDefinitionName) @ $($a.Scope)"
                        Remove-AzRoleAssignment -ObjectId $groupObjectId `
                            -RoleDefinitionName $a.RoleDefinitionName `
                            -Scope $a.Scope `
                            -ErrorAction SilentlyContinue
                    }

                    foreach ($a in $preservedAssignments) {
                        Write-Host "  Preserving non-managed role: $($a.RoleDefinitionName) @ $($a.Scope)"
                    }

                    $rbacStatus = if ($managedAssignments) {
                        "Removed $($managedAssignments.Count) managed role assignments for $groupDisplayName ✅"
                    } else {
                        "No managed role assignments found for $groupDisplayName ℹ️"
                    }
                    if ($preservedAssignments) {
                        $rbacStatus += "; preserved $($preservedAssignments.Count) non-managed assignment(s) ℹ️"
                    }

                    if (-not $userId) {
                        $userStatus = 'User membership cleanup skipped because the requesting user could not be resolved ℹ️'
                    } else {
                        $requesterLabel = if ($requesterAccount) { $requesterAccount } else { $userId }
                        $isMember = Test-GraphGroupMembership -GroupId $groupObjectId -DirectoryObjectId $userId
                        if ($isMember) {
                            Remove-MgGroupMemberByRef -GroupId $groupObjectId -DirectoryObjectId $userId
                            $userStatus = "Removed $requesterLabel from $groupDisplayName ✅"
                        } else {
                            $userStatus = "$requesterLabel was not a member of $groupDisplayName ℹ️"
                        }
                    }
                } else {
                    $rbacStatus = "Entra group '$groupDisplayName' not found; RBAC cleanup skipped ℹ️"
                    $userStatus = "Entra group '$groupDisplayName' not found ℹ️"
                }

                $resourceGroupResults = foreach ($rg in $rgList) {
                    Remove-FoundryResourceGroup `
                        -ResourceGroupName $rg.ResourceGroupName `
                        -Location $location `
                        -SubscriptionId $subscriptionId
                }
                $resourceGroupStatus = if ($resourceGroupResults) {
                    "Deleted $($resourceGroupResults.Count) managed resource groups ✅"
                } else {
                    "No zolab-ai-<suffix> resource groups found ℹ️"
                }
                $cognitiveServicesStatus = if ($resourceGroupResults) {
                    ($resourceGroupResults | ForEach-Object { $_.CognitiveServicesStatus }) -join '; '
                } else {
                    "No Cognitive Services purge work was required ℹ️"
                }

                $deployments = Get-AzSubscriptionDeployment | Where-Object { $_.DeploymentName -like 'foundry-ai-env*' }
                foreach ($d in $deployments) {
                    Write-Host "Removing deployment record '$($d.DeploymentName)'..."
                    Remove-AzSubscriptionDeployment -Name $d.DeploymentName -ErrorAction SilentlyContinue
                }
                $deploymentRecordStatus = if ($deployments) {
                    "Removed $($deployments.Count) foundry deployment records ✅"
                } else {
                    "No foundry deployment records found ℹ️"
                }

                $securityStatus = "No Security cleanup required ℹ️"
                if ($group) {
                    $lawCleanupResult = Remove-FoundryLawWorkspaceAccess `
                        -SecuritySubscriptionId $securitySubscriptionId `
                        -WorkloadSubscriptionId $subscriptionId `
                        -GroupObjectId $groupObjectId `
                        -LawResourceGroup $LawResourceGroup `
                        -LawWorkspaceName $LawWorkspaceName
                    $securityStatus = $lawCleanupResult.Status
                }

                $buildInfoStatuses = @()
                foreach ($rg in $rgList) {
                    $buildInfoStatuses += Remove-BuildInfoForResourceGroup -ResourceGroupName $rg.ResourceGroupName
                }

                $orphanedBuildInfoPaths = @(Get-BuildInfoPaths)
                if ($orphanedBuildInfoPaths) {
                    foreach ($path in $orphanedBuildInfoPaths) {
                        $fileName = Split-Path -Leaf $path
                        if ($fileName -match '^build_info-(.+)\.json$') {
                            $buildInfoStatuses += (Remove-BuildInfoBlobIfAvailable -Suffix $matches[1]).Status
                        }

                        Remove-Item -LiteralPath $path -Force
                        Write-Host "Removed stale build info file '$path'."
                    }
                    $buildInfoStatuses += "Removed $($orphanedBuildInfoPaths.Count) orphaned local build info file(s) ✅"
                }

                $buildInfoStatus = if ($buildInfoStatuses) {
                    $buildInfoStatuses -join '; '
                } else {
                    Write-Host "No build_info files found to remove."
                    "No build info files found ℹ️"
                }

                $teardownStatusLines = Write-TeardownStatus `
                    -ScopeStatus 'Full teardown of managed Foundry resources' `
                    -ResourceGroupStatus $resourceGroupStatus `
                    -RbacStatus $rbacStatus `
                    -CognitiveServicesStatus $cognitiveServicesStatus `
                    -DeploymentRecordStatus $deploymentRecordStatus `
                    -BuildInfoStatus $buildInfoStatus `
                    -SecurityStatus $securityStatus `
                    -UserStatus $userStatus
            }
        } finally {
            Stop-TeamsProgressNotifier -Notifier $teardownProgressNotifier
        }

        Write-Host ""
        Write-Host "=== Cleanup complete ==="
        $teardownStatusLines | ForEach-Object { Write-Host $_ }

        if ($UseTeamsChatFlow -and $teamsChatId) {
            [void](Send-FoundryTeamsChatMessage -ChatId $teamsChatId -Message ($teardownStatusLines -join "`n"))
        }
        return
    } catch {
        if ($UseTeamsChatFlow -and $teamsChatId) {
            $failureDetails = @(
                'Microsoft Foundry teardown failed.'
                "Account: $(if ($requesterAccount) { $requesterAccount } else { 'unknown-requester' })"
            )
            if ($CleanupResourceGroup) {
                $failureDetails += "Target resource group: $CleanupResourceGroup"
            }
            $failureDetails += ''
            $failureDetails += 'Error:'
            $failureDetails += $_.Exception.Message
            [void](Send-FoundryTeamsChatMessage -ChatId $teamsChatId -Message ($failureDetails -join "`n"))
        }
        throw
    }
}

# ════════════════════════════════════════════════════════════════
#  LIST BUILDS MODE
# ════════════════════════════════════════════════════════════════
if ($ListBuilds) {
    try {
        $buildListLines = Get-FoundryBuildListLines `
            -SubscriptionId $subscriptionId `
            -OrphanedThresholdMinutes $OrphanedBuildThresholdMinutes
        $buildListLines | ForEach-Object { Write-Host $_ }

        if ($UseTeamsChatFlow -and $teamsChatId) {
            [void](Send-FoundryTeamsChatMessage -ChatId $teamsChatId -Message ($buildListLines -join "`n"))
        }

        return
    } catch {
        if ($UseTeamsChatFlow -and $teamsChatId) {
            $failureDetails = @(
                'Microsoft Foundry list builds request failed.'
                "Account: $($currentUser.Account)"
                ''
                'Error:'
                $_.Exception.Message
            )
            [void](Send-FoundryTeamsChatMessage -ChatId $teamsChatId -Message ($failureDetails -join "`n"))
        }

        throw
    }
}

# ════════════════════════════════════════════════════════════════
#  BUILD STATUS MODE
# ════════════════════════════════════════════════════════════════
if ($BuildStatusResourceGroup) {
    try {
        $buildStatusParams = @{
            ResourceGroupName      = $BuildStatusResourceGroup
            GroupDisplayName       = $groupDisplayName
            SubscriptionId         = $subscriptionId
            SecuritySubscriptionId = $securitySubscriptionId
            LawResourceGroup       = $LawResourceGroup
            LawWorkspaceName       = $LawWorkspaceName
            OrphanedThresholdMinutes = $OrphanedBuildThresholdMinutes
        }
        if ($currentUser.Account) {
            $buildStatusParams.CurrentUserAccount = $currentUser.Account
        }
        $buildStatusLines = Get-FoundryBuildStatusLines @buildStatusParams

        $buildStatusLines | ForEach-Object { Write-Host $_ }

        if ($UseTeamsChatFlow -and $teamsChatId) {
            [void](Send-FoundryTeamsChatMessage -ChatId $teamsChatId -Message ($buildStatusLines -join "`n"))
        }

        return
    } catch {
        if ($UseTeamsChatFlow -and $teamsChatId) {
            $failureDetails = @(
                'Microsoft Foundry build status request failed.'
                "Account: $($currentUser.Account)"
                "Target resource group: $BuildStatusResourceGroup"
                ''
                'Error:'
                $_.Exception.Message
            )
            [void](Send-FoundryTeamsChatMessage -ChatId $teamsChatId -Message ($failureDetails -join "`n"))
        }

        throw
    }
}

# ════════════════════════════════════════════════════════════════
#  DEPLOY MODE
# ════════════════════════════════════════════════════════════════
$selectedAiModelSpec = $null
$suffix = $null

try {
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

    # ── Add the deploying user to the zolab-ai-dev group ──
    $currentUser = Get-MgContext
    $requesterContext = Resolve-FoundryRequesterContext `
        -RequestedBy $RequestedBy `
        -RequestedByObjectId $RequestedByObjectId `
        -GraphContextAccount $currentUser.Account
    $requesterAccount = if ($requesterContext.RequestedBy) {
        $requesterContext.RequestedBy
    } else {
        $requesterContext.RequestedByObjectId
    }

    if ($UseTeamsChatFlow -and $requesterContext.RequestedBy -notmatch '@dibsecurity\.onmicrosoft\.com$') {
        throw "UseTeamsChatFlow requires a Microsoft Graph connection in the dibsecurity.onmicrosoft.com tenant."
    }

    $userId = $requesterContext.RequestedByObjectId
    if (-not $userId) {
        if ($requesterContext.RequestedBy) {
            throw "Unable to resolve the Microsoft Graph user object for '$($requesterContext.RequestedBy)'."
        }

        throw "Unable to determine the requesting user for this deployment. Pass -RequestedBy or -RequestedByObjectId when running under managed identity."
    }

    $isMember = Test-GraphGroupMembership -GroupId $groupObjectId -DirectoryObjectId $userId
    if ($isMember) {
        Write-Host "Current user ($requesterAccount) is already a member of '$groupDisplayName'"
    } else {
        New-MgGroupMember -GroupId $groupObjectId -DirectoryObjectId $userId
        Write-Host "Added current user ($requesterAccount) to '$groupDisplayName'"
    }

    if ($UseTeamsChatFlow) {
        $teamsChat = Get-OrCreate-FoundryTeamsChat -UserId $userId -Topic $TeamsChatTopic
        $teamsChatId = $teamsChat.Id
        Write-Host "Using Teams chat '$TeamsChatTopic' ($teamsChatId) for model selection and build notifications."
        [void](Send-FoundryTeamsChatMessage -ChatId $teamsChatId -Message (
            @(
                "Microsoft Foundry deployment started."
                "Account: $requesterAccount"
                "Location: $location"
                "I'll wait here for your model choice and post the build result in this chat."
            ) -join "`n"
        ))
    }

    # ── Select and validate AI model ──
    if ($SelectedAiModel) {
        # Non-interactive path: validate against allowed list, then resolve
        $allowedChoices = Get-AllowedAiModelChoices
        if ($SelectedAiModel -notin $allowedChoices) {
            throw "Invalid AI model '$SelectedAiModel'. Allowed models: $($allowedChoices -join ', ')"
        }
        Write-Host "Non-interactive model selection: '$SelectedAiModel'"
        $selectedAiModelSpec = Resolve-AiModelSpecification `
            -ModelChoice $SelectedAiModel `
            -Location $location `
            -SubscriptionId $subscriptionId `
            -DefaultCapacity $defaultModelCapacity
        if (-not $selectedAiModelSpec) {
            throw "AI model '$SelectedAiModel' is not currently deployable in $location."
        }
    } elseif ($UseTeamsChatFlow) {
        $selectedAiModelSpec = Select-DeployableAiModelFromTeamsChat `
            -AllowedChoices (Get-AllowedAiModelChoices) `
            -Location $location `
            -SubscriptionId $subscriptionId `
            -DefaultCapacity $defaultModelCapacity `
            -ChatId $teamsChatId `
            -TimeoutMinutes $TeamsChatSelectionTimeoutMinutes
    } else {
        $selectedAiModelSpec = Select-DeployableAiModel `
            -AllowedChoices (Get-AllowedAiModelChoices) `
            -Location $location `
            -SubscriptionId $subscriptionId `
            -DefaultCapacity $defaultModelCapacity
    }

    Write-Host "AI model selection:"
    Write-Host "  Requested option : $($selectedAiModelSpec.RequestedChoice)"
    Write-Host "  Resolved model   : $($selectedAiModelSpec.ModelName) [$($selectedAiModelSpec.ModelFormat)] $($selectedAiModelSpec.ModelVersion)"
    Write-Host "  Deployment SKU   : $($selectedAiModelSpec.SkuName) x $($selectedAiModelSpec.SkuCapacity)"

    if ($UseTeamsChatFlow) {
        [void](Send-FoundryTeamsChatMessage -ChatId $teamsChatId -Message (
            @(
                "Model confirmed for deployment."
                "Requested option: $($selectedAiModelSpec.RequestedChoice)"
                "Resolved model: $($selectedAiModelSpec.ModelName) [$($selectedAiModelSpec.ModelFormat)] $($selectedAiModelSpec.ModelVersion)"
                "Deployment SKU: $($selectedAiModelSpec.SkuName) x $($selectedAiModelSpec.SkuCapacity)"
            ) -join "`n"
        ))
    }

    # ── Generate random 6-char alphanumeric suffix ──
    $suffix = -join ((48..57) + (97..122) | Get-Random -Count 6 | ForEach-Object { [char]$_ })
    Write-Host "Generated suffix: $suffix"
    Write-Host "Resource group will be: zolab-ai-$suffix"

    $buildProgressNotifier = $null
    try {
        if ($UseTeamsChatFlow -and $teamsChatId) {
            $buildProgressNotifier = Start-TeamsProgressNotifier `
                -ChatId $teamsChatId `
                -Message "🚧 👷 The Bobs Are Still Building 👷🚧 " `
                -TeamsChatHelperPath (Join-Path $PSScriptRoot 'teams-chat.ps1')
        }

        # ── Deploy Bicep (subscription-scoped via az cli) ──
        Write-Host ""
        Write-Host "Deploying AI Foundry environment..."
        $deployResult = Invoke-AzCliCommand -Arguments @(
            'deployment', 'sub', 'create',
            '--location', $location,
            '--template-file', (Join-Path $PSScriptRoot 'main.bicep'),
            '--name', "foundry-ai-env-$suffix",
            '--parameters',
            "aiDevGroupObjectId=$groupObjectId",
            "securitySubscriptionId=$securitySubscriptionId",
            "lawResourceGroup=$LawResourceGroup",
            "lawWorkspaceName=$LawWorkspaceName",
            "suffix=$suffix",
            "aiModelDeploymentName=$($selectedAiModelSpec.DeploymentName)",
            "aiModelName=$($selectedAiModelSpec.ModelName)",
            "aiModelFormat=$($selectedAiModelSpec.ModelFormat)",
            "aiModelVersion=$($selectedAiModelSpec.ModelVersion)",
            "aiModelSkuName=$($selectedAiModelSpec.SkuName)",
            "aiModelSkuCapacity=$($selectedAiModelSpec.SkuCapacity)",
            '--only-show-errors',
            '--output', 'json'
        ) -TimeoutSeconds $DeploymentAzCliTimeoutSeconds

        if ($deployResult.ExitCode -ne 0) {
            $errorText = if ([string]::IsNullOrWhiteSpace($deployResult.OutputText)) {
                'Deployment failed with no output.'
            } else {
                $deployResult.OutputText
            }
            throw "Deployment failed.`n$errorText"
        }

        $jsonString = $deployResult.StdOut.Trim()
        if ([string]::IsNullOrWhiteSpace($jsonString)) {
            $outputText = if ([string]::IsNullOrWhiteSpace($deployResult.OutputText)) {
                'Deployment returned no output.'
            } else {
                $deployResult.OutputText
            }
            throw "Deployment returned no JSON output.`n$outputText"
        }

        try {
            $result = $jsonString | ConvertFrom-Json
        } catch {
            throw "Deployment output was not valid JSON.`n$jsonString"
        }

        Write-Host ""
        Write-Host "=== Deployment Result ==="
        Write-Host "ProvisioningState        : $($result.properties.provisioningState)"
        Write-Host "Resource Group           : $($result.properties.outputs.resourceGroupName.value)"
        Write-Host "Suffix                   : $($result.properties.outputs.suffix.value)"
        Write-Host "Storage Account          : $($result.properties.outputs.storageAccountName.value)"
        Write-Host "Key Vault                : $($result.properties.outputs.keyVaultName.value)"
        Write-Host "App Insights             : $($result.properties.outputs.appInsightsName.value)"
        Write-Host "AI Foundry               : $($result.properties.outputs.aiFoundryName.value)"
        Write-Host "AI Project               : $($result.properties.outputs.aiProjectName.value)"
        Write-Host "AI Model Deployment      : $($result.properties.outputs.aiModelDeploymentName.value)"
        Write-Host "Foundry Project Endpoint : $($result.properties.outputs.foundryProjectEndpoint.value)"
        Write-Host "Azure OpenAI Endpoint    : $($result.properties.outputs.azureOpenAIEndpoint.value)"
        Write-Host ""
        Write-Host "Portal link:"
        Write-Host "https://portal.azure.com/#@/resource/subscriptions/$subscriptionId/resourceGroups/$($result.properties.outputs.resourceGroupName.value)/overview"

        $securitySubId = $securitySubscriptionId
        $lawScope = "/subscriptions/$securitySubId/resourceGroups/$LawResourceGroup/providers/Microsoft.OperationalInsights/workspaces/$LawWorkspaceName"

        Invoke-PostDeployStep -StepName "Ensure LAW RBAC on $LawWorkspaceName" -Action {
            Write-Host ""
            Write-Host "Deploying Log Analytics Reader RBAC to Security subscription..."
            $setSecuritySubscription = Invoke-AzCliCommand -Arguments @('account', 'set', '--subscription', $securitySubId, '--output', 'none') -TimeoutSeconds $PostDeployAzCliTimeoutSeconds
            if ($setSecuritySubscription.ExitCode -ne 0) {
                throw "Failed to switch Azure CLI context to security subscription: $($setSecuritySubscription.OutputText)"
            }

            try {
                $existingLawAssignmentResult = Invoke-AzCliCommand -Arguments @(
                    'role', 'assignment', 'list',
                    '--assignee-object-id', $groupObjectId,
                    '--scope', $lawScope,
                    '--query', "[?roleDefinitionName=='Log Analytics Reader'] | length(@)",
                    '--output', 'tsv'
                ) -TimeoutSeconds $PostDeployAzCliTimeoutSeconds

                if ($existingLawAssignmentResult.ExitCode -ne 0) {
                    throw "Failed to verify existing Log Analytics Reader RBAC on $LawWorkspaceName. $($existingLawAssignmentResult.OutputText)"
                }

                if ($existingLawAssignmentResult.OutputText.Trim() -eq '1') {
                    Write-Host "  Log Analytics Reader is already assigned to '$groupDisplayName' on $LawWorkspaceName workspace."
                } else {
                    $lawAssignmentResult = Invoke-AzCliCommand -Arguments @(
                        'role', 'assignment', 'create',
                        '--assignee-object-id', $groupObjectId,
                        '--assignee-principal-type', 'Group',
                        '--role', 'Log Analytics Reader',
                        '--scope', $lawScope,
                        '--subscription', $securitySubId,
                        '--output', 'json'
                    ) -TimeoutSeconds $PostDeployAzCliTimeoutSeconds

                    if ($lawAssignmentResult.ExitCode -ne 0) {
                        throw "LAW RBAC deployment failed. $($lawAssignmentResult.OutputText)"
                    }

                    $lawWarnings = ($lawAssignmentResult.OutputText -split "`r?`n") | Where-Object { $_ -match '^WARNING:|^BCP\d|\.bicep\(' }
                    if ($lawWarnings) {
                        $lawWarnings | ForEach-Object { Write-Host $_ }
                    }

                    Write-Host "  Log Analytics Reader assigned to '$groupDisplayName' on $LawWorkspaceName workspace."
                }
            } finally {
                $resetSubscription = Invoke-AzCliCommand -Arguments @('account', 'set', '--subscription', $subscriptionId, '--output', 'none') -TimeoutSeconds $PostDeployAzCliTimeoutSeconds
                if ($resetSubscription.ExitCode -ne 0) {
                    Write-Warning "Failed to switch Azure CLI context back to workload subscription: $($resetSubscription.OutputText)"
                }
            }
        }

        $connectionId = "/subscriptions/$subscriptionId/resourceGroups/$($result.properties.outputs.resourceGroupName.value)/providers/Microsoft.CognitiveServices/accounts/$($result.properties.outputs.aiFoundryName.value)/projects/$($result.properties.outputs.aiProjectName.value)/connections/$($result.properties.outputs.aiFoundryName.value)-appinsights"
        $appInsightsConnectionStatus = Invoke-PostDeployStep -StepName 'Resolve App Insights connection scope' -Action {
            $connectionResult = Invoke-AzCliCommand -Arguments @(
                'resource', 'show',
                '--ids', $connectionId,
                '--api-version', '2025-06-01',
                '--query', 'properties.isSharedToAll',
                '--output', 'tsv'
            ) -TimeoutSeconds $PostDeployAzCliTimeoutSeconds

            if ($connectionResult.ExitCode -ne 0) {
                throw "Failed to resolve App Insights connection scope. $($connectionResult.OutputText)"
            }

            if ($connectionResult.OutputText.Trim().ToLowerInvariant() -eq 'true') {
                'Shared to all projects ✅'
            } else {
                'This project only ✅'
            }
        }

        $resourceGroupScope = "/subscriptions/$subscriptionId/resourceGroups/$($result.properties.outputs.resourceGroupName.value)"
        $appInsightsAccessStatus = Invoke-PostDeployStep -StepName 'Verify Reader access on deployment resource group' -Action {
            $rgReaderAssignments = Get-AzRoleAssignment -ObjectId $groupObjectId -Scope $resourceGroupScope -RoleDefinitionName 'Reader' -ErrorAction SilentlyContinue
            if ($rgReaderAssignments) {
                'Reader on resource group ✅'
            } else {
                'Reader missing on resource group ❌'
            }
        }

        $buildInfoPath = Invoke-PostDeployStep -StepName 'Persist build info locally' -Action {
            $outputPath = Get-BuildInfoPathForSuffix -Suffix $suffix
            Write-BuildInfoJson `
                -OutputPath $outputPath `
                -ResourceGroupName $result.properties.outputs.resourceGroupName.value `
                -AppInsightsName $result.properties.outputs.appInsightsName.value `
                -FoundryProjectEndpoint $result.properties.outputs.foundryProjectEndpoint.value `
                -AzureOpenAIEndpoint $result.properties.outputs.azureOpenAIEndpoint.value `
                -StorageAccountName $result.properties.outputs.storageAccountName.value `
                -KeyVaultName $result.properties.outputs.keyVaultName.value `
                -GenAiModel $selectedAiModelSpec.DeploymentName `
                -AiFoundryName $result.properties.outputs.aiFoundryName.value `
                -AiProjectName $result.properties.outputs.aiProjectName.value `
                -RequestedBy $requesterAccount `
                -RequestedByObjectId $requesterContext.RequestedByObjectId
            $outputPath
        }
        Write-Host "📝 Build info written to $buildInfoPath"

        $buildInfoBlob = Invoke-PostDeployStep -StepName 'Upload build info to blob storage' -Action {
            Sync-BuildInfoToBlobIfAvailable -BuildInfoPath $buildInfoPath -TimeoutSeconds $PostDeployAzCliTimeoutSeconds
        }
        if ($buildInfoBlob) {
            Write-Host "☁️ Build info uploaded to blob: $($buildInfoBlob.ContainerName)/$($buildInfoBlob.BlobName)"
        }

        $buildStatusLines = Write-BuildStatus `
            -ResourceGroupName $result.properties.outputs.resourceGroupName.value `
            -StorageAccountName $result.properties.outputs.storageAccountName.value `
            -KeyVaultName $result.properties.outputs.keyVaultName.value `
            -AppInsightsName $result.properties.outputs.appInsightsName.value `
            -AiFoundryName $result.properties.outputs.aiFoundryName.value `
            -AiProjectName $result.properties.outputs.aiProjectName.value `
            -GenAiModelDisplay "$($selectedAiModelSpec.DeploymentName) ($($selectedAiModelSpec.SkuName))" `
            -BuildInfoStatus "$(Split-Path -Leaf $buildInfoPath) ✅" `
            -FoundryProjectEndpoint $result.properties.outputs.foundryProjectEndpoint.value `
            -AzureOpenAIEndpoint $result.properties.outputs.azureOpenAIEndpoint.value `
            -AppInsightsConnectionStatus $appInsightsConnectionStatus `
            -AppInsightsAccessStatus $appInsightsAccessStatus `
            -LawRbacStatus "Log Analytics Reader on $LawWorkspaceName ✅" `
            -UserStatus "$requesterAccount added to $groupDisplayName ✅"
        $buildStatusLines | ForEach-Object { Write-Host $_ }

        if ($UseTeamsChatFlow -and $teamsChatId) {
            try {
                [void](Send-FoundryTeamsChatMessage -ChatId $teamsChatId -Message ($buildStatusLines -join "`n"))
            } catch {
                Write-Warning "Failed to send Teams build notification: $($_.Exception.Message)"
            }
        }
    } finally {
        Stop-TeamsProgressNotifier -Notifier $buildProgressNotifier
    }
} catch {
    if ($UseTeamsChatFlow -and $teamsChatId) {
        try {
            $failureDetails = @(
                "Microsoft Foundry deployment failed."
                "Account: $requesterAccount"
            )
            if ($selectedAiModelSpec) {
                $failureDetails += "Model: $($selectedAiModelSpec.RequestedChoice)"
            }
            if ($suffix) {
                $failureDetails += "Suffix: $suffix"
            }
            $failureDetails += ""
            $failureDetails += "Error:"
            $failureDetails += $_.Exception.Message

            [void](Send-FoundryTeamsChatMessage -ChatId $teamsChatId -Message ($failureDetails -join "`n"))
        } catch {
            Write-Warning "Failed to send Teams failure notification: $($_.Exception.Message)"
        }
    }

    throw
}
