// Utility A network for the real PLC data path: boundary, control and test
// subnets with Purdue-style NSG rules, Bastion, and optional HMI/operator VMs.
// No VM has a public IP; optional Bastion provides private SSH access.
param location string
param prefix string
param tags object
@secure()
param adminSshPublicKey string
param allowBuildOutbound bool
param deployHmi bool
param deployOperatorHost bool = false
param enableAutoShutdown bool = false
param vmSize string = 'Standard_B2s'
param adminUsername string = 'labadmin'
@description('First two octets of this isolated lab network.')
param addressPrefix string = '10.10'
@description('Allow the isolated scenario subnet to reach Modbus directly (Route A only).')
param directPlcAccess bool = true
@description('Deploy private Premium Bastion. Leave disabled for control-plane-managed Route A/B foundations.')
param deployBastion bool = true
param enableRouteRuntime bool = false

var cidr = {
  vnet: '${addressPrefix}.0.0/16'
  control: '${addressPrefix}.1.0/24'
  boundary: '${addressPrefix}.2.0/24'
  bastion: '${addressPrefix}.3.0/26'
  test: '${addressPrefix}.4.0/24'
  operator: '${addressPrefix}.5.0/24'
}
var hmiPort = '1881'
var runtimeStorageEgress = enableRouteRuntime ? [{
  name: 'allow-route-private-storage'
  properties: {
    priority: 105
    direction: 'Outbound'
    access: 'Allow'
    protocol: 'Tcp'
    sourceAddressPrefix: '*'
    sourcePortRange: '*'
    destinationAddressPrefix: '10.20.2.0/27'
    destinationPortRange: '443'
  }
}] : []

resource egressIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: 'pip-${prefix}-uta-egress'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: { publicIPAllocationMethod: 'Static' }
}

resource egressNat 'Microsoft.Network/natGateways@2024-05-01' = {
  name: 'nat-${prefix}-uta-egress'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: { publicIpAddresses: [{id: egressIp.id}] }
}

resource isolatedRoutes 'Microsoft.Network/routeTables@2024-05-01' = {
  name: 'rt-${prefix}-uta-isolated'
  location: location
  tags: tags
  properties: {
    disableBgpRoutePropagation: true
    routes: [{
      name: 'deny-public-egress'
      properties: { addressPrefix: '0.0.0.0/0', nextHopType: 'None' }
    }]
  }
}

var isolatedSubnetEgress = allowBuildOutbound
  ? {natGateway: {id: egressNat.id}}
  : {routeTable: {id: isolatedRoutes.id}}

// Temporary egress for package installs during the build (removed for TC-17).
var buildEgress = allowBuildOutbound ? [
  {
    name: 'allow-build-egress'
    properties: {
      priority: 3000
      direction: 'Outbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: '*'
      sourcePortRange: '*'
      destinationAddressPrefix: 'Internet'
      destinationPortRanges: [ '80', '443' ]
    }
  }
] : []

var denyInternetOut = {
  name: 'deny-internet-out'
  properties: {
    priority: 4000
    direction: 'Outbound'
    access: 'Deny'
    protocol: '*'
    sourceAddressPrefix: '*'
    sourcePortRange: '*'
    destinationAddressPrefix: 'Internet'
    destinationPortRange: '*'
  }
}
var denyOtherOut = {
  name: 'deny-other-out'
  properties: {
    priority: 4090
    direction: 'Outbound'
    access: 'Deny'
    protocol: '*'
    sourceAddressPrefix: '*'
    sourcePortRange: '*'
    destinationAddressPrefix: '*'
    destinationPortRange: '*'
  }
}

var sshFromBastion = {
  name: 'allow-ssh-from-bastion'
  properties: {
    priority: 100
    direction: 'Inbound'
    access: 'Allow'
    protocol: 'Tcp'
    sourceAddressPrefix: cidr.bastion
    sourcePortRange: '*'
    destinationAddressPrefix: '*'
    destinationPortRange: '22'
  }
}

var denyVnetIn = {
  name: 'deny-vnet-in'
  properties: {
    priority: 4000
    direction: 'Inbound'
    access: 'Deny'
    protocol: '*'
    sourceAddressPrefix: 'VirtualNetwork'
    sourcePortRange: '*'
    destinationAddressPrefix: '*'
    destinationPortRange: '*'
  }
}

