function Get-TargetedTeardownSharedAccessPlanFromInventory {
    param(
        [AllowNull()]
        [object[]]$RemainingBuilds
    )

    $normalizedBuilds = @(
        $RemainingBuilds |
            Where-Object { $_ -and -not [string]::IsNullOrWhiteSpace([string]$_.ResourceGroupName) }
    )

    [pscustomobject]@{
        RemainingBuilds            = $normalizedBuilds
        RemainingBuildCount        = $normalizedBuilds.Count
        RemainingBuildNames        = @($normalizedBuilds | ForEach-Object { $_.ResourceGroupName })
        ShouldRetainLawRbac        = ($normalizedBuilds.Count -gt 0)
        ShouldRetainUserMembership = ($normalizedBuilds.Count -gt 0)
    }
}

function Get-FoundryManagedResourceGroupRoleDefinitionNames {
    @(
        'Azure AI Developer'
        'Azure AI User'
        'Reader'
        'Storage Blob Data Contributor'
        'Key Vault Secrets Officer'
        'Key Vault Crypto Officer'
    )
}

function Get-FoundryManagedResourceGroupAssignmentPlan {
    param(
        [AllowNull()]
        [object[]]$Assignments,

        [string]$ResourceGroupScope
    )

    $managedRoleLookup = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($roleName in Get-FoundryManagedResourceGroupRoleDefinitionNames) {
        [void]$managedRoleLookup.Add($roleName)
    }

    $scopedAssignments = @(
        $Assignments |
            Where-Object {
                $_ -and
                -not [string]::IsNullOrWhiteSpace([string]$_.RoleDefinitionName) -and
                (
                    [string]::IsNullOrWhiteSpace($ResourceGroupScope) -or
                    ([string]$_.Scope -ieq $ResourceGroupScope)
                )
            }
    )
    $managedAssignments = @(
        $scopedAssignments | Where-Object { $managedRoleLookup.Contains([string]$_.RoleDefinitionName) }
    )
    $preservedAssignments = @(
        $scopedAssignments | Where-Object { -not $managedRoleLookup.Contains([string]$_.RoleDefinitionName) }
    )

    [pscustomobject]@{
        ScopedAssignments    = $scopedAssignments
        ManagedAssignments   = $managedAssignments
        PreservedAssignments = $preservedAssignments
    }
}
