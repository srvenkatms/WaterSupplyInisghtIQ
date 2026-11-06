// Small Water Utility Cyber Lab - Level 1
// Deploys the shared services, one Sentinel workspace per simulated utility,
// Utility A's network for the real PLC data path, and a cost budget.
targetScope = 'subscription'

@description('Azure region for every lab resource.')
param location string = 'eastus2'

@description('Short prefix used in every resource name.')
@maxLength(8)
param prefix string = 'swulab'

@description('Simulated utilities. Utility A also gets the network and real PLC path.')
param utilities array = loadJsonContent('utility-config.json')

@description('Owner tag value.')
param owner string

@description('Cost center tag value.')
param costCenter string = 'lab'

@description('SSH public key for lab VMs (admin access through Bastion only).')
@secure()
param adminSshPublicKey string

@description('Deploy Utility A virtual network, VMs and Bastion.')
param deployUtilityANetwork bool = true

@description('Allow temporary internet egress from VM subnets while software is installed. Set false before testing (TC-17).')
param allowBuildOutbound bool = true

@description('Deploy the optional HMI VM.')
param deployHmi bool = true

@description('Deploy a private acceptance operator with managed identity and Bastion-backed SSH. No public VM IP or direct OT access.')
param deployOperatorHost bool = false

@description('Preserve the separately deployed Container Apps console subnet and private storage access.')
param preserveConsoleSubnet bool = false

@description('VM size for Utility A; select a size available in the approved region/subscription.')
@minLength(1)
param utilityAVmSize string = 'Standard_B2s'

@description('Start time for never-seen source health checks.')
param deployedAt string = utcNow('yyyy-MM-ddTHH:mm:ssZ')

@description('Enable nightly VM shutdown only outside warm-up and acceptance windows.')
param enableAutoShutdown bool = false

@description('Object ID of the tester (user or group) who runs the harness; empty to skip (SEC-05).')
param testerPrincipalId string = ''

@description('Principal type of testerPrincipalId.')
@allowed([ 'User', 'Group', 'ServicePrincipal' ])
param testerPrincipalType string = 'User'

@description('Deploy the optional Teams notification playbook (AI-04, AI-05).')
param deployTeamsPlaybook bool = false

@description('Teams team ID and channel ID for the playbook.')
param teamsTeamId string = ''
param teamsChannelId string = ''

@description('Monthly budget amount in the billing currency.')
param budgetAmount int = 300

@description('Budget start date, first day of a month, yyyy-MM-01.')
param budgetStartDate string

@description('Email addresses for budget alerts.')
param budgetContactEmails array

var tags = {
  project: 'swu-lab'
  owner: owner
  costcenter: costCenter
  deployment: prefix
}
var utilityResourceGroups = [for u in utilities: 'rg-${prefix}-${u.id}']
var utilityAIndex = indexOf(map(utilities, u => u.id), 'uta')

resource rgShared 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${prefix}-shared'
  location: location
  tags: union(tags, { utility: 'shared' })
}

resource rgUtility 'Microsoft.Resources/resourceGroups@2024-03-01' = [for u in utilities: {
  name: 'rg-${prefix}-${u.id}'
  location: location
  tags: union(tags, { utility: u.id })
}]

module shared 'modules/shared.bicep' = {
  scope: rgShared
  name: 'shared'
  params: {
    location: location
    prefix: prefix
    tags: union(tags, { utility: 'shared' })
    testerPrincipalId: testerPrincipalId
    testerPrincipalType: testerPrincipalType
    alertEmails: budgetContactEmails
    labVnetId: deployUtilityANetwork ? networkA!.outputs.vnetId : ''
    preserveConsoleSubnet: preserveConsoleSubnet
  }
}

module utility 'modules/utility.bicep' = [for u in utilities: {
  scope: resourceGroup('rg-${prefix}-${u.id}')
  name: 'utility-${u.id}'
  dependsOn: [ rgUtility ]
  params: {
    location: location
    prefix: prefix
    utilityId: u.id
    utilityName: u.name
    tags: union(tags, { utility: u.id })
    dceId: shared.outputs.dceId
    generatorPrincipalId: shared.outputs.generatorPrincipalId
    agentPrincipalId: shared.outputs.agentPrincipalId
    testerPrincipalId: testerPrincipalId
    testerPrincipalType: testerPrincipalType
    utilityProfile: u
    deployedAt: deployedAt
  }
}]

module playbook 'modules/playbook.bicep' = if (deployTeamsPlaybook) {
  scope: rgShared
  name: 'playbook'
  params: {
    location: location
    prefix: prefix
    tags: union(tags, { utility: 'shared' })
    teamsTeamId: teamsTeamId
    teamsChannelId: teamsChannelId
  }
}

