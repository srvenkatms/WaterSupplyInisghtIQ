// One simulated utility: Sentinel workspace, custom tables, data collection
// rules, reader access for agents, and detection content.
param location string
param prefix string
param utilityId string
param utilityName string
param tags object
param dceId string
param generatorPrincipalId string
param agentPrincipalId string
param testerPrincipalId string = ''
param testerPrincipalType string = 'User'
param utilityProfile object
param deployedAt string
param deployAnalyticsRules bool = true

@description('Daily ingestion cap in GB (NFR-04).')
param dailyQuotaGb int = 1

@description('Interactive retention in days for detection data.')
param retentionInDays int = 90

var allTables = loadJsonContent('../tables.json')
var tables = filter(allTables, t => t.name != 'SimAgentAction_CL')
var commonColumns = [
  { name: 'TimeGenerated', type: 'datetime' }
  { name: 'UtilityId', type: 'string' }
  { name: 'ScenarioRunId', type: 'string' } // FR-23: test correlation only, never used by rules
  { name: 'RecordId', type: 'string' }
]
var agentTable = filter(allTables, t => t.name == 'SimAgentAction_CL')[0]

var roles = {
  monitoringMetricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
  logAnalyticsReader: '73c42c96-874c-492b-b04d-ab87d138a893'
  sentinelReader: '8d289c81-5878-46d4-8554-54e1e3d8b5cb'
}

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-${prefix}-${utilityId}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: retentionInDays
    workspaceCapping: { dailyQuotaGb: dailyQuotaGb }
  }
}

resource sentinel 'Microsoft.SecurityInsights/onboardingStates@2024-03-01' = {
  scope: law
  name: 'default'
  properties: {}
}

resource simTables 'Microsoft.OperationalInsights/workspaces/tables@2022-10-01' = [for t in allTables: {
  parent: law
  name: t.name
  properties: {
    schema: {
      name: t.name
      columns: concat(commonColumns, t.columns)
    }
    retentionInDays: retentionInDays
  }
}]

// Main DCR: one stream per Sim table, written by the generator (and, for
// Utility A, by the edge gateway).
resource dcr 'Microsoft.Insights/dataCollectionRules@2022-06-01' = {
  name: 'dcr-${prefix}-${utilityId}'
  location: location
  tags: tags
  dependsOn: [ simTables ]
  properties: {
    dataCollectionEndpointId: dceId
    streamDeclarations: toObject(tables, t => 'Custom-${t.name}', t => {
      columns: concat(commonColumns, t.columns)
    })
    destinations: {
      logAnalytics: [ { name: 'law', workspaceResourceId: law.id } ]
    }
    dataFlows: [for t in tables: {
      streams: [ 'Custom-${t.name}' ]
      destinations: [ 'law' ]
      transformKql: 'source | where UtilityId == "${utilityId}"'
      outputStream: 'Custom-${t.name}'
    }]
  }
}

// Separate DCR for agent action logs, so agents can write only here (AI-03).
resource agentDcr 'Microsoft.Insights/dataCollectionRules@2022-06-01' = {
  name: 'dcr-${prefix}-${utilityId}-agent'
  location: location
  tags: tags
  dependsOn: [ simTables ]
  properties: {
    dataCollectionEndpointId: dceId
    streamDeclarations: {
      'Custom-SimAgentAction_CL': { columns: concat(commonColumns, agentTable.columns) }
    }
    destinations: {
      logAnalytics: [ { name: 'law', workspaceResourceId: law.id } ]
    }
    dataFlows: [
      {
        streams: [ 'Custom-SimAgentAction_CL' ]
        destinations: [ 'law' ]
        transformKql: 'source | where UtilityId == "${utilityId}"'
        outputStream: 'Custom-SimAgentAction_CL'
      }
    ]
  }
}

resource generatorWrite 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: dcr
  name: guid(dcr.id, generatorPrincipalId, roles.monitoringMetricsPublisher)
  properties: {
    principalId: generatorPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringMetricsPublisher)
  }
}

resource agentWrite 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: agentDcr
  name: guid(agentDcr.id, agentPrincipalId, roles.monitoringMetricsPublisher)
  properties: {
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringMetricsPublisher)
  }
}

resource agentLaReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: law
  name: guid(law.id, agentPrincipalId, roles.logAnalyticsReader)
  properties: {
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.logAnalyticsReader)
  }
}

resource agentSentinelReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: law
  name: guid(law.id, agentPrincipalId, roles.sentinelReader)
  properties: {
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.sentinelReader)
  }
}


resource testerLaReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(testerPrincipalId)) {
  scope: law
  name: guid(law.id, testerPrincipalId, '73c42c96-874c-492b-b04d-ab87d138a893')
  properties: {
    principalId: testerPrincipalId
    principalType: testerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '73c42c96-874c-492b-b04d-ab87d138a893')
  }
}

resource testerResponder 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(testerPrincipalId)) {
  scope: law
  name: guid(law.id, testerPrincipalId, '3e150937-b8fe-4cfb-8069-0eaf05ecd056')
  properties: {
    principalId: testerPrincipalId
    principalType: testerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3e150937-b8fe-4cfb-8069-0eaf05ecd056')
  }
}

// HM-02 health workbook
module workbook 'workbook.bicep' = {
  name: 'workbook-${utilityId}'
  dependsOn: [ simTables ]
  params: {
    location: location
    workspaceId: law.id
    utilityName: utilityName
    tags: tags
    healthSources: utilityProfile.healthSources
  }
}

module content 'content.bicep' = {
  name: 'content-${utilityId}'
  dependsOn: [ sentinel, simTables ]
  params: {
    workspaceName: law.name
    utilityId: utilityId
    utilityName: utilityName
    watchlists: utilityProfile.watchlists
    healthSources: utilityProfile.healthSources
    deployedAt: deployedAt
    deployAnalyticsRules: deployAnalyticsRules
  }
}

resource disabledAccounts 'Microsoft.SecurityInsights/watchlists@2024-03-01' existing = {
  scope: law
  name: 'DisabledAccounts'
}

resource accountReferenceWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: disabledAccounts
  name: guid(disabledAccounts.id, generatorPrincipalId, 'account-state-writer')
  dependsOn: [ content ]
  properties: {
    principalId: generatorPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ab8e14d6-4a74-4a29-9ba8-549422addade')
  }
}

resource generatorHistoryReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: law
  name: guid(law.id, generatorPrincipalId, roles.logAnalyticsReader)
  properties: {
    principalId: generatorPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.logAnalyticsReader)
  }
}

resource historyLists 'Microsoft.SecurityInsights/watchlists@2024-03-01' existing = [for alias in ['SourceReceipts', 'AccessHistory', 'IncidentHistory']: {
  scope: law
  name: alias
}]

resource historyReferenceWriters 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (alias, i) in ['SourceReceipts', 'AccessHistory', 'IncidentHistory']: {
  scope: historyLists[i]
  name: guid(historyLists[i].id, generatorPrincipalId, 'history-reference-writer')
  dependsOn: [ content ]
  properties: {
    principalId: generatorPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ab8e14d6-4a74-4a29-9ba8-549422addade')
  }
}]

output workspaceCustomerId string = law.properties.customerId
output workspaceResourceId string = law.id
output dcrName string = dcr.name
output dcrImmutableId string = dcr.properties.immutableId
output agentDcrImmutableId string = agentDcr.properties.immutableId
