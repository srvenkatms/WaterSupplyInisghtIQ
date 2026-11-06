// HM-02: per-workspace health workbook - last record, daily volume and status per source.
param location string
param workspaceId string
param utilityName string
param tags object
param healthSources array

var healthQuery = replace(replace('''
let expected = print Sources = dynamic(__HEALTH__)
| mv-expand Sources | project Source = tostring(Sources.Source), MaxSilenceMin = toint(Sources.MaxSilenceMin);
__HEALTH_UNION__
| where TimeGenerated > ago(9d)
| summarize LastSeen = max(ReceivedAt), Rows24h = countif(ReceivedAt > ago(1d)) by HealthFeed
| project Source = HealthFeed, LastSeen, Rows24h
| join kind=rightouter expected on Source
| extend Source = coalesce(Source, Source1)
| extend SilentMin = iff(isnull(LastSeen), long(null), datetime_diff('minute', now(), LastSeen))
| extend Status = case(isnull(LastSeen), "No data", SilentMin > MaxSilenceMin, "Silent", "Healthy")
| project Source, Status, LastSeen, SilentMin, MaxSilenceMin, Rows24h
| order by Status asc, Source asc
''', '__HEALTH_UNION__', loadTextContent('../../generator/swugen/source_health.kql')), '__HEALTH__', string(healthSources))
var volumeQuery = replace('''
__HEALTH_UNION__,
    (SwuAudit | extend HealthFeed = "SwuAudit"),
    (SwuOtAlerts | extend HealthFeed = "SwuOtAlerts")
| where TimeGenerated > ago(7d)
| summarize Rows = count() by HealthFeed, bin(TimeGenerated, 1d)
| project Source = HealthFeed, TimeGenerated, Rows
''', '__HEALTH_UNION__', loadTextContent('../../generator/swugen/source_health.kql'))
var collectorQuery = '''
SwuProcess
| where TimeGenerated > ago(9d)
| where EventType == "Heartbeat" or (Tag == "PLC" and EventType in ("PlcUnreachable", "Recovered"))
| summarize arg_max(TimeGenerated, EventType)
| extend Status = case(EventType == "PlcUnreachable", "PLC unreachable",
                       TimeGenerated < ago(30m), "No recent state",
                       EventType in ("Heartbeat", "Recovered"), "Reporting", "No state")
| project Status, LastEvent = TimeGenerated, EventType
'''
var alertQuery = '''
SecurityAlert
| where TimeGenerated > ago(7d) and AlertName startswith "DR-"
| summarize Alerts = count() by AlertName, AlertSeverity
| order by Alerts desc
'''

var workbook = {
  version: 'Notebook/1.0'
  items: [
    { type: 1, name: 'title', content: { json: '## ${utilityName}: data source health\nA source is **Silent** when it exceeds its expected interval (HM-01). Silence from the firewall or process feed during an attack is itself a signal.' } }
    { type: 3, name: 'health', content: { version: 'KqlItem/1.0', query: healthQuery, size: 0, title: 'Source health', queryType: 0, resourceType: 'microsoft.operationalinsights/workspaces' } }
    { type: 3, name: 'collector', content: { version: 'KqlItem/1.0', query: collectorQuery, size: 0, title: 'Process collector state', queryType: 0, resourceType: 'microsoft.operationalinsights/workspaces' } }
    { type: 3, name: 'volume', content: { version: 'KqlItem/1.0', query: volumeQuery, size: 0, title: 'Daily rows per source (7 days)', visualization: 'barchart', queryType: 0, resourceType: 'microsoft.operationalinsights/workspaces' } }
    { type: 3, name: 'alerts', content: { version: 'KqlItem/1.0', query: alertQuery, size: 0, title: 'Detection rule alerts (7 days)', queryType: 0, resourceType: 'microsoft.operationalinsights/workspaces' } }
  ]
  fallbackResourceIds: [ workspaceId ]
}

resource wb 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(workspaceId, 'swulab-health')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: '${utilityName} source health'
    category: 'sentinel'
    sourceId: workspaceId
    serializedData: string(workbook)
  }
}
