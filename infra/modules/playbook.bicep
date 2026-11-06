// AI-04, AI-05: optional Teams playbook. Posts each High incident as a
// four-part card and records the responder's choice as an incident comment.
// After deployment, open the 'teams' API connection and authorize it once.
param location string
param prefix string
param tags object
param teamsTeamId string
param teamsChannelId string

var card = {
  type: 'AdaptiveCard'
  version: '1.4'
  '$schema': 'http://adaptivecards.io/schemas/adaptive-card.json'
  body: [
    { type: 'TextBlock', size: 'Large', weight: 'Bolder', wrap: true, text: '@{triggerBody()?[\'object\']?[\'properties\']?[\'title\']}' }
    { type: 'TextBlock', weight: 'Bolder', text: 'What we see' }
    { type: 'TextBlock', wrap: true, text: '@{triggerBody()?[\'object\']?[\'properties\']?[\'description\']}' }
    { type: 'TextBlock', weight: 'Bolder', text: 'Why it matters' }
    { type: 'TextBlock', wrap: true, text: 'This matches a pattern used in real attacks on small water utilities.' }
    { type: 'TextBlock', weight: 'Bolder', text: 'What to do now' }
    { type: 'TextBlock', wrap: true, text: 'Check the plant values on the HMI. If this was not you or an approved vendor, switch to manual operation and choose Not me.' }
  ]
  actions: [
    { type: 'Action.Submit', title: 'It was me', data: { response: 'It was me' } }
    { type: 'Action.Submit', title: 'Not me, escalate', data: { response: 'Not me, escalate' } }
    { type: 'Action.Submit', title: 'Call the SOC', data: { response: 'Call the SOC' } }
  ]
}

resource sentinelConn 'Microsoft.Web/connections@2016-06-01' = {
  name: 'azuresentinel-${prefix}'
  location: location
  tags: tags
  // Required for managed-identity Sentinel connections; missing from the type definitions.
  #disable-next-line BCP187
  kind: 'V1'
  properties: {
    displayName: 'Sentinel (managed identity)'
    api: { id: subscriptionResourceId('Microsoft.Web/locations/managedApis', location, 'azuresentinel') }
    #disable-next-line BCP037
    parameterValueType: 'Alternative'
  }
}

resource teamsConn 'Microsoft.Web/connections@2016-06-01' = {
  name: 'teams-${prefix}'
  location: location
  tags: tags
  properties: {
    displayName: 'Teams (authorize after deployment)'
    api: { id: subscriptionResourceId('Microsoft.Web/locations/managedApis', location, 'teams') }
  }
}

resource la 'Microsoft.Logic/workflows@2019-05-01' = {
  name: 'logic-${prefix}-teams'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    state: 'Enabled'
    definition: {
      '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#'
      contentVersion: '1.0.0.0'
      parameters: { '$connections': { type: 'Object', defaultValue: {} } }
      triggers: {
        Microsoft_Sentinel_incident: {
          type: 'ApiConnectionWebhook'
          inputs: {
            host: { connection: { name: '@parameters(\'$connections\')[\'azuresentinel\'][\'connectionId\']' } }
            body: { callback_url: '@{listCallbackUrl()}' }
            path: '/incident-creation'
          }
        }
      }
      actions: {
        If_high_severity: {
          type: 'If'
          expression: { and: [ { equals: [ '@triggerBody()?[\'object\']?[\'properties\']?[\'severity\']', 'High' ] } ] }
          actions: {
            Post_card_and_wait: {
              type: 'ApiConnectionWebhook'
              inputs: {
                host: { connection: { name: '@parameters(\'$connections\')[\'teams\'][\'connectionId\']' } }
                body: {
                  body: {
                    messageBody: string(card)
                    recipient: { groupId: teamsTeamId, channelId: teamsChannelId }
                    shouldUpdateCard: true
                    updateMessage: 'Response recorded on the incident.'
                  }
                  notificationUrl: '@{listCallbackUrl()}'
                }
                path: '/v1.0/teams/conversation/gatherinput/poster/Flow bot/location/@{encodeURIComponent(\'Channel\')}/$subscriptions'
              }
            }
            Add_comment_to_incident: {
              type: 'ApiConnection'
              runAfter: { Post_card_and_wait: [ 'Succeeded' ] }
              inputs: {
                host: { connection: { name: '@parameters(\'$connections\')[\'azuresentinel\'][\'connectionId\']' } }
                method: 'post'
                path: '/Incidents/Comment'
                body: {
                  incidentArmId: '@triggerBody()?[\'object\']?[\'id\']'
                  message: '<p>Operator response from Teams: @{body(\'Post_card_and_wait\')?[\'data\']?[\'response\']} (by @{body(\'Post_card_and_wait\')?[\'responder\']?[\'userPrincipalName\']})</p>'
                }
              }
            }
          }
        }
      }
    }
    parameters: {
      '$connections': {
        value: {
          azuresentinel: {
            connectionId: sentinelConn.id
            connectionName: sentinelConn.name
            id: sentinelConn.properties.api.id
            connectionProperties: { authentication: { type: 'ManagedServiceIdentity' } }
          }
          teams: {
            connectionId: teamsConn.id
            connectionName: teamsConn.name
            id: teamsConn.properties.api.id
          }
        }
      }
    }
  }
}

output playbookId string = la.id
output playbookPrincipalId string = la.identity.principalId
