param vnetName string
param servicesVnetId string

resource labVnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: vnetName
}

resource peer 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-05-01' = {
  parent: labVnet
  name: 'private-services'
  properties: {
    remoteVirtualNetwork: { id: servicesVnetId }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: false
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}
