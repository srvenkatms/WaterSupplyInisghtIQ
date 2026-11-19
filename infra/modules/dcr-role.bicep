// Grants a principal permission to send data to one data collection rule.
param dcrName string
param principalId string

var monitoringMetricsPublisher = '3913510d-42f4-4e42-8a64-420c390055eb'

resource dcr 'Microsoft.Insights/dataCollectionRules@2022-06-01' existing = {
  name: dcrName
}

resource assignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: dcr
  name: guid(dcr.id, principalId, monitoringMetricsPublisher)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringMetricsPublisher)
  }
}
