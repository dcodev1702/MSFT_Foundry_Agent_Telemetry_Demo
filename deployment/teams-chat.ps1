function Convert-TeamsChatMessageToPlainText {
    param(
        [AllowNull()]
        [AllowEmptyString()]
        [string]$Content
    )

    if ([string]::IsNullOrWhiteSpace($Content)) {
        return ''
    }

    $text = [System.Net.WebUtility]::HtmlDecode($Content)
    $text = [regex]::Replace($text, '<[^>]+>', ' ')
    $text = [regex]::Replace($text, '\s+', ' ')
    $text.Trim()
}

function Normalize-FoundryTeamsToken {
    param(
        [Parameter(Mandatory)]
        [string]$Text
    )

    ([regex]::Replace($Text.ToLowerInvariant(), '[^a-z0-9]', '')).Trim()
}

function Get-FoundryGraphContextScope {
    $configuredScope = $env:FOUNDRY_GRAPH_CONTEXT_SCOPE
    if (-not [string]::IsNullOrWhiteSpace($configuredScope)) {
        $normalizedScope = $configuredScope.Trim()
        if ($normalizedScope -notin @('CurrentUser', 'Process')) {
            throw "FOUNDRY_GRAPH_CONTEXT_SCOPE must be either 'CurrentUser' or 'Process'."
        }

        return $normalizedScope
    }

    'CurrentUser'
}

function Get-FoundryPowerShellPath {
    $pwshCommand = Get-Command pwsh -CommandType Application -ErrorAction SilentlyContinue
    if (-not $pwshCommand) {
        throw "PowerShell 7 ('pwsh') is required but was not found on PATH."
    }

    if ($pwshCommand.Path) {
        return $pwshCommand.Path
    }

    if ($pwshCommand.Source) {
        return $pwshCommand.Source
    }

    $pwshCommand.Name
}

function Connect-FoundryGraphIfNeeded {
    param(
        [Parameter(Mandatory)]
        [string[]]$Scopes
    )

    $ctx = Get-MgContext
    $missingScopes = if ($ctx) {
        $Scopes | Where-Object { $_ -notin $ctx.Scopes }
    } else {
        $Scopes
    }

    if (-not $ctx -or $missingScopes.Count -gt 0) {
        $uamiClientId = $env:AZURE_CLIENT_ID
        if ($uamiClientId) {
            # Headless container — use managed identity (application permissions)
            Write-Host "Connecting to Microsoft Graph via managed identity (AZURE_CLIENT_ID=$uamiClientId)..."
            Connect-MgGraph -Identity -ClientId $uamiClientId -NoWelcome | Out-Null
        } else {
            # Interactive desktop — use delegated scopes
            $graphContextScope = Get-FoundryGraphContextScope
            Write-Host "Connecting to Microsoft Graph using ContextScope '$graphContextScope'..."
            Connect-MgGraph -Scopes $Scopes -ContextScope $graphContextScope -NoWelcome | Out-Null
        }
        $ctx = Get-MgContext
    }

    $ctx
}

