Set-StrictMode -Version Latest

Describe 'Resolve-FoundryRequesterContext' {
    BeforeAll {
        . (Join-Path $PSScriptRoot '..' 'foundry-identity.helpers.ps1')
    }

    It 'prefers the explicit requester and resolves its object id' {
        $plan = Resolve-FoundryRequesterContext `
            -RequestedBy 'user@dibsecurity.onmicrosoft.com' `
            -GraphContextAccount '' `
            -GraphUserResolver { param($Account) "resolved:$Account" }

        $plan.RequestedBy | Should -Be 'user@dibsecurity.onmicrosoft.com'
        $plan.RequestedByObjectId | Should -Be 'resolved:user@dibsecurity.onmicrosoft.com'
        $plan.HasExplicitRequester | Should -BeTrue
        $plan.UsedGraphContextBackup | Should -BeFalse
    }

    It 'uses the explicit object id without calling the resolver' {
        $script:resolverCalls = 0
        $plan = Resolve-FoundryRequesterContext `
            -RequestedBy 'user@dibsecurity.onmicrosoft.com' `
            -RequestedByObjectId 'entra-object-id' `
            -GraphUserResolver {
                param($Account)
                $script:resolverCalls += 1
                "resolved:$Account"
            }

        $plan.RequestedBy | Should -Be 'user@dibsecurity.onmicrosoft.com'
        $plan.RequestedByObjectId | Should -Be 'entra-object-id'
        $script:resolverCalls | Should -Be 0
    }

    It 'falls back to the graph context account when no explicit requester is supplied' {
        $plan = Resolve-FoundryRequesterContext `
            -GraphContextAccount 'graph-user@dibsecurity.onmicrosoft.com' `
            -GraphUserResolver { param($Account) "resolved:$Account" }

        $plan.RequestedBy | Should -Be 'graph-user@dibsecurity.onmicrosoft.com'
        $plan.RequestedByObjectId | Should -Be 'resolved:graph-user@dibsecurity.onmicrosoft.com'
        $plan.UsedGraphContextBackup | Should -BeTrue
    }

    It 'leaves the requester unresolved when neither an explicit requester nor graph account exists' {
        $plan = Resolve-FoundryRequesterContext -GraphUserResolver { throw 'should not be called' }

        $plan.RequestedBy | Should -BeNullOrEmpty
        $plan.RequestedByObjectId | Should -BeNullOrEmpty
        $plan.UsedGraphContextBackup | Should -BeFalse
    }
}