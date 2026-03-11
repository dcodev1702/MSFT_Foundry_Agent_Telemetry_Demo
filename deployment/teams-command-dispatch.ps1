# Listen for Teams chat commands and dispatch Foundry build or teardown actions.
# Usage:
#   pwsh ./teams-command-dispatch.ps1

param(
    [string]$TeamsChatTopic = 'Microsoft Foundry Deployments',
    [int]$CommandTimeoutMinutes = 60,
    [int]$ConfirmationTimeoutMinutes = 10,
    [int]$HeartbeatIntervalMinutes = 30,
    [int]$PollIntervalSeconds = 10
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'teams-chat.ps1')

$deployScript = Join-Path $PSScriptRoot 'deploy-foundry-env.ps1'
$subscriptionId = (Get-AzSubscription -SubscriptionName "zolab").Id
$listenerStartTime = Get-Date
$listenerStartTimeUtc = $listenerStartTime.ToUniversalTime()
$listenerScriptName = Split-Path -Leaf $PSCommandPath

if (-not (Get-Module -ListAvailable -Name Microsoft.Graph.Teams)) {
    Write-Host "Installing Microsoft.Graph.Teams module..."
    Install-Module Microsoft.Graph.Teams -Scope CurrentUser -Force
}

Import-Module Microsoft.Graph.Authentication
Import-Module Microsoft.Graph.Teams

$requiredGraphScopes = @(
    'User.Read'
    'Chat.Create'
    'Chat.ReadWrite'
    'ChatMessage.Send'
)

$ctx = Connect-FoundryGraphIfNeeded -Scopes $requiredGraphScopes

if ($ctx.Account -notmatch '@dibsecurity\.onmicrosoft\.com$') {
    throw "Teams command dispatch requires a Microsoft Graph connection in the dibsecurity.onmicrosoft.com tenant."
}

Set-AzContext -SubscriptionId $subscriptionId | Out-Null

$user = Get-MgUser -UserId $ctx.Account
$chat = Get-OrCreate-FoundryTeamsChat -UserId $user.Id -Topic $TeamsChatTopic

function Get-ListenerAzureContextSummary {
    $azContext = Get-AzContext -ErrorAction SilentlyContinue
    $cliContextJson = & az account show --query "{account:user.name,subscriptionId:id}" --output json 2>$null
    $cliContext = if ($LASTEXITCODE -eq 0 -and $cliContextJson) {
        $cliContextJson | ConvertFrom-Json
    } else {
        $null
    }

    [pscustomobject]@{
        PowerShellAccount = if ($azContext -and $azContext.Account -and $azContext.Account.Id) {
            $azContext.Account.Id
        } else {
            'Unavailable ❌'
        }
        CliAccount = if ($cliContext -and $cliContext.account) {
            $cliContext.account
        } else {
            'Unavailable ❌'
        }
        SubscriptionId = if ($cliContext -and $cliContext.subscriptionId) {
            $cliContext.subscriptionId
        } elseif ($azContext -and $azContext.Subscription -and $azContext.Subscription.Id) {
            $azContext.Subscription.Id
        } else {
            'Unavailable ❌'
        }
    }
}

function Get-ListenerHelpLines {
    @(
        'Foundry Teams command listener is online.'
        'Available commands:'
        '- build it'
        '- heartbeat'
        '- list builds'
        '- build status ''zolab-ai-xxxxxx'''
        '- teardown'
        '- teardown ''zolab-ai-xxxxxx'''
        '- listener status'
        '- ?'
        '- stop listener'
        ''
        'I will ask for confirmation before build, build status, and teardown actions.'
    )
}