resource nsgBoundary 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-${prefix}-uta-boundary'
  location: location
  tags: tags
  properties: {
    securityRules: concat([
      sshFromBastion
      denyVnetIn
      {
        name: 'allow-modbus-to-control'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: cidr.control
          destinationPortRange: '502'
        }
      }
      {
        name: 'allow-azure-monitor'
        properties: {
          priority: 110
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureMonitor'
          destinationPortRange: '443'
        }
      }
      {
        name: 'allow-entra'
        properties: {
          priority: 120
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureActiveDirectory'
          destinationPortRange: '443'
        }
      }
      denyInternetOut
      denyOtherOut
    ], buildEgress, runtimeStorageEgress)
  }
}

resource nsgControl 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-${prefix}-uta-control'
  location: location
  tags: tags
  properties: {
    securityRules: concat([
      sshFromBastion
      {
        name: 'allow-modbus-from-boundary-and-test'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefixes: directPlcAccess ? [ cidr.boundary, cidr.test ] : [ cidr.boundary ]
          sourcePortRange: '*'
          destinationAddressPrefix: cidr.control
          destinationPortRange: '502'
        }
      }
      {
        name: 'allow-hmi-from-test'
        properties: {
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: cidr.test
          sourcePortRange: '*'
          destinationAddressPrefix: cidr.control
          destinationPortRange: hmiPort
        }
      }
      {
        name: 'allow-within-control-in'
        properties: {
          priority: 130
          direction: 'Inbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: cidr.control
          sourcePortRange: '*'
          destinationAddressPrefix: cidr.control
          destinationPortRange: '*'
        }
      }
      denyVnetIn
      {
        name: 'allow-within-control-out'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: cidr.control
          sourcePortRange: '*'
          destinationAddressPrefix: cidr.control
          destinationPortRange: '*'
        }
      }
      {
        name: 'deny-vnet-out'
        properties: {
          priority: 3900
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '*'
        }
      }
      denyInternetOut
      denyOtherOut
    ], buildEgress)
  }
}

resource nsgTest 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-${prefix}-uta-test'
  location: location
  tags: tags
  properties: {
    securityRules: concat([
      sshFromBastion
      denyVnetIn
      {
        name: 'allow-modbus-and-hmi-to-control'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: cidr.control
          destinationPortRanges: directPlcAccess ? [ '502', hmiPort ] : [ hmiPort ]
        }
      }
      denyInternetOut
      denyOtherOut
    ], buildEgress, runtimeStorageEgress)
  }
}

var operatorAzureEgress = [for (service, i) in ['AzureResourceManager', 'AzureMonitor', 'AzureActiveDirectory', 'Storage']: {
  name: 'allow-${toLower(service)}'
  properties: {
    priority: 110 + i
    direction: 'Outbound'
    access: 'Allow'
    protocol: 'Tcp'
    sourceAddressPrefix: '*'
    sourcePortRange: '*'
    destinationAddressPrefix: service
    destinationPortRange: '443'
  }
}]

resource nsgOperator 'Microsoft.Network/networkSecurityGroups@2024-05-01' = if (deployOperatorHost) {
  name: 'nsg-${prefix}-uta-operator'
  location: location
  tags: tags
  properties: {
    securityRules: concat([
      sshFromBastion
      denyVnetIn
      {
        name: 'allow-private-bastion'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: cidr.bastion
          destinationPortRange: '443'
        }
      }
      {
        name: 'allow-private-storage'
        properties: {
          priority: 105
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '10.20.2.0/27'
          destinationPortRange: '443'
        }
      }
    ], operatorAzureEgress, [denyInternetOut, denyOtherOut], buildEgress)
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${prefix}-uta'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [ cidr.vnet ] }
    subnets: concat([
      { name: 'snet-control', properties: union({ addressPrefix: cidr.control, defaultOutboundAccess: false, networkSecurityGroup: { id: nsgControl.id } }, isolatedSubnetEgress) }
      { name: 'snet-boundary', properties: { addressPrefix: cidr.boundary, defaultOutboundAccess: false, natGateway: {id: egressNat.id}, networkSecurityGroup: { id: nsgBoundary.id } } }
      { name: 'AzureBastionSubnet', properties: { addressPrefix: cidr.bastion } }
      { name: 'snet-test', properties: union({ addressPrefix: cidr.test, defaultOutboundAccess: false, networkSecurityGroup: { id: nsgTest.id } }, isolatedSubnetEgress) }
    ], deployOperatorHost ? [
      { name: 'snet-operator', properties: { addressPrefix: cidr.operator, defaultOutboundAccess: false, natGateway: {id: egressNat.id}, networkSecurityGroup: {id: nsgOperator!.id} } }
    ] : [])
  }
}

