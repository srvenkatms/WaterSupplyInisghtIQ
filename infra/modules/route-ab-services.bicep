param location string
param prefix string
param tags object
param routeVnetIds array
param enableRouteRuntime bool = false
param preserveConsoleSubnet bool = false
param runnerPrincipalIds array = ['', '']
param gatewayPrincipalIds array = ['', '']

var suffix = uniqueString(resourceGroup().id)

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take('st${prefix}${suffix}', 24)
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    publicNetworkAccess: 'Disabled'
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource state 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for route in ['routea', 'routeb']: {
  parent: blobs
  name: '${route}-state'
  properties: { publicAccess: 'None' }
}]

resource queues 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource commands 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = [for route in ['routea', 'routeb']: {
  parent: queues
  name: '${route}-commands'
}]

resource auditQueues 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = [for route in ['routea', 'routeb']: {
  parent: queues
  name: '${route}-audit'
}]

resource runnerState 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (route, i) in ['routea', 'routeb']: if (enableRouteRuntime) {
  scope: state[i]
  name: guid(state[i].id, runnerPrincipalIds[i], 'runner-state')
  properties: {
    principalId: runnerPrincipalIds[i]
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}]
resource runnerCommands 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (route, i) in ['routea', 'routeb']: if (enableRouteRuntime) {
  scope: commands[i]
  name: guid(commands[i].id, runnerPrincipalIds[i], 'runner-commands')
  properties: {
    principalId: runnerPrincipalIds[i]
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '8a0f0c08-91a1-4084-bc3d-661d67233fed')
  }
}]
resource runnerAudit 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (route, i) in ['routea', 'routeb']: if (enableRouteRuntime) {
  scope: auditQueues[i]
  name: guid(auditQueues[i].id, runnerPrincipalIds[i], 'runner-audit')
  properties: {
    principalId: runnerPrincipalIds[i]
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'c6a89b2d-59bc-44d0-9896-0f6e12d7b80a')
  }
}]
resource gatewayAudit 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (route, i) in ['routea', 'routeb']: if (enableRouteRuntime) {
  scope: auditQueues[i]
  name: guid(auditQueues[i].id, gatewayPrincipalIds[i], 'gateway-audit')
  properties: {
    principalId: gatewayPrincipalIds[i]
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '8a0f0c08-91a1-4084-bc3d-661d67233fed')
  }
}]

resource consoleLogs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${prefix}-shared'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: 1 }
  }
}

module privateServices 'private-services.bicep' = {
  name: 'private-services'
  params: {
    location: location
    prefix: prefix
    tags: tags
    storageAccountId: storage.id
    labVnetId: routeVnetIds[0]
    additionalLabVnetIds: [routeVnetIds[1]]
    preserveConsoleSubnet: preserveConsoleSubnet
    additionalStorageClientPrefixes: concat(['10.30.5.0/24', '10.40.5.0/24'],
      enableRouteRuntime ? ['10.30.4.0/24', '10.40.4.0/24', '10.30.2.0/24', '10.40.2.0/24'] : [])
  }
}

resource collector 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-collector'
  location: location
  tags: tags
}

resource observer 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-observer'
  location: location
  tags: tags
}

resource dce 'Microsoft.Insights/dataCollectionEndpoints@2022-06-01' = {
  name: 'dce-${prefix}'
  location: location
  tags: tags
  properties: { networkAcls: { publicNetworkAccess: 'Enabled' } }
}

output servicesVnetId string = privateServices.outputs.vnetId
output storageAccountName string = storage.name
output dceId string = dce.id
output dceEndpoint string = dce.properties.logsIngestion.endpoint
output collectorPrincipalId string = collector.properties.principalId
output observerPrincipalId string = observer.properties.principalId