function Get-ListenerStatusLines {
    param(
        [Parameter(Mandatory)]
        [string]$Account,

        [Parameter(Mandatory)]
        [string]$AzurePowerShellAccount,

        [Parameter(Mandatory)]
        [string]$AzureCliAccount,

        [Parameter(Mandatory)]
        [string]$SubscriptionId,

        [Parameter(Mandatory)]
        [string]$ChatTopic,

        [Parameter(Mandatory)]
        [int]$CommandTimeoutMinutes,

        [Parameter(Mandatory)]
        [int]$ConfirmationTimeoutMinutes,

        [Parameter(Mandatory)]
        [int]$PollIntervalSeconds
    )

    @(
        '● 🛰️ Listener status'
        ''
        "Status: Online ✅"
        "Account: $Account"
        "Azure PowerShell account: $AzurePowerShellAccount"
        "Azure CLI account: $AzureCliAccount"
        "Subscription: $SubscriptionId"
        "Chat topic: $ChatTopic"
        "Process ID: $PID"
        "Command timeout: $CommandTimeoutMinutes minute(s)"
        "Confirmation timeout: $ConfirmationTimeoutMinutes minute(s)"
        "Poll interval: $PollIntervalSeconds second(s)"
        "Checked at: $((Get-Date).ToUniversalTime().ToString('u'))"
    )
}

function Format-ListenerDuration {
    param(
        [Parameter(Mandatory)]
        [TimeSpan]$Duration
    )

    $parts = @()
    if ($Duration.Days -gt 0) {
        $parts += "$($Duration.Days)d"
    }

    if ($Duration.Hours -gt 0 -or $parts.Count -gt 0) {
        $parts += "$($Duration.Hours)h"
    }

    if ($Duration.Minutes -gt 0 -or $parts.Count -gt 0) {
        $parts += "$($Duration.Minutes)m"
    }

    $parts += "$($Duration.Seconds)s"
    $parts -join ' '
}

function Get-ListenerHeartbeatLines {
    param(
        [Parameter(Mandatory)]
        [string]$Account,

        [Parameter(Mandatory)]
        [string]$ChatTopic
    )

    $process = Get-Process -Id $PID -ErrorAction SilentlyContinue
    $uptime = if ($process) {
        Format-ListenerDuration -Duration ((Get-Date) - $process.StartTime)
    } else {
        Format-ListenerDuration -Duration ((Get-Date) - $listenerStartTime)
    }

    $memoryUsage = if ($process) {
        ('{0:N1} MB' -f ($process.WorkingSet64 / 1MB))
    } else {
        'Unavailable'
    }

    $graphCtx = Get-MgContext
    $missingGraphScopes = if ($graphCtx) {
        $requiredGraphScopes | Where-Object { $_ -notin $graphCtx.Scopes }
    } else {
        $requiredGraphScopes
    }
    $graphState = if ($graphCtx -and
        $graphCtx.Account -ieq $Account -and
        $missingGraphScopes.Count -eq 0) {
        'Connected'
    } else {
        'Disconnected'
    }

    $lastResponse = if ($global:FoundryTeamsLastResponse) {
        $global:FoundryTeamsLastResponse.SentAtUtc.ToString('u')
    } else {
        'No Teams response has been sent yet.'
    }

    @(
        "🟢 Status: Online ✅"
        "📜 Script: $listenerScriptName"
        "🆔 PID: $PID"
        "🖥️ pwsh version: $($PSVersionTable.PSVersion)"
        "⏱️ Uptime: $uptime"
        "🧠 Memory: $memoryUsage"
        "💬 Last response: $lastResponse"
        "🔗 Graph API: $graphState 🔌"
        "📢 Listening in: $ChatTopic"
        "👤 Identity: $Account"
        "🕒 Checked at: $((Get-Date).ToUniversalTime().ToString('u'))"
    )
}

function Get-ListenerManagedTeardownTargets {
    param(
        [Parameter(Mandatory)]
        [string]$SubscriptionId
    )

    Set-AzContext -SubscriptionId $SubscriptionId | Out-Null
    @(Get-AzResourceGroup |
        Where-Object { $_.ResourceGroupName -match '^zolab-ai-.{4,}$' } |
        Sort-Object ResourceGroupName)
}