// Sentinel may run the playbook from each utility workspace.
module automation 'modules/automation.bicep' = [for (u, i) in utilities: if (deployTeamsPlaybook) {
  scope: resourceGroup('rg-${prefix}-${u.id}')
  name: 'automation-${u.id}'
  dependsOn: [ rgUtility, utility[i] ]
  params: {
    workspaceName: 'law-${prefix}-${u.id}'
    playbookId: deployTeamsPlaybook ? playbook!.outputs.playbookId : ''
    playbookPrincipalId: deployTeamsPlaybook ? playbook!.outputs.playbookPrincipalId : ''
  }
}]

module networkA 'modules/network.bicep' = if (deployUtilityANetwork) {
  scope: resourceGroup(utilityResourceGroups[utilityAIndex])
  name: 'network-uta'
  dependsOn: [ rgUtility ]
  params: {
    location: location
    prefix: prefix
    tags: union(tags, { utility: 'uta' })
    adminSshPublicKey: adminSshPublicKey
    allowBuildOutbound: allowBuildOutbound
    deployHmi: deployHmi
    deployOperatorHost: deployOperatorHost
    vmSize: utilityAVmSize
    enableAutoShutdown: enableAutoShutdown
  }
}

// The edge gateway may write only to Utility A's data collection rule.
module servicesPeer 'modules/services-peer.bicep' = if (deployUtilityANetwork) {
  scope: resourceGroup(utilityResourceGroups[utilityAIndex])
  name: 'private-services-peer'
  params: {
    vnetName: networkA!.outputs.vnetName
    servicesVnetId: shared.outputs.servicesVnetId
  }
}

module gatewayRole 'modules/dcr-role.bicep' = if (deployUtilityANetwork) {
  scope: resourceGroup(utilityResourceGroups[utilityAIndex])
  name: 'gateway-dcr-role'
  dependsOn: [ rgUtility ]
  params: {
    dcrName: utility[utilityAIndex].outputs.dcrName
    principalId: deployUtilityANetwork ? networkA!.outputs.gatewayPrincipalId : ''
  }
}

module budget 'modules/budget.bicep' = {
  name: 'budget'
  params: {
    prefix: prefix
    amount: budgetAmount
    startDate: budgetStartDate
    contactEmails: budgetContactEmails
    resourceGroups: concat([rgShared.name], utilityResourceGroups)
  }
}

module operatorSharedRole 'modules/operator-role.bicep' = if (deployUtilityANetwork && deployOperatorHost) {
  scope: rgShared
  name: 'operator-shared-role'
  params: {
    workspaceName: shared.outputs.sharedWorkspaceName
    principalId: networkA!.outputs.operatorPrincipalId
    queueStorageAccountName: shared.outputs.storageAccountName
  }
}

module operatorUtilityRole 'modules/operator-role.bicep' = [for (u, i) in utilities: if (deployUtilityANetwork && deployOperatorHost) {
  scope: resourceGroup('rg-${prefix}-${u.id}')
  name: 'operator-role-${u.id}'
  dependsOn: [rgUtility, utility[i]]
  params: {
    workspaceName: 'law-${prefix}-${u.id}'
    principalId: networkA!.outputs.operatorPrincipalId
    incidentResponse: true
  }
}]

output functionAppName string = shared.outputs.functionAppName
output dceEndpoint string = shared.outputs.dceEndpoint
output scenarioQueueUrl string = shared.outputs.scenarioQueueUrl
output sharedResourceGroup string = rgShared.name
output sharedWorkspaceId string = shared.outputs.sharedWorkspaceId
output approvedBudgetAmount int = budgetAmount
output resourceGroups array = concat([rgShared.name], utilityResourceGroups)
output prefix string = prefix
output gatewayVmName string = 'vm-uta-gw'
output attackerVmName string = 'vm-uta-attack'
output utilityAResourceGroup string = 'rg-${prefix}-uta'
output hmiIp string = deployHmi ? '10.10.1.11' : '10.10.1.10'
output utilityAVmSize string = utilityAVmSize
output operatorVmName string = deployUtilityANetwork && deployOperatorHost ? 'vm-uta-operator' : ''
output operatorPrincipalId string = deployUtilityANetwork && deployOperatorHost ? networkA!.outputs.operatorPrincipalId : ''
output deployedAt string = deployedAt
output agentClientId string = shared.outputs.agentClientId
output utilityIngestion array = [for (u, i) in utilities: {
  utilityId: u.id
  workspaceId: utility[i].outputs.workspaceCustomerId
  dcrImmutableId: utility[i].outputs.dcrImmutableId
  agentDcrImmutableId: utility[i].outputs.agentDcrImmutableId
  workspaceResourceId: utility[i].outputs.workspaceResourceId
  processSource: u.processSource
}]
