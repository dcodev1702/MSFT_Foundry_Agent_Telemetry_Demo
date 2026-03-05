// ════════════════════════════════════════════════════════════════
// law-rbac.bicep (module) — Creates a role assignment scoped to
// the Log Analytics Workspace resource.
// ════════════════════════════════════════════════════════════════

@description('Name of the Log Analytics Workspace')
param lawName string

@description('Object ID of the principal to assign the role to')
param principalId string

@description('Built-in role definition ID to assign')
param roleDefinitionId string

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: lawName
}

resource lawReaderAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(law.id, principalId, roleDefinitionId)
  scope: law
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleDefinitionId)
    principalId: principalId
    principalType: 'Group'
  }
}