function Request-ListenerTeardownTargetSelection {
    param(
        [Parameter(Mandatory)]
        [string]$ChatId,

        [Parameter(Mandatory)]
        [string]$SubscriptionId,

        [Parameter(Mandatory)]
        [int]$TimeoutMinutes,

        [Parameter(Mandatory)]
        [int]$PollIntervalSeconds
    )

    $targets = @(Get-ListenerManagedTeardownTargets -SubscriptionId $SubscriptionId)
    if (-not $targets) {
        return [pscustomobject]@{
            Outcome           = 'none-available'
            ResourceGroupName = $null
        }
    }

    $allowedChoices = @($targets | ForEach-Object { $_.ResourceGroupName }) + 'none'
    $promptLines = @(
        'Teardown selection requested.'
        'Reply with:'
    )

    for ($i = 0; $i -lt $targets.Count; $i++) {
        $promptLines += "$($i + 1). $($targets[$i].ResourceGroupName)"
    }

    $promptLines += "$($targets.Count + 1). none"
    $promptLines += ''
    $promptLines += 'You can reply with the menu number or the resource group name.'
    $promptLines += "This selection expires in $TimeoutMinutes minutes."

    $selectionPrompt = Send-FoundryTeamsChatMessage -ChatId $ChatId -Message ($promptLines -join "`n")

    try {
        $selection = Wait-FoundryTeamsChatChoice `
            -ChatId $ChatId `
            -AllowedChoices $allowedChoices `
            -PromptCreatedDateTime $selectionPrompt.CreatedDateTime `
            -PromptMessageId $selectionPrompt.Id `
            -TimeoutMinutes $TimeoutMinutes `
            -PollIntervalSeconds $PollIntervalSeconds
    } catch {
        if ($_.Exception.Message -like 'Timed out waiting for a Teams response*') {
            return [pscustomobject]@{
                Outcome           = 'timed-out'
                ResourceGroupName = $null
            }
        }

        throw
    }

    if ($selection.Choice -eq 'none') {
        return [pscustomobject]@{
            Outcome           = 'aborted'
            ResourceGroupName = $null
        }
    }

    return [pscustomobject]@{
        Outcome           = 'selected'
        ResourceGroupName = $selection.Choice
    }
}

