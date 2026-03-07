[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$JobPath,

    [switch]$Execute
)

if (-not (Test-Path -LiteralPath $JobPath)) {
    throw "Job file not found: $JobPath"
}

$job = Get-Content -LiteralPath $JobPath -Raw | ConvertFrom-Json

Write-Host "=== Sample worker ==="
Write-Host "Job ID      : $($job.job_id)"
Write-Host "Operation   : $($job.operation)"
Write-Host "Requested by: $($job.requested_by)"
Write-Host "Conversation: $($job.conversation_id)"
Write-Host "Submitted   : $($job.submitted_utc)"
Write-Host ""

switch ($job.operation) {
    'build' {
        $example = @(
            "Future non-interactive PowerShell handoff:"
            "Invoke-FoundryBuild -RequestedBy '$($job.requested_by)' -ModelChoice '$($job.model)' -JobId '$($job.job_id)'"
        )
    }
    'teardown' {
        $example = @(
            "Future non-interactive PowerShell handoff:"
            "Invoke-FoundryTeardown -RequestedBy '$($job.requested_by)' -ResourceGroup '$($job.resource_group)' -JobId '$($job.job_id)'"
        )
    }
    'build-status' {
        $example = @(
            "Future non-interactive PowerShell handoff:"
            "Get-FoundryBuildStatus -ResourceGroup '$($job.resource_group)' -JobId '$($job.job_id)'"
        )
    }
    'list-builds' {
        $example = @(
            "Future non-interactive PowerShell handoff:"
            "Get-FoundryBuildInventory -JobId '$($job.job_id)'"
        )
    }
    default {
        $example = @("No worker mapping defined for operation '$($job.operation)'.")
    }
}

$example | ForEach-Object { Write-Host $_ }

if (-not $Execute) {
    Write-Host ""
    Write-Host "Study mode only. Use -Execute after wiring these commands to real worker-safe entry points."
    return
}

throw "Execution mode is intentionally blocked in this study sample until real non-interactive worker entry points exist."
