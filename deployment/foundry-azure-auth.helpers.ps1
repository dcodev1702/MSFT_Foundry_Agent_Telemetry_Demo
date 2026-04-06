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

function Get-AzureCliContext {
    param(
        [scriptblock]$CliAccountShowInvoker
    )

    if (-not $CliAccountShowInvoker) {
        $CliAccountShowInvoker = {
            & az account show --query "{account:user.name,tenantId:tenantId,subscriptionId:id}" --output json 2>$null
        }
    }

    $cliContextJson = & $CliAccountShowInvoker
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($cliContextJson)) {
        return $null
    }

    $cliContextJson | ConvertFrom-Json
}

function Connect-AzureCliWithManagedIdentityRetry {
    param(
        [Parameter(Mandatory)]
        [string]$ClientId,

        [int]$MaxAttempts = 5,

        [int]$InitialDelaySeconds = 2,

        [scriptblock]$CliLoginInvoker,

        [scriptblock]$CliContextGetter,

        [scriptblock]$SleepCommand
    )

    if (-not $CliLoginInvoker) {
        $CliLoginInvoker = {
            param($LoginClientId, $UseLegacyUsername)

            $loginOutput = if ($UseLegacyUsername) {
                & az login --identity --username $LoginClientId --output none 2>&1
            } else {
                & az login --identity --client-id $LoginClientId --output none 2>&1
            }
            $loginExitCode = $LASTEXITCODE
            $loginMessage = ($loginOutput | Out-String).Trim()

            if ($UseLegacyUsername) {
                if ($loginExitCode -ne 0) {
                    $suffix = if ([string]::IsNullOrWhiteSpace($loginMessage)) { '' } else { " $loginMessage" }
                    throw "Azure CLI managed identity auth failed using the legacy --username flag.$suffix"
                }
                return
            }

            if ($loginExitCode -ne 0) {
                $suffix = if ([string]::IsNullOrWhiteSpace($loginMessage)) { '' } else { " $loginMessage" }
                throw "Azure CLI managed identity auth failed using the --client-id flag.$suffix"
            }
        }
    }

    if (-not $CliContextGetter) {
        $CliContextGetter = {
            Get-AzureCliContext
        }
    }

    if (-not $SleepCommand) {
        $SleepCommand = {
            param($Seconds)

            Start-Sleep -Seconds $Seconds
        }
    }

    $attempt = 1
    $delaySeconds = $InitialDelaySeconds
    $lastErrorMessage = $null

    while ($attempt -le $MaxAttempts) {
        $attemptErrors = @()
        foreach ($useLegacyUsername in @($false, $true)) {
            try {
                & $CliLoginInvoker $ClientId $useLegacyUsername
                $cliContext = & $CliContextGetter
                if ($cliContext) {
                    return $cliContext
                }

                $attemptErrors += if ($useLegacyUsername) {
                    'Azure CLI managed identity auth returned no account context after the legacy --username login.'
                } else {
                    'Azure CLI managed identity auth returned no account context after the --client-id login.'
                }
            } catch {
                $attemptErrors += $_.Exception.Message
            }
        }

        $lastErrorMessage = ($attemptErrors | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join ' | '

        if ($attempt -ge $MaxAttempts) {
            break
        }

        Write-Warning "Managed identity Azure CLI sign-in attempt $attempt/$MaxAttempts failed: $lastErrorMessage"
        & $SleepCommand $delaySeconds
        $attempt += 1
        $delaySeconds = [Math]::Min($delaySeconds * 2, 15)
    }

    throw "Managed identity Azure CLI sign-in failed after $MaxAttempts attempts. $lastErrorMessage"
}