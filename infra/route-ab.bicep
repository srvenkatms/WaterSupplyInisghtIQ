targetScope = 'subscription'

param location string = 'eastus2'
@minLength(3)
@maxLength(6)
param prefix string = 'swurab'
param owner string
@secure()
param adminSshPublicKey string
param vmSize string = 'Standard_E2as_v6'
param testerPrincipalId string
param allowBuildOutbound bool = true
param deployAnalyticsRules bool = true
param enableRouteRuntime bool = false
param preserveConsoleSubnet bool = false
param deployedAt string = utcNow('yyyy-MM-ddTHH:mm:ssZ')

var tags = {
  project: 'swu-lab'
  deployment: prefix
  owner: owner
  environment: 'route-ab'
}
var routes = [
  { id: 'routea', suffix: 'a', network: '10.30', directPlcAccess: true, name: 'Route A - Direct PLC access' }
  { id: 'routeb', suffix: 'b', network: '10.40', directPlcAccess: false, name: 'Route B - HMI-only PLC access' }
]
// Both routes represent the same simulated Utility A, in separate workspaces.
var utilityProfile = loadJsonContent('utility-config.json')[0]
var routeResourceGroupNames = [for route in routes: 'rg-${prefix}-${route.id}']

resource sharedGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${prefix}-shared'
  location: location
  tags: tags
}

resource routeGroups 'Microsoft.Resources/resourceGroups@2024-03-01' = [for route in routes: {
  name: 'rg-${prefix}-${route.id}'
  location: location
  tags: union(tags, { route: route.id })
}]

module networks 'modules/network.bicep' = [for route in routes: {
  name: 'network-${route.id}'
  scope: resourceGroup('rg-${prefix}-${route.id}')
  dependsOn: [routeGroups]
  params: {
    location: location
    prefix: '${prefix}${route.suffix}'
    tags: union(tags, { route: route.id })
    adminSshPublicKey: adminSshPublicKey
    addressPrefix: route.network
    directPlcAccess: route.directPlcAccess
    deployBastion: false
    deployHmi: true
    deployOperatorHost: true
    allowBuildOutbound: allowBuildOutbound
    vmSize: vmSize
    enableRouteRuntime: enableRouteRuntime
  }
}]

module services 'modules/route-ab-services.bicep' = {
  name: 'route-ab-services'
  scope: sharedGroup
  params: {
    location: location
    prefix: prefix
    tags: tags
    routeVnetIds: [networks[0].outputs.vnetId, networks[1].outputs.vnetId]
    enableRouteRuntime: enableRouteRuntime
    preserveConsoleSubnet: preserveConsoleSubnet
    runnerPrincipalIds: [networks[0].outputs.runnerPrincipalId, networks[1].outputs.runnerPrincipalId]
    gatewayPrincipalIds: [networks[0].outputs.gatewayPrincipalId, networks[1].outputs.gatewayPrincipalId]
  }
}

module peers 'modules/services-peer.bicep' = [for (route, i) in routes: {
  name: 'services-peer-${route.id}'
  scope: resourceGroup('rg-${prefix}-${route.id}')
  dependsOn: [routeGroups]
  params: {
    vnetName: networks[i].outputs.vnetName
    servicesVnetId: services.outputs.servicesVnetId
  }
}]

module telemetry 'modules/utility.bicep' = [for (route, i) in routes: {
  name: 'telemetry-${route.id}'
  scope: resourceGroup('rg-${prefix}-${route.id}')
  dependsOn: [routeGroups]
  params: {
    location: location
    prefix: '${prefix}${route.suffix}'
    utilityId: 'uta'
    utilityName: 'Utility A - ${route.name}'
    tags: union(tags, { route: route.id })
    dceId: services.outputs.dceId
    generatorPrincipalId: services.outputs.collectorPrincipalId
    agentPrincipalId: services.outputs.observerPrincipalId
    testerPrincipalId: testerPrincipalId
    utilityProfile: utilityProfile
    deployedAt: deployedAt
    deployAnalyticsRules: deployAnalyticsRules
  }
}]

module gatewayRoles 'modules/dcr-role.bicep' = [for (route, i) in routes: {
  name: 'gateway-role-${route.id}'
  scope: resourceGroup('rg-${prefix}-${route.id}')
  dependsOn: [routeGroups]
  params: {
    dcrName: telemetry[i].outputs.dcrName
    principalId: networks[i].outputs.gatewayPrincipalId
  }
}]

module operatorRoles 'modules/operator-role.bicep' = [for (route, i) in routes: {
  name: 'operator-role-${route.id}'
  scope: resourceGroup('rg-${prefix}-${route.id}')
  params: {
    workspaceName: 'law-${prefix}${route.suffix}-uta'
    principalId: networks[i].outputs.operatorPrincipalId
  }
  dependsOn: [telemetry[i]]
}]

output subscriptionId string = subscription().subscriptionId
output tenantId string = subscription().tenantId
output prefix string = prefix
output location string = location
output deployedAt string = deployedAt
output vmSize string = vmSize
output buildOutboundEnabled bool = allowBuildOutbound
output analyticsRulesEnabled bool = deployAnalyticsRules
output runtimeInfrastructureEnabled bool = enableRouteRuntime
output sharedResourceGroup string = sharedGroup.name
output resourceGroups array = concat([sharedGroup.name], routeResourceGroupNames)
output storageAccountName string = services.outputs.storageAccountName
output servicesVnetId string = services.outputs.servicesVnetId
output dceEndpoint string = services.outputs.dceEndpoint
output routes array = [for (route, i) in routes: {
  id: route.id
  resourceGroup: routeGroups[i].name
  prefix: '${prefix}${route.suffix}'
  utilityId: 'uta'
  directPlcAccess: route.directPlcAccess
  vnetId: networks[i].outputs.vnetId
  machines: networks[i].outputs.machines
  workspaceId: telemetry[i].outputs.workspaceCustomerId
  workspaceResourceId: telemetry[i].outputs.workspaceResourceId
  dcrImmutableId: telemetry[i].outputs.dcrImmutableId
}]
