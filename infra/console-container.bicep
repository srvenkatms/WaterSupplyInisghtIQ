targetScope = 'resourceGroup'

param location string = 'eastus2'
param prefix string = 'swulab'
param storageAccountName string
param enableLiveControl bool = false

var tags = { project: 'swu-lab', deployment: prefix, role: 'Authenticated lab console' }

resource services 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: 'vnet-${prefix}-services'
}

resource networkSecurity 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-${prefix}-console'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'allow-private-storage'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '10.20.2.0/27'
          destinationPortRange: '443'
        }
      }
      {
        name: 'allow-container-environment'
        properties: {
          priority: 105
          direction: 'Outbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '10.20.3.0/27'
          destinationPortRange: '*'
        }
      }
      {
        name: 'deny-direct-ot-access'
        properties: {
          priority: 110
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefixes: enableLiveControl ? ['10.30.0.0/16', '10.40.0.0/16'] : ['10.10.0.0/16']
          destinationPortRange: '*'
        }
      }
      {
        name: 'deny-other-private-access'
        properties: {
          priority: 120
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: services
  name: 'snet-console'
  properties: {
    addressPrefix: '10.20.3.0/27'
    defaultOutboundAccess: false
    natGateway: { id: resourceId('Microsoft.Network/natGateways', 'nat-${prefix}-services') }
    networkSecurityGroup: { id: networkSecurity.id }
    delegations: [{
      name: 'container-apps'
      properties: { serviceName: 'Microsoft.App/environments' }
    }]
  }
}

resource endpointNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' existing = {
  name: 'nsg-${prefix}-storage-endpoints'
}

resource storageRule 'Microsoft.Network/networkSecurityGroups/securityRules@2024-05-01' = {
  parent: endpointNsg
  name: 'allow-console-private-storage'
  properties: {
    priority: 110
    direction: 'Inbound'
    access: 'Allow'
    protocol: 'Tcp'
    sourceAddressPrefix: '10.20.3.0/27'
    sourcePortRange: '*'
    destinationAddressPrefix: '10.20.2.0/27'
    destinationPortRange: '443'
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-console'
  location: location
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'cr${prefix}${uniqueString(resourceGroup().id)}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource imageReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, identity.id, 'console-image-reader')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}
resource state 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'wateriq-console'
  properties: { publicAccess: 'None' }
}
resource stateWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: state
  name: guid(state.id, identity.id, 'console-state-writer')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}

resource routeStates 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = [for route in ['routea', 'routeb']: if (enableLiveControl) {
  parent: blobs
  name: '${route}-state'
}]
resource routeReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (route, i) in ['routea', 'routeb']: if (enableLiveControl) {
  scope: routeStates[i]
  name: guid(routeStates[i].id, identity.id, 'console-route-reader')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1')
  }
}]
resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}
resource routeCommands 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' existing = [for route in ['routea', 'routeb']: if (enableLiveControl) {
  parent: queueService
  name: '${route}-commands'
}]
resource routeSenders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (route, i) in ['routea', 'routeb']: if (enableLiveControl) {
  scope: routeCommands[i]
  name: guid(routeCommands[i].id, identity.id, 'console-command-sender')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'c6a89b2d-59bc-44d0-9896-0f6e12d7b80a')
  }
}]
module routeEvidence 'modules/operator-role.bicep' = [for route in ['routea', 'routeb']: if (enableLiveControl) {
  name: 'console-evidence-${route}'
  scope: resourceGroup('rg-${prefix}-${route}')
  params: {
    workspaceName: 'law-${prefix}${route == 'routea' ? 'a' : 'b'}-uta'
    principalId: identity.properties.principalId
  }
}]

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: 'log-${prefix}-shared'
}
resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: 'cae-${prefix}-console'
  location: location
  tags: tags
  properties: {
    vnetConfiguration: { infrastructureSubnetId: subnet.id }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

output environmentId string = environment.id
output environmentDomain string = environment.properties.defaultDomain
output registryName string = registry.name
output registryServer string = registry.properties.loginServer
output identityId string = identity.id
output identityClientId string = identity.properties.clientId
output identityPrincipalId string = identity.properties.principalId
output storageUrl string = storage.properties.primaryEndpoints.blob
