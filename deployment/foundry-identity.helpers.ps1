function Resolve-FoundryRequesterContext {
    param(
        [string]$RequestedBy,

        [string]$RequestedByObjectId,

        [string]$GraphContextAccount,

        [scriptblock]$GraphUserResolver
    )

    $resolvedRequestedBy = if ([string]::IsNullOrWhiteSpace($RequestedBy)) {
        $null
    } else {
        $RequestedBy.Trim()
    }
    $resolvedRequestedByObjectId = if ([string]::IsNullOrWhiteSpace($RequestedByObjectId)) {
        $null
    } else {
        $RequestedByObjectId.Trim()
    }
    $resolvedGraphContextAccount = if ([string]::IsNullOrWhiteSpace($GraphContextAccount)) {
        $null
    } else {
        $GraphContextAccount.Trim()
    }

    if (-not $resolvedRequestedBy) {
        $resolvedRequestedBy = $resolvedGraphContextAccount
    }

    if (-not $GraphUserResolver) {
        $GraphUserResolver = {
            param($Account)

            Get-GraphUserObjectId -Account $Account
        }
    }

    if (-not $resolvedRequestedByObjectId -and $resolvedRequestedBy) {
        $resolvedRequestedByObjectId = & $GraphUserResolver $resolvedRequestedBy
    }

    [pscustomobject]@{
        RequestedBy            = $resolvedRequestedBy
        RequestedByObjectId    = $resolvedRequestedByObjectId
        GraphContextAccount    = $resolvedGraphContextAccount
        HasExplicitRequester   = -not [string]::IsNullOrWhiteSpace($RequestedBy)
        HasExplicitObjectId    = -not [string]::IsNullOrWhiteSpace($RequestedByObjectId)
        UsedGraphContextBackup = (-not [string]::IsNullOrWhiteSpace($resolvedGraphContextAccount)) -and ($resolvedRequestedBy -eq $resolvedGraphContextAccount)
    }
}