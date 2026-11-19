// Runs the Teams playbook on every new incident in one utility workspace.
// Sentinel also needs the 'Microsoft Sentinel Automation Contributor' role on
// the shared resource group for its 'Azure Security Insights' app (one-time,
// tenant-specific; see scripts/deploy.sh).
param workspaceName string
param playbookId string
param playbookPrincipalId string

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

resource rule 'Microsoft.SecurityInsights/automationRules@2024-03-01' = {
  scope: law
  name: guid(law.id, 'swulab-teams-playbook')
  properties: {
    displayName: 'Notify operator in Teams'
    order: 1
    triggeringLogic: {
      isEnabled: true
      triggersOn: 'Incidents'
      triggersWhen: 'Created'
    }
    actions: [
      {
        order: 1
        actionType: 'RunPlaybook'
        actionConfiguration: {
          logicAppResourceId: playbookId
          tenantId: subscription().tenantId
        }
      }
    ]
  }
}

// The playbook's identity may add comments to incidents (Sentinel Responder).
resource playbookResponder 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: law
  name: guid(law.id, playbookPrincipalId, '3e150937-b8fe-4cfb-8069-0eaf05ecd056')
  properties: {
    principalId: playbookPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3e150937-b8fe-4cfb-8069-0eaf05ecd056')
  }
}
