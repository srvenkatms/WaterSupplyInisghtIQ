param location string
param prefix string
param tags object
param storageAccountId string
param labVnetId string = ''
param preserveConsoleSubnet bool = false
param additionalLabVnetIds array = []
param additionalStorageClientPrefixes array = []

resource egressIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: 'pip-${prefix}-services-egress'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: { publicIPAllocationMethod: 'Static' }
}

resource nat 'Microsoft.Network/natGateways@2024-05-01' = {
  name: 'nat-${prefix}-services'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: { publicIpAddresses: [{ id: egressIp.id }] }
}

resource endpointNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-${prefix}-storage-endpoints'
  location: location
  tags: tags
  properties: {
    securityRules: concat([
      {
        name: 'allow-private-storage-clients'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefixes: [ '10.20.1.0/26', '10.10.5.0/24' ]
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
      {
        name: 'deny-other-inbound'
        properties: {
          priority: 4000
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ], preserveConsoleSubnet ? [{
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
    }] : [], !empty(additionalStorageClientPrefixes) ? [{
      name: 'allow-additional-private-storage-clients'
      properties: {
        priority: 120
        direction: 'Inbound'
        access: 'Allow'
        protocol: 'Tcp'
        sourceAddressPrefixes: additionalStorageClientPrefixes
        sourcePortRange: '*'
        destinationAddressPrefix: '10.20.2.0/27'
        destinationPortRange: '443'
      }
    }] : [])
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${prefix}-services'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.20.0.0/16'] }
    subnets: concat([
      {
        name: 'snet-functions'
        properties: {
          addressPrefix: '10.20.1.0/26'
          defaultOutboundAccess: false
          natGateway: { id: nat.id }
          delegations: [{
            name: 'flex-consumption'
            properties: { serviceName: 'Microsoft.App/environments' }
          }]
        }
      }
      {
        name: 'snet-storage'
        properties: {
          addressPrefix: '10.20.2.0/27'
          defaultOutboundAccess: false
          privateEndpointNetworkPolicies: 'Enabled'
          networkSecurityGroup: { id: endpointNsg.id }
        }
      }
    ], preserveConsoleSubnet ? [{
      name: 'snet-console'
      properties: {
        addressPrefix: '10.20.3.0/27'
        defaultOutboundAccess: false
        natGateway: { id: nat.id }
        networkSecurityGroup: { id: resourceId('Microsoft.Network/networkSecurityGroups', 'nsg-${prefix}-console') }
        delegations: [{
          name: 'container-apps'
          properties: { serviceName: 'Microsoft.App/environments' }
        }]
      }
    }] : [])
  }
}

var services = ['blob', 'queue']

resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for service in services: {
  name: 'privatelink.${service}.core.windows.net'
  location: 'global'
  tags: tags
}]

resource servicesLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (service, i) in services: {
  parent: zones[i]
  name: 'services'
  location: 'global'
  properties: { virtualNetwork: { id: vnet.id }, registrationEnabled: false }
}]

resource labLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (service, i) in services: if (!empty(labVnetId)) {
  parent: zones[i]
  name: 'utility-a'
  location: 'global'
  properties: { virtualNetwork: { id: labVnetId }, registrationEnabled: false }
}]

resource endpoints 'Microsoft.Network/privateEndpoints@2024-05-01' = [for (service, i) in services: {
  name: 'pe-${prefix}-storage-${service}'
  location: location
  tags: tags
  properties: {
    subnet: { id: '${vnet.id}/subnets/snet-storage' }
    ipConfigurations: [{
      name: service
      properties: { groupId: service, memberName: service, privateIPAddress: '10.20.2.${10 + i}' }
    }]
    privateLinkServiceConnections: [{
      name: 'storage-${service}'
      properties: { privateLinkServiceId: storageAccountId, groupIds: [service] }
    }]
  }
}]

resource zoneGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for (service, i) in services: {
  parent: endpoints[i]
  name: 'storage'
  properties: { privateDnsZoneConfigs: [{ name: service, properties: { privateDnsZoneId: zones[i].id } }] }
}]

resource labPeering 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-05-01' = if (!empty(labVnetId)) {
  parent: vnet
  name: 'utility-a'
  properties: {
    remoteVirtualNetwork: { id: labVnetId }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: false
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

resource additionalLabLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (labId, i) in additionalLabVnetIds: {
  parent: zones[0]
  name: 'additional-lab-${i}'
  location: 'global'
  properties: { virtualNetwork: { id: labId }, registrationEnabled: false }
}]

resource additionalQueueLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (labId, i) in additionalLabVnetIds: {
  parent: zones[1]
  name: 'additional-lab-${i}'
  location: 'global'
  properties: { virtualNetwork: { id: labId }, registrationEnabled: false }
}]

resource additionalLabPeerings 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-05-01' = [for (labId, i) in additionalLabVnetIds: {
  parent: vnet
  name: 'additional-lab-${i}'
  properties: {
    remoteVirtualNetwork: { id: labId }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: false
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}]

output vnetId string = vnet.id
output functionsSubnetId string = '${vnet.id}/subnets/snet-functions'
