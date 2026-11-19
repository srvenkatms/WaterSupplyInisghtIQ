// Shared lab services: generator function app, scenario queue, Key Vault,
// Application Insights, data collection endpoint and the agent identity.
param location string
param prefix string
param tags object
param testerPrincipalId string = ''
param testerPrincipalType string = 'User'
param alertEmails array
param labVnetId string = ''
param preserveConsoleSubnet bool = false

var suffix = uniqueString(resourceGroup().id)
var storageName = take('st${prefix}${suffix}', 24)

// Built-in role definition IDs
var roles = {
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  storageQueueDataContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
}

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${prefix}-shared'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: 1 }
  }
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${prefix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
  }
}

resource st 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false // identity-based access only (SEC-01)
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Disabled'
  }
}

resource blobSvc 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: st
  name: 'default'
}

resource deployContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobSvc
  name: 'deployments'
}

module privateServices 'private-services.bicep' = {
  name: 'private-services'
  params: {
    location: location
    prefix: prefix
    tags: tags
    storageAccountId: st.id
    preserveConsoleSubnet: preserveConsoleSubnet
    labVnetId: labVnetId
  }
}

resource queueSvc 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  parent: st
  name: 'default'
}

resource scenarioQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueSvc
  name: 'scenarios'
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: take('kv-${prefix}-${suffix}', 24)
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    publicNetworkAccess: 'Disabled'
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

resource dce 'Microsoft.Insights/dataCollectionEndpoints@2022-06-01' = {
  name: 'dce-${prefix}'
  location: location
  tags: tags
  properties: {
    networkAcls: { publicNetworkAccess: 'Enabled' }
  }
}

resource agentId 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-agents'
  location: location
  tags: tags
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'asp-${prefix}-gen'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
}

resource func 'Microsoft.Web/sites@2024-04-01' = {
  name: 'func-${prefix}-gen-${suffix}'
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    virtualNetworkSubnetId: privateServices.outputs.functionsSubnetId
    httpsOnly: true
    siteConfig: {
      ipSecurityRestrictions: [
        { ipAddress: 'Any', action: 'Deny', priority: 2147483647, name: 'Deny public ingress' }
      ]
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: st.name }
        { name: 'AzureWebJobsStorage__queueServiceUri', value: st.properties.primaryEndpoints.queue }
        { name: 'AzureWebJobsStorage__blobServiceUri', value: st.properties.primaryEndpoints.blob }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appi.properties.ConnectionString }
        { name: 'SWU_DCE_ENDPOINT', value: dce.properties.logsIngestion.endpoint }
        { name: 'SWU_SCENARIO_QUEUE', value: scenarioQueue.name }
        { name: 'SWU_KEYVAULT_URI', value: kv.properties.vaultUri }
        { name: 'SWU_TIME_ACCELERATION', value: '1' } // FR-07: off by default
      ]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${st.properties.primaryEndpoints.blob}${deployContainer.name}'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: { name: 'python', version: '3.11' }
    }
  }
}

resource alerts 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-${prefix}-generator'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'swugen'
    enabled: true
    emailReceivers: [for (email, index) in alertEmails: {
      name: 'lab-owner-${index}'
      emailAddress: email
      useCommonAlertSchema: true
    }]
  }
}

resource generationFailure 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = {
  name: 'alert-${prefix}-generation-failure'
  location: location
  tags: tags
  properties: {
    displayName: 'Repeated lab generator failures'
    severity: 1
    enabled: true
    scopes: [law.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      allOf: [{
        query: 'AppTraces | where Message has "swu-generate failed" or Message has "swu-history failed" | summarize Failures=count()'
        timeAggregation: 'Maximum'
        metricMeasureColumn: 'Failures'
        operator: 'GreaterThanOrEqual'
        threshold: 3
        failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
      }]
    }
    actions: { actionGroups: [alerts.id] }
  }
}

resource funcBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: st
  name: guid(st.id, func.id, roles.storageBlobDataOwner)
  properties: {
    principalId: func.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataOwner)
  }
}

resource funcQueueRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: st
  name: guid(st.id, func.id, roles.storageQueueDataContributor)
  properties: {
    principalId: func.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageQueueDataContributor)
  }
}

resource funcKvRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kv
  name: guid(kv.id, func.id, roles.keyVaultSecretsUser)
  properties: {
    principalId: func.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
  }
}


resource testerQueueSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(testerPrincipalId)) {
  scope: st
  name: guid(st.id, testerPrincipalId, 'c6a89b2d-59bc-44d0-9896-0f6e12d7b80a')
  properties: {
    principalId: testerPrincipalId
    principalType: testerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'c6a89b2d-59bc-44d0-9896-0f6e12d7b80a')
  }
}

resource testerSharedReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(testerPrincipalId)) {
  scope: law
  name: guid(law.id, testerPrincipalId, '73c42c96-874c-492b-b04d-ab87d138a893')
  properties: {
    principalId: testerPrincipalId
    principalType: testerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '73c42c96-874c-492b-b04d-ab87d138a893')
  }
}

output sharedWorkspaceId string = law.properties.customerId
output sharedWorkspaceName string = law.name
output servicesVnetId string = privateServices.outputs.vnetId
output storageAccountName string = st.name
output dceId string = dce.id
output dceEndpoint string = dce.properties.logsIngestion.endpoint
output generatorPrincipalId string = func.identity.principalId
output agentPrincipalId string = agentId.properties.principalId
output agentClientId string = agentId.properties.clientId
output functionAppName string = func.name
output scenarioQueueUrl string = '${st.properties.primaryEndpoints.queue}${scenarioQueue.name}'
