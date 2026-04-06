Set-StrictMode -Version Latest

Describe 'Connect-AzureCliWithManagedIdentityRetry' {
    BeforeAll {
        . (Join-Path $PSScriptRoot '..' 'foundry-azure-auth.helpers.ps1')
    }

    It 'returns the CLI context after a transient managed identity login failure' {
        $script:loginAttempts = @()
        $script:contextCalls = 0
        $script:sleepCalls = @()

        $context = Connect-AzureCliWithManagedIdentityRetry `
            -ClientId '59bffc04-c429-4580-9833-8ce88c088877' `
            -MaxAttempts 3 `
            -CliLoginInvoker {
                param($ClientId, $UseLegacyUsername)

                $script:loginAttempts += if ($UseLegacyUsername) { 'legacy' } else { 'client-id' }
                if ($script:loginAttempts.Count -eq 1) {
                    throw 'transient managed identity endpoint failure'
                }
            } `
            -CliContextGetter {
                $script:contextCalls += 1
                if ($script:contextCalls -lt 2) {
                    return $null
                }

                [pscustomobject]@{
                    account        = 'managed-identity'
                    tenantId       = 'b22dee98-83da-4207-b9ab-5ba931866f44'
                    subscriptionId = '08fdc492-f5aa-4601-84ae-03a37449c2ba'
                }
            } `
            -SleepCommand {
                param($Seconds)

                $script:sleepCalls += $Seconds
            }

        $context.account | Should -Be 'managed-identity'
        $script:loginAttempts | Should -Be @('client-id', 'legacy', 'client-id')
        $script:sleepCalls | Should -Be @(2)
    }

    It 'retries and throws when no Azure CLI context can be established' {
        $script:sleepCalls = @()

        {
            Connect-AzureCliWithManagedIdentityRetry `
                -ClientId '59bffc04-c429-4580-9833-8ce88c088877' `
                -MaxAttempts 2 `
                -CliLoginInvoker {
                    param($ClientId, $UseLegacyUsername)

                    throw "login failed using $([bool]$UseLegacyUsername)"
                } `
                -CliContextGetter { $null } `
                -SleepCommand {
                    param($Seconds)

                    $script:sleepCalls += $Seconds
                }
        } | Should -Throw 'Managed identity Azure CLI sign-in failed after 2 attempts.*'

        $script:sleepCalls | Should -Be @(2)
    }
}