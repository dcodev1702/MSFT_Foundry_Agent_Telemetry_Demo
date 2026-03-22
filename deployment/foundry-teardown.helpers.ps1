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

function Get-TargetedTeardownSharedAccessPlan {
    param(
        [Parameter(Mandatory)]
        [string]$TargetResourceGroupName,

        [scriptblock]$InventoryResolver
    )

    if (-not $InventoryResolver) {
        $InventoryResolver = {
            param($ExcludedResourceGroupName)

            Get-FoundryBuildInventory -ExcludeResourceGroupName $ExcludedResourceGroupName
        }
    }

    $remainingBuilds = @(& $InventoryResolver $TargetResourceGroupName)
    Get-TargetedTeardownSharedAccessPlanFromInventory -RemainingBuilds $remainingBuilds
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

function Get-FoundryFullTeardownAssignmentPlan {
    param(
        [AllowNull()]
        [object[]]$Assignments,

        [AllowEmptyCollection()]
        [string[]]$ManagedResourceGroupScopes
    )

    $normalizedAssignments = @(
        $Assignments |
            Where-Object {
                $_ -and
                -not [string]::IsNullOrWhiteSpace([string]$_.RoleDefinitionName) -and
                -not [string]::IsNullOrWhiteSpace([string]$_.Scope)
            }
    )

    $managedScopeLookup = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($scope in @($ManagedResourceGroupScopes)) {
        if (-not [string]::IsNullOrWhiteSpace($scope)) {
            [void]$managedScopeLookup.Add($scope)
        }
    }

    $managedAssignmentLookup = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $managedAssignments = @()

    foreach ($scope in $managedScopeLookup) {
        $scopePlan = Get-FoundryManagedResourceGroupAssignmentPlan -Assignments $normalizedAssignments -ResourceGroupScope $scope
        foreach ($assignment in @($scopePlan.ManagedAssignments)) {
            $assignmentKey = "{0}|{1}" -f [string]$assignment.Scope, [string]$assignment.RoleDefinitionName
            if ($managedAssignmentLookup.Add($assignmentKey)) {
                $managedAssignments += $assignment
            }
        }
    }

    $preservedAssignments = @(
        $normalizedAssignments |
            Where-Object {
                -not $managedAssignmentLookup.Contains(("{0}|{1}" -f [string]$_.Scope, [string]$_.RoleDefinitionName))
            }
    )

    [pscustomobject]@{
        ManagedAssignments   = $managedAssignments
        PreservedAssignments = $preservedAssignments
    }
}
