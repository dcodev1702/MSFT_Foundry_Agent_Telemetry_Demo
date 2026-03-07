# Listen for Teams chat commands and dispatch Foundry build or teardown actions.
# Usage:
#   .\teams-command-dispatch.ps1

param(
    [string]$TeamsChatTopic = 'Microsoft Foundry Deployments',
    [int]$CommandTimeoutMinutes = 60,
    [int]$ConfirmationTimeoutMinutes = 30,
    [int]$PollIntervalSeconds = 10
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'teams-chat.ps1')

$deployScript = Join-Path $PSScriptRoot 'deploy-foundry-env.ps1'
$subscriptionId = (Get-AzSubscription -SubscriptionName "zolab").Id

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

$ctx = Get-MgContext
$missingScopes = if ($ctx) {
    $requiredGraphScopes | Where-Object { $_ -notin $ctx.Scopes }
} else {
    $requiredGraphScopes
}

if (-not $ctx -or $missingScopes.Count -gt 0) {
    Write-Host "Connecting to Microsoft Graph..."
    Connect-MgGraph -Scopes $requiredGraphScopes -NoWelcome | Out-Null
    $ctx = Get-MgContext
}

if ($ctx.Account -notmatch '@dibsecurity\.onmicrosoft\.com$') {
    throw "Teams command dispatch requires a Microsoft Graph connection in the dibsecurity.onmicrosoft.com tenant."
}

Set-AzContext -SubscriptionId $subscriptionId | Out-Null

$user = Get-MgUser -UserId $ctx.Account
$chat = Get-OrCreate-FoundryTeamsChat -UserId $user.Id -Topic $TeamsChatTopic

try {
    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message (
        @(
            "Foundry Teams command listener is online."
            "Supported commands:"
            "- build it"
            "- teardown 'zolab-ai-xxxxxx'"
            ""
            "I'll ask for a 1/2 confirmation before I do anything."
        ) -join "`n"
    ))

    while ($true) {
        $commandPrompt = Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message (
            @(
                "Waiting for the next command."
                "Send 'build it' to start a fresh deployment or 'teardown <resource-group>' to remove a deployment."
            ) -join "`n"
        )

        $command = Wait-FoundryTeamsChatCommand `
            -ChatId $chat.Id `
            -PromptCreatedDateTime $commandPrompt.CreatedDateTime `
            -PromptMessageId $commandPrompt.Id `
            -TimeoutMinutes $CommandTimeoutMinutes `
            -PollIntervalSeconds $PollIntervalSeconds

        switch ($command.CommandType) {
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

                $confirmation = Wait-FoundryTeamsChatChoice `
                    -ChatId $chat.Id `
                    -AllowedChoices @('build', 'abort') `
                    -PromptCreatedDateTime $confirmationPrompt.CreatedDateTime `
                    -PromptMessageId $confirmationPrompt.Id `
                    -TimeoutMinutes $ConfirmationTimeoutMinutes `
                    -PollIntervalSeconds $PollIntervalSeconds

                if ($confirmation.Choice -eq 'abort') {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Build request aborted.")
                    Write-Host "Build request aborted."
                    return
                }

                [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Build confirmed. Starting deployment...")
                Write-Host "Build confirmed. Starting deployment..."
                & $deployScript -UseTeamsChatFlow -TeamsChatTopic $TeamsChatTopic
                return
            }

            'teardown' {
                $targetResourceGroup = Get-AzResourceGroup -Name $command.ResourceGroupName -ErrorAction SilentlyContinue
                if (-not $targetResourceGroup) {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Resource group '$($command.ResourceGroupName)' was not found in the zolab subscription. Send another command.")
                    Write-Host "Resource group '$($command.ResourceGroupName)' was not found. Waiting for another command."
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

                $confirmation = Wait-FoundryTeamsChatChoice `
                    -ChatId $chat.Id `
                    -AllowedChoices @('confirm teardown', 'abort') `
                    -PromptCreatedDateTime $confirmationPrompt.CreatedDateTime `
                    -PromptMessageId $confirmationPrompt.Id `
                    -TimeoutMinutes $ConfirmationTimeoutMinutes `
                    -PollIntervalSeconds $PollIntervalSeconds

                if ($confirmation.Choice -eq 'abort') {
                    [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Teardown request for '$($targetResourceGroup.ResourceGroupName)' aborted.")
                    Write-Host "Teardown request aborted."
                    return
                }

                [void](Send-FoundryTeamsChatMessage -ChatId $chat.Id -Message "Teardown confirmed for '$($targetResourceGroup.ResourceGroupName)'. Starting cleanup...")
                Write-Host "Teardown confirmed for '$($targetResourceGroup.ResourceGroupName)'. Starting cleanup..."
                & $deployScript -Cleanup -CleanupResourceGroup $targetResourceGroup.ResourceGroupName -UseTeamsChatFlow -TeamsChatTopic $TeamsChatTopic
                return
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