resource bastion 'Microsoft.Network/bastionHosts@2024-05-01' = if (deployBastion) {
  name: 'bas-${prefix}-uta'
  location: location
  tags: tags
  sku: { name: 'Premium' }
  properties: {
    enablePrivateOnlyBastion: true
    enableTunneling: true
    ipConfigurations: [
      {
        name: 'ipconfig'
        properties: {
          subnet: { id: '${vnet.id}/subnets/AzureBastionSubnet' }
        }
      }
    ]
  }
}

// Docker is installed at first boot; OpenPLC, the plant simulator, the HMI
// and the gateway run as containers deployed by scripts/deploy-apps.sh.
var cloudInit = base64('''#cloud-config
package_update: true
packages:
  - docker.io
runcmd:
  - systemctl enable --now docker
''')

var vms = concat([
  { name: 'vm-uta-gw', subnet: 'snet-boundary', ip: '${addressPrefix}.2.10', role: 'Edge gateway', identity: true }
  { name: 'vm-uta-plc', subnet: 'snet-control', ip: '${addressPrefix}.1.10', role: 'PLC and plant simulator', identity: false }
  { name: 'vm-uta-attack', subnet: 'snet-test', ip: '${addressPrefix}.4.10', role: 'Scenario actions', identity: enableRouteRuntime }
], deployHmi ? [
  { name: 'vm-uta-hmi', subnet: 'snet-control', ip: '${addressPrefix}.1.11', role: 'HMI', identity: false }
] : [], deployOperatorHost ? [
  { name: 'vm-uta-operator', subnet: 'snet-operator', ip: '${addressPrefix}.5.10', role: 'Private acceptance operator', identity: true }
] : [])

resource nics 'Microsoft.Network/networkInterfaces@2024-05-01' = [for v in vms: {
  name: 'nic-${v.name}'
  location: location
  tags: union(tags, { role: v.role })
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig'
        properties: {
          subnet: { id: '${vnet.id}/subnets/${v.subnet}' }
          privateIPAllocationMethod: 'Static'   // apps and profiles use these fixed addresses
          privateIPAddress: v.ip
        }
      }
    ]
  }
}]

resource vmRes 'Microsoft.Compute/virtualMachines@2024-07-01' = [for (v, i) in vms: {
  name: v.name
  location: location
  tags: union(tags, { role: v.role })
  identity: v.identity ? { type: 'SystemAssigned' } : null
  properties: {
    hardwareProfile: { vmSize: vmSize }
    osProfile: {
      computerName: v.name
      adminUsername: adminUsername
      customData: cloudInit
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            { path: '/home/${adminUsername}/.ssh/authorized_keys', keyData: adminSshPublicKey }
          ]
        }
      }
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: '0001-com-ubuntu-server-jammy'
        sku: '22_04-lts-gen2'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        managedDisk: { storageAccountType: 'StandardSSD_LRS' }
        deleteOption: 'Delete'
      }
    }
    networkProfile: {
      networkInterfaces: [ { id: nics[i].id, properties: { deleteOption: 'Delete' } } ]
    }
  }
}]

resource autoShutdown 'Microsoft.DevTestLab/schedules@2018-09-15' = [for (v, i) in vms: {
  name: 'shutdown-computevm-${v.name}'
  location: location
  tags: tags
  properties: {
    status: enableAutoShutdown ? 'Enabled' : 'Disabled'
    taskType: 'ComputeVmShutdownTask'
    dailyRecurrence: { time: '1900' }
    timeZoneId: 'Pacific Standard Time'
    targetResourceId: vmRes[i].id
  }
}]

output gatewayPrincipalId string = vmRes[0].identity.principalId
output runnerPrincipalId string = enableRouteRuntime ? vmRes[2].identity.principalId : ''
output vnetName string = vnet.name
output vnetId string = vnet.id
output operatorPrincipalId string = deployOperatorHost ? vmRes[length(vms) - 1].identity.principalId : ''
output machines array = [for (v, i) in vms: {
  name: v.name
  id: vmRes[i].id
  ip: v.ip
}]

resource operatorNetworkReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployOperatorHost) {
  name: guid(resourceGroup().id, vmRes[length(vms) - 1].id, 'operator-network-reader')
  properties: {
    principalId: vmRes[length(vms) - 1].identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'acdd72a7-3385-48ef-bd42-f606fba81ae7')
  }
}
