param workspaceName string
param principalId string
param incidentResponse bool = false
param queueStorageAccountName string = ''

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

resource workspaceReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: workspace
  name: guid(workspace.id, principalId, '73c42c96-874c-492b-b04d-ab87d138a893')
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '73c42c96-874c-492b-b04d-ab87d138a893')
  }
}

resource responder 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (incidentResponse) {
  scope: workspace
  name: guid(workspace.id, principalId, '3e150937-b8fe-4cfb-8069-0eaf05ecd056')
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3e150937-b8fe-4cfb-8069-0eaf05ecd056')
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = if (!empty(queueStorageAccountName)) {
  name: queueStorageAccountName
}

resource queueSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(queueStorageAccountName)) {
  scope: storage
  name: guid(storage!.id, principalId, 'c6a89b2d-59bc-44d0-9896-0f6e12d7b80a')
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'c6a89b2d-59bc-44d0-9896-0f6e12d7b80a')
  }
}
