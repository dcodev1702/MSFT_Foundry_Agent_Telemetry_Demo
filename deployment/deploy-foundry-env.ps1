# Deploy (or clean up) Azure AI Foundry Environment
# Pre-req: Az PowerShell, Microsoft.Graph PowerShell, Azure CLI with Bicep
# Usage:
#   .\deploy-foundry-env.ps1              # deploy all resources + RBAC
#   .\deploy-foundry-env.ps1 -UseTeamsChatFlow
#   .\deploy-foundry-env.ps1 -ListBuilds
#   .\deploy-foundry-env.ps1 -BuildStatusResourceGroup zolab-ai-abc123
#   .\deploy-foundry-env.ps1 -Cleanup -CleanupResourceGroup zolab-ai-abc123
#   .\deploy-foundry-env.ps1 -Cleanup     # tear down resources + RBAC (keeps Entra group)
param(
    [switch]$Cleanup,
    [string]$CleanupResourceGroup,
    [switch]$ListBuilds,
    [string]$BuildStatusResourceGroup,
    [switch]$UseTeamsChatFlow,
    [int]$TeamsChatSelectionTimeoutMinutes = 30,
    [string]$TeamsChatTopic = 'Microsoft Foundry Deployments',
    [string]$SelectedAiModel   # Non-interactive model selection (bypasses PromptForChoice)
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'teams-chat.ps1')

if ($CleanupResourceGroup) {
    $Cleanup = $true
}

if ($BuildStatusResourceGroup -and $Cleanup) {
    throw "Build status mode cannot be combined with cleanup mode."
}