function Resolve-FoundryTeamsChoiceFromMessage {
    param(
        [AllowEmptyString()]
        [string]$MessageText,

        [Parameter(Mandatory)]
        [string[]]$AllowedChoices
    )

    $trimmed = $MessageText.Trim()
    if (-not $trimmed) {
        return $null
    }

    if ($trimmed -match '^\d+$') {
        $selectedIndex = [int]$trimmed
        if ($selectedIndex -ge 1 -and $selectedIndex -le $AllowedChoices.Count) {
            return $AllowedChoices[$selectedIndex - 1]
        }
    }

    $normalizedInput = Normalize-FoundryTeamsToken -Text $trimmed
    foreach ($choice in $AllowedChoices) {
        if ($choice.Equals($trimmed, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $choice
        }

        if ((Normalize-FoundryTeamsToken -Text $choice) -eq $normalizedInput) {
            return $choice
        }
    }

    $null
}

function Resolve-AiModelChoiceFromMessage {
    param(
        [AllowEmptyString()]
        [string]$MessageText,

        [Parameter(Mandatory)]
        [string[]]$AllowedChoices
    )

    Resolve-FoundryTeamsChoiceFromMessage -MessageText $MessageText -AllowedChoices $AllowedChoices
}

function Resolve-FoundryTeamsCommandFromMessage {
    param(
        [AllowEmptyString()]
        [string]$MessageText
    )

    $trimmed = $MessageText.Trim()
    if (-not $trimmed) {
        return $null
    }

    if ($trimmed -match '^(?i)build\s+it$') {
        return [pscustomobject]@{
            CommandType = 'build'
            CommandText = $trimmed
        }
    }

    if ($trimmed -match '^(?i)list\s+builds$') {
        return [pscustomobject]@{
            CommandType = 'list-builds'
            CommandText = $trimmed
        }
    }

    if ($trimmed -match '^(?i)build\s+status\s+["'']?([A-Za-z0-9-]+)["'']?$') {
        return [pscustomobject]@{
            CommandType       = 'build-status'
            CommandText       = $trimmed
            ResourceGroupName = $matches[1]
        }
    }

    if ($trimmed -match '^(?i)listener\s+status$') {
        return [pscustomobject]@{
            CommandType = 'listener-status'
            CommandText = $trimmed
        }
    }

    if ($trimmed -match '^(?i)heartbeat$') {
        return [pscustomobject]@{
            CommandType = 'heartbeat'
            CommandText = $trimmed
        }
    }

    if ($trimmed -in @('?', 'help')) {
        return [pscustomobject]@{
            CommandType = 'help'
            CommandText = $trimmed
        }
    }

    if ($trimmed -match '^(?i)stop\s+listener$') {
        return [pscustomobject]@{
            CommandType = 'stop-listener'
            CommandText = $trimmed
        }
    }

    if ($trimmed -match '^(?i)teardown$') {
        return [pscustomobject]@{
            CommandType       = 'teardown'
            CommandText       = $trimmed
            ResourceGroupName = $null
        }
    }

    if ($trimmed -match '^(?i)teardown\s+["'']?([A-Za-z0-9-]+)["'']?$') {
        return [pscustomobject]@{
            CommandType       = 'teardown'
            CommandText       = $trimmed
            ResourceGroupName = $matches[1]
        }
    }

    $null
}

function Get-OrCreate-FoundryTeamsChat {
    param(
        [Parameter(Mandatory)]
        [string]$UserId,

        [Parameter(Mandatory)]
        [string]$Topic
    )

    $existingChat = Get-MgChat -All |
        Where-Object { $_.ChatType -eq 'group' -and $_.Topic -eq $Topic } |
        Sort-Object @{ Expression = { $_.LastUpdatedDateTime }; Descending = $true } |
        Select-Object -First 1

    if ($existingChat) {
        return $existingChat
    }

    $selfMember = @{
        '@odata.type'     = '#microsoft.graph.aadUserConversationMember'
        roles             = @('owner')
        'user@odata.bind' = "https://graph.microsoft.com/v1.0/users('$UserId')"
    }

    New-MgChat -BodyParameter @{
        chatType = 'group'
        topic    = $Topic
        members  = @($selfMember)
    }
}

function Send-FoundryTeamsChatMessage {
    param(
        [Parameter(Mandatory)]
        [string]$ChatId,

        [Parameter(Mandatory)]
        [string]$Message
    )

    $htmlLines = foreach ($line in ($Message -split "`r?`n")) {
        ([System.Net.WebUtility]::HtmlEncode($line)).Replace(' ', '&nbsp;')
    }
    $htmlMessage = $htmlLines -join '<br/>'

    $chatMessage = New-MgChatMessage -ChatId $ChatId -BodyParameter @{
        body = @{
            contentType = 'html'
            content     = $htmlMessage
        }
    }

    $previewLine = (($Message -split "`r?`n" | Select-Object -First 1) -join '').Trim()
    if ($previewLine.Length -gt 120) {
        $previewLine = $previewLine.Substring(0, 117) + '...'
    }

    $global:FoundryTeamsLastResponse = [pscustomobject]@{
        SentAtUtc = (Get-Date).ToUniversalTime()
        ChatId    = $ChatId
        MessageId = $chatMessage.Id
        Preview   = if ($previewLine) { $previewLine } else { '[blank message]' }
    }

    $chatMessage
}

function Wait-FoundryTeamsChatChoice {
    param(
        [Parameter(Mandatory)]
        [string]$ChatId,

        [Parameter(Mandatory)]
        [string[]]$AllowedChoices,

        [Parameter(Mandatory)]
        [datetime]$PromptCreatedDateTime,

        [string]$PromptMessageId,

        [int]$TimeoutMinutes = 30,

        [int]$PollIntervalSeconds = 10
    )

    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    $processedMessageIds = [System.Collections.Generic.HashSet[string]]::new()

    while ((Get-Date) -lt $deadline) {
        $messages = Get-MgChatMessage -ChatId $ChatId -Top 20
        foreach ($message in ($messages | Sort-Object CreatedDateTime)) {
            if ($PromptMessageId -and $message.Id -eq $PromptMessageId) {
                continue
            }

            if ($message.CreatedDateTime -le $PromptCreatedDateTime) {
                continue
            }

            if (-not $processedMessageIds.Add($message.Id)) {
                continue
            }

            $messageText = Convert-TeamsChatMessageToPlainText -Content $message.Body.Content
            if ([string]::IsNullOrWhiteSpace($messageText)) {
                continue
            }
            $resolvedChoice = Resolve-FoundryTeamsChoiceFromMessage -MessageText $messageText -AllowedChoices $AllowedChoices
            if ($resolvedChoice) {
                return [pscustomobject]@{
                    MessageId   = $message.Id
                    MessageText = $messageText
                    Choice      = $resolvedChoice
                }
            }
        }

        Start-Sleep -Seconds $PollIntervalSeconds
    }

    throw "Timed out waiting for a Teams response after $TimeoutMinutes minutes."
}

function Wait-FoundryTeamsChatResponse {
    param(
        [Parameter(Mandatory)]
        [string]$ChatId,

        [Parameter(Mandatory)]
        [string[]]$AllowedChoices,

        [Parameter(Mandatory)]
        [datetime]$PromptCreatedDateTime,

        [string]$PromptMessageId,

        [int]$TimeoutMinutes = 30,

        [int]$PollIntervalSeconds = 10
    )

    Wait-FoundryTeamsChatChoice `
        -ChatId $ChatId `
        -AllowedChoices $AllowedChoices `
        -PromptCreatedDateTime $PromptCreatedDateTime `
        -PromptMessageId $PromptMessageId `
        -TimeoutMinutes $TimeoutMinutes `
        -PollIntervalSeconds $PollIntervalSeconds
}

function Wait-FoundryTeamsChatCommand {
    param(
        [Parameter(Mandatory)]
        [string]$ChatId,

        [Parameter(Mandatory)]
        [datetime]$PromptCreatedDateTime,

        [string]$PromptMessageId,

        [int]$TimeoutMinutes = 60,

        [datetime]$DeadlineAt,

        [int]$PollIntervalSeconds = 10
    )

    $deadline = if ($PSBoundParameters.ContainsKey('DeadlineAt')) {
        $DeadlineAt.ToUniversalTime()
    } else {
        (Get-Date).ToUniversalTime().AddMinutes($TimeoutMinutes)
    }
    $processedMessageIds = [System.Collections.Generic.HashSet[string]]::new()

    while ((Get-Date).ToUniversalTime() -lt $deadline) {
        $messages = Get-MgChatMessage -ChatId $ChatId -Top 20
        foreach ($message in ($messages | Sort-Object CreatedDateTime)) {
            if ($PromptMessageId -and $message.Id -eq $PromptMessageId) {
                continue
            }

            if ($message.CreatedDateTime -le $PromptCreatedDateTime) {
                continue
            }

            if (-not $processedMessageIds.Add($message.Id)) {
                continue
            }

            $messageText = Convert-TeamsChatMessageToPlainText -Content $message.Body.Content
            if ([string]::IsNullOrWhiteSpace($messageText)) {
                continue
            }
            $resolvedCommand = Resolve-FoundryTeamsCommandFromMessage -MessageText $messageText
            if ($resolvedCommand) {
                return [pscustomobject]@{
                    MessageId         = $message.Id
                    MessageText       = $messageText
                    CommandType       = $resolvedCommand.CommandType
                    ResourceGroupName = $resolvedCommand.ResourceGroupName
                }
            }
        }

        Start-Sleep -Seconds $PollIntervalSeconds
    }

    throw "Timed out waiting for a Teams command after $TimeoutMinutes minutes."
}
