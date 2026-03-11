Set-StrictMode -Version Latest

Describe 'Get-TargetedTeardownSharedAccessPlanFromInventory' {
    BeforeAll {
        . (Join-Path $PSScriptRoot '..' 'foundry-teardown.helpers.ps1')
    }

    It 'retains shared LAW RBAC and user membership when another build exists' {
        $plan = Get-TargetedTeardownSharedAccessPlanFromInventory -RemainingBuilds @(
            [pscustomobject]@{ ResourceGroupName = 'zolab-ai-bbb222' }
        )

        $plan.RemainingBuildCount | Should -Be 1
        $plan.ShouldRetainLawRbac | Should -BeTrue
        $plan.ShouldRetainUserMembership | Should -BeTrue
        $plan.RemainingBuildNames | Should -Be @('zolab-ai-bbb222')
    }

    It 'allows shared LAW RBAC and user membership removal when no builds remain' {
        $plan = Get-TargetedTeardownSharedAccessPlanFromInventory -RemainingBuilds @()

        $plan.RemainingBuildCount | Should -Be 0
        $plan.ShouldRetainLawRbac | Should -BeFalse
        $plan.ShouldRetainUserMembership | Should -BeFalse
    }
}

Describe 'Get-TargetedTeardownSharedAccessPlan' {
    BeforeAll {
        . (Join-Path $PSScriptRoot '..' 'foundry-teardown.helpers.ps1')
    }

    It 'excludes the targeted resource group and preserves shared access when another build remains' {
        $requestedExclusion = $null
        $plan = Get-TargetedTeardownSharedAccessPlan `
            -TargetResourceGroupName 'zolab-ai-aaa111' `
            -InventoryResolver {
                param($ExcludedResourceGroupName)

                $script:requestedExclusion = $ExcludedResourceGroupName
                @(
                    [pscustomobject]@{ ResourceGroupName = 'zolab-ai-bbb222' }
                )
            }

        $script:requestedExclusion | Should -Be 'zolab-ai-aaa111'
        $plan.RemainingBuildCount | Should -Be 1
        $plan.ShouldRetainLawRbac | Should -BeTrue
        $plan.ShouldRetainUserMembership | Should -BeTrue
        $plan.RemainingBuildNames | Should -Be @('zolab-ai-bbb222')
    }

    It 'allows shared access cleanup when no other builds remain' {
        $plan = Get-TargetedTeardownSharedAccessPlan `
            -TargetResourceGroupName 'zolab-ai-aaa111' `
            -InventoryResolver { param($ExcludedResourceGroupName) @() }

        $plan.RemainingBuildCount | Should -Be 0
        $plan.ShouldRetainLawRbac | Should -BeFalse
        $plan.ShouldRetainUserMembership | Should -BeFalse
    }
}

Describe 'Get-FoundryManagedResourceGroupAssignmentPlan' {
    BeforeAll {
        . (Join-Path $PSScriptRoot '..' 'foundry-teardown.helpers.ps1')
    }

    It 'selects only the six managed resource-group roles for removal' {
        $scope = '/subscriptions/sub-123/resourceGroups/zolab-ai-abc123'
        $plan = Get-FoundryManagedResourceGroupAssignmentPlan -Assignments @(
            [pscustomobject]@{ RoleDefinitionName = 'Azure AI Developer'; Scope = $scope },
            [pscustomobject]@{ RoleDefinitionName = 'Azure AI User'; Scope = $scope },
            [pscustomobject]@{ RoleDefinitionName = 'Reader'; Scope = $scope },
            [pscustomobject]@{ RoleDefinitionName = 'Storage Blob Data Contributor'; Scope = $scope },
            [pscustomobject]@{ RoleDefinitionName = 'Key Vault Secrets Officer'; Scope = $scope },
            [pscustomobject]@{ RoleDefinitionName = 'Key Vault Crypto Officer'; Scope = $scope },
            [pscustomobject]@{ RoleDefinitionName = 'Contributor'; Scope = $scope },
            [pscustomobject]@{ RoleDefinitionName = 'Reader'; Scope = '/subscriptions/sub-123/resourceGroups/other-rg' }
        ) -ResourceGroupScope $scope

        @($plan.ManagedAssignments).Count | Should -Be 6
        @($plan.PreservedAssignments).Count | Should -Be 1
        @($plan.ManagedAssignments | ForEach-Object RoleDefinitionName) | Should -Contain 'Azure AI Developer'
        @($plan.ManagedAssignments | ForEach-Object RoleDefinitionName) | Should -Contain 'Azure AI User'
        @($plan.ManagedAssignments | ForEach-Object RoleDefinitionName) | Should -Contain 'Reader'
        @($plan.ManagedAssignments | ForEach-Object RoleDefinitionName) | Should -Contain 'Storage Blob Data Contributor'
        @($plan.ManagedAssignments | ForEach-Object RoleDefinitionName) | Should -Contain 'Key Vault Secrets Officer'
        @($plan.ManagedAssignments | ForEach-Object RoleDefinitionName) | Should -Contain 'Key Vault Crypto Officer'
        @($plan.PreservedAssignments | ForEach-Object RoleDefinitionName) | Should -Be @('Contributor')
    }

    It 'treats role names case-insensitively and preserves unrelated assignments' {
        $scope = '/subscriptions/sub-123/resourceGroups/zolab-ai-abc123'
        $plan = Get-FoundryManagedResourceGroupAssignmentPlan -Assignments @(
            [pscustomobject]@{ RoleDefinitionName = 'reader'; Scope = $scope },
            [pscustomobject]@{ RoleDefinitionName = 'custom role'; Scope = $scope }
        ) -ResourceGroupScope $scope

        @($plan.ManagedAssignments | ForEach-Object RoleDefinitionName) | Should -Be @('reader')
        @($plan.PreservedAssignments | ForEach-Object RoleDefinitionName) | Should -Be @('custom role')
    }
}