if ($ListBuilds -and ($Cleanup -or $BuildStatusResourceGroup)) {
    throw "List builds mode cannot be combined with cleanup or build status mode."
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

function Ensure-AzureSession {
    param(
        [Parameter(Mandatory)]
        [string]$SubscriptionId,

        [string]$ExpectedAccount
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

    [pscustomobject]@{
        SubscriptionId    = $SubscriptionId
        SubscriptionName  = $targetSubscription.Name
        TenantId          = $targetSubscription.TenantId
        PowerShellAccount = $azContext.Account.Id
        CliAccount        = $cliContext.account
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

`$ctx = Get-MgContext
`$missingScopes = if (`$ctx) {
    `$requiredGraphScopes | Where-Object { `$_ -notin `$ctx.Scopes }
} else {
    `$requiredGraphScopes
}

if (-not `$ctx -or `$missingScopes.Count -gt 0) {
    Connect-MgGraph -Scopes `$requiredGraphScopes -ContextScope CurrentUser -NoWelcome | Out-Null
}

while (-not (Test-Path -LiteralPath `$StopFilePath)) {
    Start-Sleep -Seconds `$IntervalSeconds
    if (Test-Path -LiteralPath `$StopFilePath) {
        break
    }

    [void](Send-FoundryTeamsChatMessage -ChatId `$ChatId -Message `$Message)
}
"@

    [System.IO.File]::WriteAllText($scriptPath, $scriptContent, $utf8Bom)

    $process = Start-Process -FilePath 'pwsh.exe' `
        -ArgumentList @(
            '-NoLogo',
            '-NoProfile',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            $scriptPath
        ) `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -WindowStyle Hidden `
        -PassThru

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

        [string]$UserStatus = 'Not applicable'
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
        '● 🧹🗑️ Teardown complete!'
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
    $lines += 'Environment cleanup finished. ✅'

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
        [string]$RequestedBy
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

    $buildInfo | ConvertTo-Json | Set-Content -Path $OutputPath -Encoding utf8
}

function Get-BuildInfoDirectory {
    Split-Path $PSScriptRoot -Parent
}

function Get-LegacyBuildInfoPath {
    Join-Path (Get-BuildInfoDirectory) 'build_info.json'
}

function Get-BuildInfoPathForSuffix {
    param(
        [Parameter(Mandatory)]
        [string]$Suffix
    )

    Join-Path (Get-BuildInfoDirectory) "build_info-$Suffix.json"
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

function Read-BuildInfoFile {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Get-FoundryManagedResourceGroups {
    Get-AzResourceGroup | Where-Object { $_.ResourceGroupName -match '^zolab-ai-.{4,}$' } | Sort-Object ResourceGroupName
}

function Get-FoundryBuildInventory {
    param(
        [string]$ExcludeResourceGroupName
    )

    $inventory = foreach ($resourceGroup in Get-FoundryManagedResourceGroups) {
        if ($ExcludeResourceGroupName -and $resourceGroup.ResourceGroupName -ieq $ExcludeResourceGroupName) {
            continue
        }

        $buildInfoRecord = Get-BuildInfoRecordForResourceGroup -ResourceGroupName $resourceGroup.ResourceGroupName
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
            RequestedBy       = $requestedBy
            OwnershipKnown    = -not [string]::IsNullOrWhiteSpace($requestedBy)
        }
    }

    @($inventory)
}

function Get-TargetedTeardownSharedAccessPlan {
    param(
        [Parameter(Mandatory)]
        [string]$TargetResourceGroupName,

        [Parameter(Mandatory)]
        [string]$CurrentUserAccount
    )

    $remainingBuilds = @(Get-FoundryBuildInventory -ExcludeResourceGroupName $TargetResourceGroupName)
    $remainingBuildsForCurrentUser = @(
        $remainingBuilds | Where-Object { $_.RequestedBy -and $_.RequestedBy -ieq $CurrentUserAccount }
    )
    $remainingBuildsWithUnknownOwnership = @(
        $remainingBuilds | Where-Object { -not $_.OwnershipKnown }
    )

    [pscustomobject]@{
        RemainingBuilds                  = $remainingBuilds
        RemainingBuildCount              = $remainingBuilds.Count
        RemainingBuildNames              = @($remainingBuilds | ForEach-Object { $_.ResourceGroupName })
        RemainingBuildsForCurrentUser    = $remainingBuildsForCurrentUser
        RemainingBuildsForCurrentUserCount = $remainingBuildsForCurrentUser.Count
        RemainingUnknownOwnershipBuilds  = $remainingBuildsWithUnknownOwnership
        RemainingUnknownOwnershipCount   = $remainingBuildsWithUnknownOwnership.Count
        ShouldRetainLawRbac              = ($remainingBuilds.Count -gt 0)
        ShouldRetainUserMembership       = (
            $remainingBuilds.Count -gt 0 -and (
                $remainingBuildsForCurrentUser.Count -gt 0 -or
                $remainingBuildsWithUnknownOwnership.Count -gt 0
            )
        )
    }
}

function Get-FoundryBuildListLines {
    param(
        [Parameter(Mandatory)]
        [string]$SubscriptionId
    )

    Set-AzContext -SubscriptionId $SubscriptionId | Out-Null

    $resourceGroups = @(Get-FoundryManagedResourceGroups)
    $buildInfoPaths = @(Get-BuildInfoPaths)
    $matchedBuildInfoPaths = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)

    $lines = @(
        ''
        '● 📚 Foundry builds'
        ''
    )

    if ($resourceGroups.Count -eq 0) {
        $lines += 'No active managed resource groups found ℹ️'
    } else {
        $lines += 'Active resource groups:'
        for ($i = 0; $i -lt $resourceGroups.Count; $i++) {
            $resourceGroupName = $resourceGroups[$i].ResourceGroupName
            $buildInfoRecord = Get-BuildInfoRecordForResourceGroup -ResourceGroupName $resourceGroupName
            if ($buildInfoRecord) {
                [void]$matchedBuildInfoPaths.Add($buildInfoRecord.Path)
                $lines += "$($i + 1). $resourceGroupName — model: $($buildInfoRecord.Data.genai_model) — build info: $(Split-Path -Leaf $buildInfoRecord.Path) ✅"
            } else {
                $lines += "$($i + 1). $resourceGroupName — build info file missing ❌"
            }
        }
    }

    $orphanedBuildInfoPaths = @($buildInfoPaths | Where-Object { -not $matchedBuildInfoPaths.Contains($_) })
    if ($orphanedBuildInfoPaths.Count -gt 0) {
        $lines += ''
        $lines += 'Orphaned build info files:'
        foreach ($path in $orphanedBuildInfoPaths) {
            try {
                $buildInfo = Read-BuildInfoFile -Path $path
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

function Read-AiModelSelection {
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
            $currentChoice = Read-AiModelSelection -Choices $remainingChoices.ToArray()
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

    $suffixPath = Get-BuildInfoPathForSuffix -Suffix (Get-FoundryDeploymentSuffixFromResourceGroupName -ResourceGroupName $ResourceGroupName)
    if (Test-Path -LiteralPath $suffixPath) {
        return $suffixPath
    }

    foreach ($path in Get-BuildInfoPaths) {
        try {
            $buildInfo = Read-BuildInfoFile -Path $path
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
        Data = Read-BuildInfoFile -Path $path
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
        [string]$CurrentUserAccount
    )

    $resourceGroup = Get-AzResourceGroup -Name $ResourceGroupName -ErrorAction SilentlyContinue
    if (-not $resourceGroup) {
        throw "Resource group '$ResourceGroupName' was not found in subscription '$SubscriptionId'."
    }

    $buildInfoRecord = Get-BuildInfoRecordForResourceGroup -ResourceGroupName $ResourceGroupName
    if (-not $buildInfoRecord) {
        throw "No build_info-<suffix>.json file was found for '$ResourceGroupName'."
    }

    $buildInfo = $buildInfoRecord.Data
    $buildInfoFileName = Split-Path -Leaf $buildInfoRecord.Path

    $storageExists = [bool](Get-AzStorageAccount -ResourceGroupName $ResourceGroupName -Name $buildInfo.storage_account -ErrorAction SilentlyContinue)
    $keyVaultExists = [bool](Get-AzResource -ResourceGroupName $ResourceGroupName -ResourceType 'Microsoft.KeyVault/vaults' -Name $buildInfo.key_vault -ErrorAction SilentlyContinue)
    $appInsightsExists = [bool](Get-AzResource -ResourceGroupName $ResourceGroupName -ResourceType 'Microsoft.Insights/components' -Name $buildInfo.appinsights -ErrorAction SilentlyContinue)
    $foundryExists = [bool](Get-AzResource -ResourceGroupName $ResourceGroupName -ResourceType 'Microsoft.CognitiveServices/accounts' -Name $buildInfo.foundry_name -ErrorAction SilentlyContinue)

    $projectExists = $false
    $projectResourceId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName/providers/Microsoft.CognitiveServices/accounts/$($buildInfo.foundry_name)/projects/$($buildInfo.foundry_project_name)"
    az resource show --ids $projectResourceId --api-version 2025-06-01 --only-show-errors --output none 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $projectExists = $true
    }

    $appInsightsConnectionStatus = 'App Insights connection not found ❌'
    if ($foundryExists -and $projectExists) {
        $connectionId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName/providers/Microsoft.CognitiveServices/accounts/$($buildInfo.foundry_name)/projects/$($buildInfo.foundry_project_name)/connections/$($buildInfo.foundry_name)-appinsights"
        $connectionSharedToAll = az resource show `
            --ids $connectionId `
            --api-version 2025-06-01 `
            --query properties.isSharedToAll `
            --output tsv 2>&1

        if ($LASTEXITCODE -eq 0) {
            $appInsightsConnectionStatus = if (($connectionSharedToAll -join '').Trim().ToLowerInvariant() -eq 'true') {
                'Shared to all projects ✅'
            } else {
                'This project only ✅'
            }
        }
    }

    $lawRbacStatus = "Entra group '$GroupDisplayName' not found ❌"
    $appInsightsAccessStatus = "Reader missing on resource group ❌"
    $userStatus = "$CurrentUserAccount membership could not be checked ❌"
    $group = Get-MgGroup -Filter "displayName eq '$GroupDisplayName'" -ErrorAction SilentlyContinue
    if ($group) {
        $statusUserId = Get-GraphUserObjectId -Account $CurrentUserAccount
        if ($statusUserId) {
            $isMember = Test-GraphGroupMembership -GroupId $group.Id -DirectoryObjectId $statusUserId
            $userStatus = if ($isMember) {
                "$CurrentUserAccount added to $GroupDisplayName ✅"
            } else {
                "$CurrentUserAccount is not a member of $GroupDisplayName ❌"
            }
        }

        $resourceGroupScope = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName"
        $rgReaderAssignments = Get-AzRoleAssignment -ObjectId $group.Id -Scope $resourceGroupScope -RoleDefinitionName 'Reader' -ErrorAction SilentlyContinue
        $appInsightsAccessStatus = if ($rgReaderAssignments) {
            'Reader on resource group ✅'
        } else {
            'Reader missing on resource group ❌'
        }

        $lawScope = "/subscriptions/$SecuritySubscriptionId/resourceGroups/Sentinel/providers/Microsoft.OperationalInsights/workspaces/DIBSecCom"
        Set-AzContext -SubscriptionId $SecuritySubscriptionId | Out-Null
        try {
            $lawAssignments = Get-AzRoleAssignment -ObjectId $group.Id -Scope $lawScope -ErrorAction SilentlyContinue
        } finally {
            Set-AzContext -SubscriptionId $SubscriptionId | Out-Null
        }

        $lawRbacStatus = if ($lawAssignments) {
            'Log Analytics Reader on DIBSecCom ✅'
        } else {
            'Log Analytics Reader missing on DIBSecCom ❌'
        }
    }

    Write-BuildStatus `
        -ResourceGroupName $ResourceGroupName `
        -StorageAccountName (if ($storageExists) { "$($buildInfo.storage_account) ✅" } else { "$($buildInfo.storage_account) ❌" }) `
        -KeyVaultName (if ($keyVaultExists) { "$($buildInfo.key_vault) ✅" } else { "$($buildInfo.key_vault) ❌" }) `
        -AppInsightsName (if ($appInsightsExists) { "$($buildInfo.appinsights) ✅" } else { "$($buildInfo.appinsights) ❌" }) `
        -AiFoundryName (if ($foundryExists) { "$($buildInfo.foundry_name) ✅" } else { "$($buildInfo.foundry_name) ❌" }) `
        -AiProjectName (if ($projectExists) { "$($buildInfo.foundry_project_name) ✅" } else { "$($buildInfo.foundry_project_name) ❌" }) `
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
        [string]$GroupObjectId
    )

    $lawScope = "/subscriptions/$SecuritySubscriptionId/resourceGroups/Sentinel/providers/Microsoft.OperationalInsights/workspaces/DIBSecCom"
    Write-Host "Cleaning up LAW RBAC in Security subscription..."
    Set-AzContext -SubscriptionId $SecuritySubscriptionId | Out-Null

    try {
        $lawAssignments = Get-AzRoleAssignment -ObjectId $GroupObjectId -Scope $lawScope -ErrorAction SilentlyContinue
        foreach ($assignment in $lawAssignments) {
            Write-Host "  Removing: $($assignment.RoleDefinitionName) @ $($assignment.Scope)"
            Remove-AzRoleAssignment -ObjectId $GroupObjectId `
                -RoleDefinitionName $assignment.RoleDefinitionName `
                -Scope $assignment.Scope `
                -ErrorAction SilentlyContinue
        }

        $lawDeployments = Get-AzSubscriptionDeployment | Where-Object { $_.DeploymentName -like 'law-rbac*' }
        foreach ($deployment in $lawDeployments) {
            Write-Host "Removing deployment record '$($deployment.DeploymentName)'..."
            Remove-AzSubscriptionDeployment -Name $deployment.DeploymentName -ErrorAction SilentlyContinue
        }
    } finally {
        Set-AzContext -SubscriptionId $WorkloadSubscriptionId | Out-Null
    }

    [pscustomobject]@{
        AssignmentCount = @($lawAssignments).Count
        DeploymentCount = @($lawDeployments).Count
        Status          = @(
            if ($lawAssignments) { "Removed $($lawAssignments.Count) LAW role assignments ✅" } else { "No LAW role assignments found ℹ️" }
            if ($lawDeployments) { "Removed $($lawDeployments.Count) LAW deployment records ✅" } else { "No LAW deployment records found ℹ️" }
        ) -join '; '
    }
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

    foreach ($cog in $cogAccounts) {
        Write-Host "  Purging soft-deleted account '$($cog.Name)'..."
        az cognitiveservices account purge `
            --name $cog.Name `
            --resource-group $ResourceGroupName `
            --location $Location `
            --subscription $SubscriptionId 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to purge Cognitive Services account '$($cog.Name)' after deleting '$ResourceGroupName'."
        }
    }

    [pscustomobject]@{
        ResourceGroupStatus = "Deleted $ResourceGroupName ✅"
        CognitiveServicesStatus = if ($cogAccounts) {
            "Purged $($cogAccounts.Name -join ', ') ✅"
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

    $buildInfoPath = Get-BuildInfoPathForResourceGroup -ResourceGroupName $ResourceGroupName
    if (-not $buildInfoPath) {
        Write-Host "No build_info file found for '$ResourceGroupName'."
        return "No build_info file found for $ResourceGroupName ℹ️"
    }

    Remove-Item -LiteralPath $buildInfoPath -Force
    Write-Host "Removed build info file '$buildInfoPath' for '$ResourceGroupName'."
    "Removed $(Split-Path -Leaf $buildInfoPath) for $ResourceGroupName ✅"
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

$ctx = Get-MgContext
$missingScopes = if ($ctx) {
    $requiredGraphScopes | Where-Object { $_ -notin $ctx.Scopes }
} else {
    $requiredGraphScopes
}

if (-not $ctx -or $missingScopes.Count -gt 0) {
    Write-Host "Connecting to Microsoft Graph..."
    Connect-MgGraph -Scopes $requiredGraphScopes -ContextScope CurrentUser -NoWelcome
    $ctx = Get-MgContext
}

$teamsChatId = $null
$currentUser = $ctx
$userId = Get-GraphUserObjectId -Account $currentUser.Account
$azureSession = $null

if ($UseTeamsChatFlow) {
    if ($currentUser.Account -notmatch '@dibsecurity\.onmicrosoft\.com$') {
        throw "UseTeamsChatFlow requires a Microsoft Graph connection in the dibsecurity.onmicrosoft.com tenant."
    }

    if (-not $userId) {
        throw "Unable to resolve the Microsoft Graph user object for '$($currentUser.Account)'."
    }

    $teamsChat = Get-OrCreate-FoundryTeamsChat -UserId $userId -Topic $TeamsChatTopic
    $teamsChatId = $teamsChat.Id
}

# ── 3. Set Azure subscription context (both Az PowerShell and az CLI) ──
$azureSession = Ensure-AzureSession -SubscriptionId $subscriptionId -ExpectedAccount $(if ($UseTeamsChatFlow) { $currentUser.Account } else { $null })
Write-Host "Subscription set to $($azureSession.SubscriptionName) ($subscriptionId)"
if ($UseTeamsChatFlow) {
    Write-Host "Azure account validated for Teams chat flow: $($azureSession.PowerShellAccount)"
}

# ════════════════════════════════════════════════════════════════
#  CLEANUP MODE
# ════════════════════════════════════════════════════════════════
if ($Cleanup) {
    Write-Host ""
    try {
        if ($UseTeamsChatFlow -and $teamsChatId) {
            $cleanupStartMessage = if ($CleanupResourceGroup) {
                "Microsoft Foundry teardown started for '$CleanupResourceGroup'."
            } else {
                "Microsoft Foundry full teardown started."
            }
            [void](Send-FoundryTeamsChatMessage -ChatId $teamsChatId -Message $cleanupStartMessage)
        }

        $teardownProgressNotifier = $null
        try {
            if ($UseTeamsChatFlow -and $teamsChatId) {
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
                Write-Host "=== TARGETED CLEANUP MODE ==="

                $targetResourceGroup = Get-AzResourceGroup -Name $CleanupResourceGroup -ErrorAction SilentlyContinue
                if (-not $targetResourceGroup) {
                    throw "Resource group '$CleanupResourceGroup' was not found in subscription '$subscriptionId'."
                }

                $sharedAccessPlan = Get-TargetedTeardownSharedAccessPlan `
                    -TargetResourceGroupName $CleanupResourceGroup `
                    -CurrentUserAccount $currentUser.Account

                $group = Get-MgGroup -Filter "displayName eq '$groupDisplayName'" -ErrorAction SilentlyContinue
                if ($group) {
                    $groupObjectId = $group.Id
                    Write-Host "Found Entra group '$groupDisplayName' — ObjectId: $groupObjectId"

                    $resourceGroupScope = "/subscriptions/$subscriptionId/resourceGroups/$CleanupResourceGroup"
                    $rgAssignments = Get-AzRoleAssignment -ObjectId $groupObjectId -Scope $resourceGroupScope -ErrorAction SilentlyContinue
                    foreach ($a in $rgAssignments) {
                        Write-Host "  Removing: $($a.RoleDefinitionName) @ $($a.Scope)"
                        Remove-AzRoleAssignment -ObjectId $groupObjectId `
                            -RoleDefinitionName $a.RoleDefinitionName `
                            -Scope $a.Scope `
                            -ErrorAction SilentlyContinue
                    }
                    $rbacStatus = if ($rgAssignments) {
                        "Removed $($rgAssignments.Count) scoped assignments from $groupDisplayName ✅"
                    } else {
                        "No scoped assignments found for $groupDisplayName ℹ️"
                    }
                } else {
                    $rbacStatus = "Entra group '$groupDisplayName' not found; scoped RBAC cleanup skipped ℹ️"
                }

                $resourceGroupCleanup = Remove-FoundryResourceGroup `
                    -ResourceGroupName $CleanupResourceGroup `
                    -Location $location `
                    -SubscriptionId $subscriptionId

                $targetSuffix = Get-FoundryDeploymentSuffixFromResourceGroupName -ResourceGroupName $CleanupResourceGroup
                $deploymentName = "foundry-ai-env-$targetSuffix"
                $deployment = Get-AzSubscriptionDeployment -Name $deploymentName -ErrorAction SilentlyContinue
                $deploymentRecordStatus = if ($deployment) {
                    Write-Host "Removing deployment record '$deploymentName'..."
                    Remove-AzSubscriptionDeployment -Name $deploymentName -ErrorAction SilentlyContinue
                    "Removed $deploymentName ✅"
                } else {
                    "No deployment record named $deploymentName found ℹ️"
                }
                
                $buildInfoStatus = Remove-BuildInfoForResourceGroup `
                    -ResourceGroupName $CleanupResourceGroup

                if ($group) {
                    if ($sharedAccessPlan.ShouldRetainLawRbac) {
                        $remainingBuildLabel = if ($sharedAccessPlan.RemainingBuildCount -eq 1) { 'build remains' } else { 'builds remain' }
                        $securityStatus = "Preserved LAW RBAC because $($sharedAccessPlan.RemainingBuildCount) managed $($remainingBuildLabel): $($sharedAccessPlan.RemainingBuildNames -join ', ') ℹ️"
                    } else {
                        $lawCleanupResult = Remove-FoundryLawWorkspaceAccess `
                            -SecuritySubscriptionId $securitySubscriptionId `
                            -WorkloadSubscriptionId $subscriptionId `
                            -GroupObjectId $groupObjectId
                        $securityStatus = $lawCleanupResult.Status
                    }

                    if ($sharedAccessPlan.ShouldRetainUserMembership) {
                        if ($sharedAccessPlan.RemainingBuildsForCurrentUserCount -gt 0) {
                            $userStatus = "Preserved $($currentUser.Account) in $groupDisplayName because $($sharedAccessPlan.RemainingBuildsForCurrentUserCount) owned build(s) remain ✅"
                        } else {
                            $unknownBuildNames = $sharedAccessPlan.RemainingUnknownOwnershipBuilds | ForEach-Object { $_.ResourceGroupName }
                            $userStatus = "Preserved $($currentUser.Account) in $groupDisplayName because remaining build ownership is unknown for: $($unknownBuildNames -join ', ') ℹ️"
                        }
                    } else {
                        $isMember = Test-GraphGroupMembership -GroupId $groupObjectId -DirectoryObjectId $userId
                        if ($isMember) {
                            Remove-MgGroupMemberByRef -GroupId $groupObjectId -DirectoryObjectId $userId
                            $userStatus = "Removed $($currentUser.Account) from $groupDisplayName because no owned builds remain ✅"
                        } else {
                            $userStatus = "$($currentUser.Account) was not a member of $groupDisplayName ℹ️"
                        }
                    }
                } else {
                    $securityStatus = "Entra group '$groupDisplayName' not found; shared LAW RBAC cleanup skipped ℹ️"
                    $userStatus = "Entra group '$groupDisplayName' not found ℹ️"
                }

                $teardownStatusLines = Write-TeardownStatus `
                    -ScopeStatus "Targeted teardown for $CleanupResourceGroup" `
                    -ResourceGroupStatus $resourceGroupCleanup.ResourceGroupStatus `
                    -RbacStatus $rbacStatus `
                    -CognitiveServicesStatus $resourceGroupCleanup.CognitiveServicesStatus `
                    -DeploymentRecordStatus $deploymentRecordStatus `
                    -BuildInfoStatus $buildInfoStatus `
                    -SecurityStatus $securityStatus `
                    -UserStatus $userStatus
            } else {
                Write-Host "=== CLEANUP MODE ==="

                $group = Get-MgGroup -Filter "displayName eq '$groupDisplayName'" -ErrorAction SilentlyContinue
                if ($group) {
                    $groupObjectId = $group.Id
                    Write-Host "Found Entra group '$groupDisplayName' — ObjectId: $groupObjectId"

                    Write-Host "Removing RBAC role assignments for '$groupDisplayName'..."
                    $assignments = Get-AzRoleAssignment -ObjectId $groupObjectId -ErrorAction SilentlyContinue
                    foreach ($a in $assignments) {
                        Write-Host "  Removing: $($a.RoleDefinitionName) @ $($a.Scope)"
                        Remove-AzRoleAssignment -ObjectId $groupObjectId `
                            -RoleDefinitionName $a.RoleDefinitionName `
                            -Scope $a.Scope `
                            -ErrorAction SilentlyContinue
                    }
                    $rbacStatus = if ($assignments) {
                        "Removed $($assignments.Count) role assignments for $groupDisplayName ✅"
                    } else {
                        "No role assignments found for $groupDisplayName ℹ️"
                    }

                    $isMember = Test-GraphGroupMembership -GroupId $groupObjectId -DirectoryObjectId $userId
                    if ($isMember) {
                        Remove-MgGroupMemberByRef -GroupId $groupObjectId -DirectoryObjectId $userId
                        $userStatus = "Removed $($currentUser.Account) from $groupDisplayName ✅"
                    } else {
                        $userStatus = "$($currentUser.Account) was not a member of $groupDisplayName ℹ️"
                    }
                } else {
                    $rbacStatus = "Entra group '$groupDisplayName' not found; RBAC cleanup skipped ℹ️"
                    $userStatus = "Entra group '$groupDisplayName' not found ℹ️"
                }

                $rgList = Get-AzResourceGroup | Where-Object { $_.ResourceGroupName -match '^zolab-ai-.{4,}$' }
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
                        -GroupObjectId $groupObjectId
                    $securityStatus = $lawCleanupResult.Status
                }

                $buildInfoPaths = Get-BuildInfoPaths
                $buildInfoStatus = if ($buildInfoPaths) {
                    foreach ($path in $buildInfoPaths) {
                        Remove-Item -LiteralPath $path -Force
                        Write-Host "Removed stale build info file '$path'."
                    }
                    "Removed $($buildInfoPaths.Count) build info file(s) ✅"
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
                "Account: $($currentUser.Account)"
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
        $buildListLines = Get-FoundryBuildListLines -SubscriptionId $subscriptionId
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
        $buildStatusLines = Get-FoundryBuildStatusLines `
            -ResourceGroupName $BuildStatusResourceGroup `
            -GroupDisplayName $groupDisplayName `
            -SubscriptionId $subscriptionId `
            -SecuritySubscriptionId $securitySubscriptionId `
            -CurrentUserAccount $currentUser.Account

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
    if ($UseTeamsChatFlow -and $currentUser.Account -notmatch '@dibsecurity\.onmicrosoft\.com$') {
        throw "UseTeamsChatFlow requires a Microsoft Graph connection in the dibsecurity.onmicrosoft.com tenant."
    }

    $userId = Get-GraphUserObjectId -Account $currentUser.Account
    if (-not $userId) {
        throw "Unable to resolve the Microsoft Graph user object for '$($currentUser.Account)'."
    }

    $isMember = Test-GraphGroupMembership -GroupId $groupObjectId -DirectoryObjectId $userId
    if ($isMember) {
        Write-Host "Current user ($($currentUser.Account)) is already a member of '$groupDisplayName'"
    } else {
        New-MgGroupMember -GroupId $groupObjectId -DirectoryObjectId $userId
        Write-Host "Added current user ($($currentUser.Account)) to '$groupDisplayName'"
    }

    if ($UseTeamsChatFlow) {
        $teamsChat = Get-OrCreate-FoundryTeamsChat -UserId $userId -Topic $TeamsChatTopic
        $teamsChatId = $teamsChat.Id
        Write-Host "Using Teams chat '$TeamsChatTopic' ($teamsChatId) for model selection and build notifications."
        [void](Send-FoundryTeamsChatMessage -ChatId $teamsChatId -Message (
            @(
                "Microsoft Foundry deployment started."
                "Account: $($currentUser.Account)"
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
                -Message "🚧 One moment ..the Bob's are still building! 🚧" `
                -TeamsChatHelperPath (Join-Path $PSScriptRoot 'teams-chat.ps1')
        }

        # ── Deploy Bicep (subscription-scoped via az cli) ──
        Write-Host ""
        Write-Host "Deploying AI Foundry environment..."
        $deployOutput = az deployment sub create `
            --location $location `
            --template-file "$PSScriptRoot\main.bicep" `
            --name "foundry-ai-env-$suffix" `
            --parameters `
                aiDevGroupObjectId=$groupObjectId `
                securitySubscriptionId=$securitySubscriptionId `
                suffix=$suffix `
                aiModelDeploymentName=$($selectedAiModelSpec.DeploymentName) `
                aiModelName=$($selectedAiModelSpec.ModelName) `
                aiModelFormat=$($selectedAiModelSpec.ModelFormat) `
                aiModelVersion=$($selectedAiModelSpec.ModelVersion) `
                aiModelSkuName=$($selectedAiModelSpec.SkuName) `
                aiModelSkuCapacity=$($selectedAiModelSpec.SkuCapacity) `
            --output json 2>&1

        $rawOutput = $deployOutput -join "`n"
        $jsonStart = $rawOutput.IndexOf('{')
        $jsonEnd = $rawOutput.LastIndexOf('}')
        if ($jsonStart -lt 0 -or $jsonEnd -le $jsonStart) {
            throw "No JSON found in deployment output.`n$rawOutput"
        }

        if ($LASTEXITCODE -ne 0) {
            throw "Deployment failed.`n$rawOutput"
        }

        $jsonString = $rawOutput.Substring($jsonStart, $jsonEnd - $jsonStart + 1)
        $result = $jsonString | ConvertFrom-Json

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

        $lawExitCode = $LASTEXITCODE
        $lawWarnings = $lawOutput | Where-Object { $_ -match '^WARNING:|^BCP\d|\.bicep\(' }
        if ($lawWarnings) {
            $lawWarnings | ForEach-Object { Write-Host $_ }
        }

        az account set --subscription $subscriptionId 2>&1 | Out-Null

        if ($lawExitCode -ne 0) {
            throw "LAW RBAC deployment failed.`n$($lawOutput -join "`n")"
        }

        Write-Host "  Log Analytics Reader assigned to '$groupDisplayName' on DIBSecCom workspace."

        $connectionId = "/subscriptions/$subscriptionId/resourceGroups/$($result.properties.outputs.resourceGroupName.value)/providers/Microsoft.CognitiveServices/accounts/$($result.properties.outputs.aiFoundryName.value)/projects/$($result.properties.outputs.aiProjectName.value)/connections/$($result.properties.outputs.aiFoundryName.value)-appinsights"
        $connectionSharedToAll = az resource show `
            --ids $connectionId `
            --api-version 2025-06-01 `
            --query properties.isSharedToAll `
            --output tsv 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to resolve App Insights connection scope.`n$($connectionSharedToAll -join "`n")"
        }

        $appInsightsConnectionStatus = if (($connectionSharedToAll -join '').Trim().ToLowerInvariant() -eq 'true') {
            'Shared to all projects ✅'
        } else {
            'This project only ✅'
        }

        $resourceGroupScope = "/subscriptions/$subscriptionId/resourceGroups/$($result.properties.outputs.resourceGroupName.value)"
        $rgReaderAssignments = Get-AzRoleAssignment -ObjectId $groupObjectId -Scope $resourceGroupScope -RoleDefinitionName 'Reader' -ErrorAction SilentlyContinue
        $appInsightsAccessStatus = if ($rgReaderAssignments) {
            'Reader on resource group ✅'
        } else {
            'Reader missing on resource group ❌'
        }

        $buildInfoPath = Get-BuildInfoPathForSuffix -Suffix $suffix
        Write-BuildInfoJson `
            -OutputPath $buildInfoPath `
            -ResourceGroupName $result.properties.outputs.resourceGroupName.value `
            -AppInsightsName $result.properties.outputs.appInsightsName.value `
            -FoundryProjectEndpoint $result.properties.outputs.foundryProjectEndpoint.value `
            -AzureOpenAIEndpoint $result.properties.outputs.azureOpenAIEndpoint.value `
            -StorageAccountName $result.properties.outputs.storageAccountName.value `
            -KeyVaultName $result.properties.outputs.keyVaultName.value `
            -GenAiModel $selectedAiModelSpec.DeploymentName `
            -AiFoundryName $result.properties.outputs.aiFoundryName.value `
            -AiProjectName $result.properties.outputs.aiProjectName.value `
            -RequestedBy $currentUser.Account
        Write-Host "📝 Build info written to $buildInfoPath"

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
            -LawRbacStatus 'Log Analytics Reader on DIBSecCom ✅' `
            -UserStatus "$($currentUser.Account) added to $groupDisplayName ✅"
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
                "Account: $($currentUser.Account)"
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