try {
    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message ((Get-ListenerHelpLines) -join "`n"))
    Write-Host "Listener online. PID=$PID Account=$($ctx.Account) Topic=$TeamsChatTopic UTC=$((Get-Date).ToUniversalTime().ToString('u'))"
    $nextAutomaticHeartbeatUtc = $listenerStartTimeUtc.AddMinutes($HeartbeatIntervalMinutes)

    while ($true) {
        $commandWaitStartedAt = (Get-Date).ToUniversalTime()
        $commandWaitDeadlineUtc = $commandWaitStartedAt.AddMinutes($CommandTimeoutMinutes)
        if ($HeartbeatIntervalMinutes -gt 0 -and $nextAutomaticHeartbeatUtc -lt $commandWaitDeadlineUtc) {
            $commandWaitDeadlineUtc = $nextAutomaticHeartbeatUtc
        }

        try {
            $command = Wait-FoundryTeamsChatCommand `
                -ChatId $chat.Id `
                -PromptCreatedDateTime $commandWaitStartedAt `
                -DeadlineAt $commandWaitDeadlineUtc `
                -TimeoutMinutes $CommandTimeoutMinutes `
                -PollIntervalSeconds $PollIntervalSeconds
        } catch {
            if ($_.Exception.Message -like 'Timed out waiting for a Teams command*') {
                $currentUtc = (Get-Date).ToUniversalTime()
                if ($HeartbeatIntervalMinutes -gt 0 -and $currentUtc -ge $nextAutomaticHeartbeatUtc) {
                    $heartbeatLines = Get-ListenerHeartbeatLines `
                        -Account $ctx.Account `
                        -ChatTopic $TeamsChatTopic
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message ($heartbeatLines -join "`n"))
                    Write-Host "Automatic heartbeat sent."
                    do {
                        $nextAutomaticHeartbeatUtc = $nextAutomaticHeartbeatUtc.AddMinutes($HeartbeatIntervalMinutes)
                    } while ($nextAutomaticHeartbeatUtc -le (Get-Date).ToUniversalTime())
                    continue
                }

                Write-Host "No Teams command received during the last wait window. Continuing to listen..."
                continue
            }

            throw
        }

        switch ($command.CommandType) {
            'help' {
                [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message ((Get-ListenerHelpLines) -join "`n"))
                Write-Host "Help command received."
                continue
            }

            'listener-status' {
                $azureSummary = Get-ListenerAzureContextSummary
                $statusLines = Get-ListenerStatusLines `
                    -Account $ctx.Account `
                    -AzurePowerShellAccount $azureSummary.PowerShellAccount `
                    -AzureCliAccount $azureSummary.CliAccount `
                    -SubscriptionId $azureSummary.SubscriptionId `
                    -ChatTopic $TeamsChatTopic `
                    -CommandTimeoutMinutes $CommandTimeoutMinutes `
                    -ConfirmationTimeoutMinutes $ConfirmationTimeoutMinutes `
                    -PollIntervalSeconds $PollIntervalSeconds
                [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message ($statusLines -join "`n"))
                Write-Host "Listener status command received."
                continue
            }

            'heartbeat' {
                $heartbeatLines = Get-ListenerHeartbeatLines `
                    -Account $ctx.Account `
                    -ChatTopic $TeamsChatTopic
                [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message ($heartbeatLines -join "`n"))
                Write-Host "Heartbeat command received."
                continue
            }

            'list-builds' {
                [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message 'Listing managed Foundry builds...')
                Write-Host "List builds command received."
                try {
                    & $deployScript -ListBuilds -UseTeamsChatFlow -TeamsChatTopic $TeamsChatTopic
                } catch {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message (
                        @(
                            "List builds command failed."
                            ""
                            $_.Exception.Message
                            ""
                            "Listener is still online."
                        ) -join "`n"
                    ))
                    Write-Warning "List builds command failed: $($_.Exception.Message)"
                }

                continue
            }

            'stop-listener' {
                [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Foundry Teams command listener is stopping now.")
                Write-Host "Stop listener command received."
                return
            }

            'build' {
                $confirmationPrompt = Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message (
                    @(
                        "Build request received."
                        "Reply with:"
                        "1. build"
                        "2. abort"
                        ""
                        "This confirmation expires in $ConfirmationTimeoutMinutes minutes."
                    ) -join "`n"
                )

                try {
                    $confirmation = Wait-FoundryTeamsChatChoice `
                        -ChatId $chat.Id `
                        -AllowedChoices @('build', 'abort') `
                        -PromptCreatedDateTime $confirmationPrompt.CreatedDateTime `
                        -PromptMessageId $confirmationPrompt.Id `
                        -TimeoutMinutes $ConfirmationTimeoutMinutes `
                        -PollIntervalSeconds $PollIntervalSeconds
                } catch {
                    if ($_.Exception.Message -like 'Timed out waiting for a Teams response*') {
                        [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Build confirmation timed out. Listener is still online.")
                        Write-Host "Build confirmation timed out."
                        continue
                    }

                    throw
                }

                if ($confirmation.Choice -eq 'abort') {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Build request aborted. Listener is still online.")
                    Write-Host "Build request aborted."
                    continue
                }

                [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Build confirmed. Starting deployment...")
                Write-Host "Build confirmed. Starting deployment..."
                try {
                    & $deployScript -UseTeamsChatFlow -TeamsChatTopic $TeamsChatTopic
                } catch {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message (
                        @(
                            "Build command failed."
                            ""
                            $_.Exception.Message
                            ""
                            "Listener is still online."
                        ) -join "`n"
                    ))
                    Write-Warning "Build command failed: $($_.Exception.Message)"
                }

                continue
            }

            'build-status' {
                $confirmationPrompt = Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message (
                    @(
                        "Build status request received for '$($command.ResourceGroupName)'."
                        "Reply with:"
                        "1. confirm"
                        "2. abort"
                        ""
                        "This confirmation expires in $ConfirmationTimeoutMinutes minutes."
                    ) -join "`n"
                )

                try {
                    $confirmation = Wait-FoundryTeamsChatChoice `
                        -ChatId $chat.Id `
                        -AllowedChoices @('confirm', 'abort') `
                        -PromptCreatedDateTime $confirmationPrompt.CreatedDateTime `
                        -PromptMessageId $confirmationPrompt.Id `
                        -TimeoutMinutes $ConfirmationTimeoutMinutes `
                        -PollIntervalSeconds $PollIntervalSeconds
                } catch {
                    if ($_.Exception.Message -like 'Timed out waiting for a Teams response*') {
                        [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Build status confirmation timed out. Listener is still online.")
                        Write-Host "Build status confirmation timed out."
                        continue
                    }

                    throw
                }

                if ($confirmation.Choice -eq 'abort') {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Build status request for '$($command.ResourceGroupName)' aborted. Listener is still online.")
                    Write-Host "Build status request aborted."
                    continue
                }

                [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Build status confirmed for '$($command.ResourceGroupName)'. Gathering deployment status...")
                Write-Host "Build status confirmed for '$($command.ResourceGroupName)'."
                try {
                    & $deployScript -BuildStatusResourceGroup $command.ResourceGroupName -UseTeamsChatFlow -TeamsChatTopic $TeamsChatTopic
                } catch {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message (
                        @(
                            "Build status command failed for '$($command.ResourceGroupName)'."
                            ""
                            $_.Exception.Message
                            ""
                            "Listener is still online."
                        ) -join "`n"
                    ))
                    Write-Warning "Build status command failed: $($_.Exception.Message)"
                }

                continue
            }

            'teardown' {
                $selectedResourceGroupName = $command.ResourceGroupName
                if ([string]::IsNullOrWhiteSpace($selectedResourceGroupName)) {
                    $selectionResult = Request-ListenerTeardownTargetSelection `
                        -ChatId $chat.Id `
                        -SubscriptionId $subscriptionId `
                        -TimeoutMinutes $ConfirmationTimeoutMinutes `
                        -PollIntervalSeconds $PollIntervalSeconds

                    if ($selectionResult.Outcome -eq 'none-available') {
                        [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message 'No managed Foundry builds are currently available for teardown. Listener is still online.')
                        Write-Host 'No managed Foundry builds are currently available for teardown.'
                        continue
                    }

                    if ($selectionResult.Outcome -eq 'timed-out') {
                        [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message 'Teardown selection timed out. Listener is still online.')
                        Write-Host 'Teardown selection timed out.'
                        continue
                    }

                    if ($selectionResult.Outcome -eq 'aborted') {
                        [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message 'Teardown selection canceled. Listener is still online.')
                        Write-Host 'Teardown selection canceled.'
                        continue
                    }

                    if ($selectionResult.Outcome -ne 'selected' -or [string]::IsNullOrWhiteSpace($selectionResult.ResourceGroupName)) {
                        [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message 'Teardown selection did not resolve to a valid build. Listener is still online.')
                        Write-Host 'Teardown selection did not resolve to a valid build.'
                        continue
                    }

                    $selectedResourceGroupName = $selectionResult.ResourceGroupName
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Teardown target selected: '$selectedResourceGroupName'.")
                    Write-Host "Teardown target selected: '$selectedResourceGroupName'."
                }

                Set-AzContext -SubscriptionId $subscriptionId | Out-Null
                $targetResourceGroup = Get-AzResourceGroup -Name $selectedResourceGroupName -ErrorAction SilentlyContinue
                if (-not $targetResourceGroup) {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Resource group '$selectedResourceGroupName' was not found in the zolab subscription. Send another command.")
                    Write-Host "Resource group '$selectedResourceGroupName' was not found. Waiting for another command."
                    continue
                }

                if ($targetResourceGroup.ResourceGroupName -notmatch '^zolab-ai-.{4,}$') {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Resource group '$($targetResourceGroup.ResourceGroupName)' is outside the managed Foundry naming pattern. Send another command.")
                    Write-Host "Resource group '$($targetResourceGroup.ResourceGroupName)' is outside the managed Foundry naming pattern."
                    continue
                }

                $confirmationPrompt = Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message (
                    @(
                        "Teardown request received for '$($targetResourceGroup.ResourceGroupName)'."
                        "Reply with:"
                        "1. confirm teardown"
                        "2. abort"
                        ""
                        "This confirmation expires in $ConfirmationTimeoutMinutes minutes."
                    ) -join "`n"
                )

                try {
                    $confirmation = Wait-FoundryTeamsChatChoice `
                        -ChatId $chat.Id `
                        -AllowedChoices @('confirm teardown', 'abort') `
                        -PromptCreatedDateTime $confirmationPrompt.CreatedDateTime `
                        -PromptMessageId $confirmationPrompt.Id `
                        -TimeoutMinutes $ConfirmationTimeoutMinutes `
                        -PollIntervalSeconds $PollIntervalSeconds
                } catch {
                    if ($_.Exception.Message -like 'Timed out waiting for a Teams response*') {
                        [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Teardown confirmation timed out. Listener is still online.")
                        Write-Host "Teardown confirmation timed out."
                        continue
                    }

                    throw
                }

                if ($confirmation.Choice -eq 'abort') {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Teardown request for '$($targetResourceGroup.ResourceGroupName)' aborted. Listener is still online.")
                    Write-Host "Teardown request aborted."
                    continue
                }

                try {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Running teardown preview for '$($targetResourceGroup.ResourceGroupName)' before any changes are made...")
                    Write-Host "Running teardown preview for '$($targetResourceGroup.ResourceGroupName)'."

                    & $deployScript -Cleanup -CleanupResourceGroup $targetResourceGroup.ResourceGroupName -PreviewCleanup -UseTeamsChatFlow -TeamsChatTopic $TeamsChatTopic
                } catch {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message (
                        @(
                            "Teardown preview failed for '$($targetResourceGroup.ResourceGroupName)'."
                            ""
                            $_.Exception.Message
                            ""
                            "Listener is still online."
                        ) -join "`n"
                    ))
                    Write-Warning "Teardown preview failed: $($_.Exception.Message)"
                    continue
                }

                $finalConfirmationPrompt = Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message (
                    @(
                        "Preview finished for '$($targetResourceGroup.ResourceGroupName)'."
                        "Reply with:"
                        "1. proceed with teardown"
                        "2. abort"
                        ""
                        "This confirmation expires in $ConfirmationTimeoutMinutes minutes."
                    ) -join "`n"
                )

                try {
                    $finalConfirmation = Wait-FoundryTeamsChatChoice `
                        -ChatId $chat.Id `
                        -AllowedChoices @('proceed with teardown', 'abort') `
                        -PromptCreatedDateTime $finalConfirmationPrompt.CreatedDateTime `
                        -PromptMessageId $finalConfirmationPrompt.Id `
                        -TimeoutMinutes $ConfirmationTimeoutMinutes `
                        -PollIntervalSeconds $PollIntervalSeconds
                } catch {
                    if ($_.Exception.Message -like 'Timed out waiting for a Teams response*') {
                        [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Teardown execution confirmation timed out. Listener is still online.")
                        Write-Host "Teardown execution confirmation timed out."
                        continue
                    }

                    throw
                }

                if ($finalConfirmation.Choice -eq 'abort') {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Teardown request for '$($targetResourceGroup.ResourceGroupName)' aborted after preview. Listener is still online.")
                    Write-Host "Teardown request aborted after preview."
                    continue
                }

                [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Teardown confirmed for '$($targetResourceGroup.ResourceGroupName)'. Starting cleanup...")
                Write-Host "Teardown confirmed for '$($targetResourceGroup.ResourceGroupName)'. Starting cleanup..."
                try {
                    & $deployScript -Cleanup -CleanupResourceGroup $targetResourceGroup.ResourceGroupName -UseTeamsChatFlow -TeamsChatTopic $TeamsChatTopic
                } catch {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message (
                        @(
                            "Teardown command failed for '$($targetResourceGroup.ResourceGroupName)'."
                            ""
                            $_.Exception.Message
                            ""
                            "Listener is still online."
                        ) -join "`n"
                    ))
                    Write-Warning "Teardown command failed: $($_.Exception.Message)"
                }

                continue
            }
        }
    }
} catch {
    if ($chat) {
        [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message (
            @(
                "Foundry Teams command processing failed."
                ""
                $_.Exception.Message
            ) -join "`n"
        ))
    }

    throw
}
