// Detection content for one utility workspace: normalizing KQL functions,
// reference-data watchlists and analytics rules.
param workspaceName string
param utilityId string
param utilityName string
param watchlists array
param healthSources array
param deployedAt string
param deployAnalyticsRules bool = true
var ruleContract = loadJsonContent('../../rules/contract.json')

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

// FR-10: rules query these functions, never the Sim tables directly.
// ScenarioRunId is deliberately not projected (FR-23).
var functions = [
  { alias: 'SwuExposure', query: 'SimExposure_CL | summarize arg_max(TimeGenerated, *) by RecordId | project TimeGenerated, UtilityId, Source, AssetIp, Port, Service, Product, Finding, Severity, FirstSeen, Status' }
  { alias: 'SwuSignins', query: 'SimSignin_CL | summarize arg_max(TimeGenerated, *) by RecordId | project TimeGenerated, UtilityId, UserPrincipalName, UserType, AppDisplayName, ResultType, IPAddress, Location, DeviceId, MfaResult, RiskLevel' }
  { alias: 'SwuAudit', query: 'SimAudit_CL | summarize arg_max(TimeGenerated, *) by RecordId | project TimeGenerated, UtilityId, OperationName, InitiatedBy, TargetUser, Result' }
  { alias: 'SwuAccess', query: 'SimAccessTraffic_CL | summarize arg_max(TimeGenerated, *) by RecordId | project TimeGenerated, UtilityId, UserPrincipalName, DestinationApp, SourceIp, Location, SessionStart, SessionEnd, Action' }
  { alias: 'SwuFirewall', query: 'SimFirewall_CL | summarize arg_max(TimeGenerated, *) by RecordId | project TimeGenerated, UtilityId, DeviceAction, SourceIP, DestinationIP, DestinationPort, Protocol, SourceZone, DestinationZone, RuleName, Activity' }
  { alias: 'SwuEndpoint', query: 'SimEndpoint_CL | summarize arg_max(TimeGenerated, *) by RecordId | project TimeGenerated, UtilityId, DeviceName, DeviceRole, ActionType, FileName, ProcessCommandLine, AccountName, AlertTitle, Severity' }
  { alias: 'SwuOtAlerts', query: 'SimOtAlert_CL | summarize arg_max(TimeGenerated, *) by RecordId | project TimeGenerated, UtilityId, AlertType, SourceIp, DestinationIp, Protocol, Register, Value, Severity' }
  { alias: 'SwuProcess', query: 'SimProcess_CL | summarize arg_max(TimeGenerated, *) by RecordId | project TimeGenerated, UtilityId, SiteId, Tag, EventType, OldValue, NewValue, Unit, ChangedBy, Limit' }
  { alias: 'SwuInventory', query: 'SimInventory_CL | summarize arg_max(TimeGenerated, *) by RecordId | project TimeGenerated, UtilityId, AssetId, AssetType, SiteId, Model, Firmware, IpType, LastSeen' }
  { alias: 'SwuAgentActions', query: 'SimAgentAction_CL | summarize arg_max(TimeGenerated, *) by RecordId | project TimeGenerated, UtilityId, AgentName, Action, Target, ApprovedBy, Result' }
  { alias: 'SwuAlerts', query: 'SecurityAlert | project TimeGenerated, AlertName, AlertSeverity, ExtendedProperties, SystemAlertId' }
  { alias: 'SwuIncidents', query: 'SecurityIncident | summarize arg_max(TimeGenerated, *) by IncidentNumber | project TimeGenerated, IncidentNumber, Severity, Status, AlertIds' }
]
var receiptFunctions = [for f in functions: {
  alias: f.alias
  query: startsWith(f.alias, 'Swu') && !contains(['SwuAlerts', 'SwuIncidents'], f.alias)
    ? replace(f.query, ' | summarize arg_max(TimeGenerated, *) by RecordId | project TimeGenerated', ' | extend ReceivedAt = ingestion_time() | summarize arg_max(ReceivedAt, *) by RecordId | project ReceivedAt, TimeGenerated')
    : f.query
}]
var collectorFields = {
  SwuSignins: 'ResultType'
  SwuAccess: 'Action'
  SwuEndpoint: 'ActionType'
  SwuFirewall: 'Activity'
}
var analyticsFunctions = [for f in receiptFunctions: {
  alias: f.alias
  query: contains(collectorFields, f.alias)
    ? '${f.query} | where ${collectorFields[f.alias]} != "CollectorHeartbeat"'
    : f.query
}]
var collectionFunctions = [for f in filter(receiptFunctions, f => contains(collectorFields, f.alias)): {
  alias: '${f.alias}Feed'
  query: f.query
}]

resource kqlFunctions 'Microsoft.OperationalInsights/workspaces/savedSearches@2020-08-01' = [for f in concat(analyticsFunctions, collectionFunctions): {
  parent: law
  name: f.alias
  properties: {
    category: 'SwuLab'
    displayName: f.alias
    functionAlias: f.alias
    query: f.query
    version: 2
  }
}]

resource wl 'Microsoft.SecurityInsights/watchlists@2024-03-01' = [for w in watchlists: {
  scope: law
  name: w.alias
  properties: union({
    displayName: w.alias
    provider: 'SwuLab'
    source: 'Local file'
    itemsSearchKey: w.key
    description: '${utilityName} ${w.alias}'
  }, w.hasItems ? {
    contentType: 'text/csv'
    numberOfLinesToSkip: 0
    rawContent: w.csv
  } : {})
}]

var incidentConfig = {
  createIncident: true
  groupingConfiguration: {
    enabled: true
    reopenClosedIncident: false
    lookbackDuration: 'PT2H'
    matchingMethod: 'Selected'
    groupByEntities: []
    groupByAlertDetails: []
    groupByCustomDetails: [ 'UtilityId' ]
  }
}

// DR-02 Password guessing then success
resource dr02 'Microsoft.SecurityInsights/alertRules@2024-03-01' = if (deployAnalyticsRules) {
  scope: law
  name: guid(law.id, 'DR-02')
  dependsOn: [ kqlFunctions ]
  kind: 'Scheduled'
  properties: {
    displayName: 'DR-02 Password guessing then success'
    description: 'Twenty or more failed sign-ins for one account within 15 minutes, followed by a success. ICS ATT&CK: ${join(ruleContract['DR-02'].techniques, ', ')}.'
    severity: 'High'
    enabled: true
    query: '''
let failures = SwuSignins
| where TimeGenerated > ago(20m) and ResultType != "0"
| project UtilityId, UserPrincipalName, FailedAt = TimeGenerated;
SwuSignins
| where TimeGenerated > ago(5m) and ResultType == "0"
| join kind=inner failures on UtilityId, UserPrincipalName
| where FailedAt >= TimeGenerated - 15m and FailedAt < TimeGenerated
| summarize Failures = count() by TimeGenerated, UtilityId, UserPrincipalName, IPAddress
| where Failures >= 20
'''
    queryFrequency: 'PT5M'
    queryPeriod: 'PT20M'
    triggerOperator: 'GreaterThan'
    triggerThreshold: 0
    suppressionDuration: 'PT1H'
    suppressionEnabled: false
    tactics: [ 'CredentialAccess' ]
    customDetails: { UtilityId: 'UtilityId' }
    entityMappings: [
      { entityType: 'Account', fieldMappings: [ { identifier: 'FullName', columnName: 'UserPrincipalName' } ] }
      { entityType: 'IP', fieldMappings: [ { identifier: 'Address', columnName: 'IPAddress' } ] }
    ]
    incidentConfiguration: incidentConfig
  }
}

// DR-06 Setpoint change by an unexpected actor, outside a window, or outside limits
resource dr06 'Microsoft.SecurityInsights/alertRules@2024-03-01' = if (deployAnalyticsRules) {
  scope: law
  name: guid(law.id, 'DR-06')
  dependsOn: [ kqlFunctions, wl ]
  kind: 'Scheduled'
  properties: {
    displayName: 'DR-06 Setpoint change by an unexpected actor'
    description: 'Setpoint change outside a maintenance window, by someone not on the roster or vendor list, or to a value outside safe limits. ICS ATT&CK: ${join(ruleContract['DR-06'].techniques, ', ')}.'
    severity: 'High'
    enabled: true
    query: '''
let limits = _GetWatchlist('SafeLimits') | project Tag = tostring(Tag), Min = todouble(Min), Max = todouble(Max);
let known = union (_GetWatchlist('Roster') | project Upn = tostring(UserPrincipalName)), (_GetWatchlist('Vendors') | project Upn = tostring(UserPrincipalName));
let windows = _GetWatchlist('MaintenanceWindows')
| project Start = todatetime(column_ifexists("Start", "")), End = todatetime(column_ifexists("End", "")), JoinKey = 1;
SwuProcess
| where TimeGenerated > ago(10m) and EventType == "SetpointChange"
| extend JoinKey = 1
| join kind=leftouter windows on JoinKey
| summarize InWindow = countif(TimeGenerated between (Start .. End)) > 0 by TimeGenerated, UtilityId, SiteId, Tag, OldValue, NewValue, ChangedBy
| join kind=leftouter limits on Tag
| extend OutsideLimits = isnotnull(Max) and (NewValue > Max or NewValue < Min)
| extend UnknownActor = isnotempty(ChangedBy) and ChangedBy !in (known)
| where OutsideLimits or UnknownActor or not(InWindow)
| project TimeGenerated, UtilityId, SiteId, Tag, OldValue, NewValue, ChangedBy, OutsideLimits, UnknownActor
'''
    queryFrequency: 'PT5M'
    queryPeriod: 'PT10M'
    triggerOperator: 'GreaterThan'
    triggerThreshold: 0
    suppressionDuration: 'PT1H'
    suppressionEnabled: false
    tactics: [ 'Impact' ]
    customDetails: { UtilityId: 'UtilityId', Tag: 'Tag', NewValue: 'NewValue' }
    incidentConfiguration: incidentConfig
  }
}

// DR-07 Process out of range (near-real-time rule)
resource dr07 'Microsoft.SecurityInsights/alertRules@2024-01-01-preview' = if (deployAnalyticsRules) {
  scope: law
  name: guid(law.id, 'DR-07')
  dependsOn: [ kqlFunctions ]
  kind: 'NRT'
  properties: {
    displayName: 'DR-07 Process value out of safe range'
    description: 'A chemical or tank value has left its safe range. ICS ATT&CK: ${join(ruleContract['DR-07'].techniques, ', ')}.'
    severity: 'High'
    enabled: true
    query: '''
SwuProcess
| where EventType == "OutOfRange"
| project TimeGenerated, UtilityId, SiteId, Tag, NewValue, Limit
'''
    suppressionDuration: 'PT1H'
    suppressionEnabled: false
    tactics: [ 'Impact' ]
    customDetails: { UtilityId: 'UtilityId', Tag: 'Tag', NewValue: 'NewValue' }
    incidentConfiguration: incidentConfig
  }
}

// DR-11 Data source silent (health)
resource dr11 'Microsoft.SecurityInsights/alertRules@2024-03-01' = if (deployAnalyticsRules) {
  scope: law
  name: guid(law.id, 'DR-11')
  dependsOn: [ kqlFunctions, wl ]
  kind: 'Scheduled'
  properties: {
    displayName: 'DR-11 Data source silent'
    description: 'A source has exceeded its expected silence threshold (HM-01). ICS ATT&CK: ${join(ruleContract['DR-11'].techniques, ', ')}.'
    severity: 'Low'
    enabled: true
    query: replace(replace(replace(replace('''
let expected = print Sources = dynamic(__HEALTH__)
| mv-expand Sources | project Source = tostring(Sources.Source), MaxSilenceMin = toint(Sources.MaxSilenceMin);
let recent = __HEALTH_UNION__
| where TimeGenerated > ago(1d)
| summarize LastSeen = max(ReceivedAt) by HealthFeed
| project Source = HealthFeed, LastSeen;
let history = _GetWatchlist('SourceReceipts')
| project Source = tostring(column_ifexists("Source", "")), LastSeen = todatetime(column_ifexists("LastSeen", ""));
let received = union recent, history | summarize LastSeen = max(LastSeen) by Source;
let silent = expected
| join kind=leftouter received on Source
| extend LastSeen = coalesce(LastSeen, datetime(__DEPLOYED_AT__))
| extend SilentMin = datetime_diff('minute', now(), LastSeen)
| where SilentMin > MaxSilenceMin
| project TimeGenerated = now(), UtilityId = "__UTILITY__", Source, LastSeen, SilentMin;
let recentHeartbeat = SwuProcess
| where TimeGenerated > ago(1d) and EventType == "Heartbeat"
| summarize HeartbeatAt = max(TimeGenerated) by UtilityId;
let cachedHeartbeat = _GetWatchlist('SourceReceipts')
| where tostring(column_ifexists("Source", "")) == "SwuProcess"
| project UtilityId = "__UTILITY__", HeartbeatAt = todatetime(column_ifexists("HeartbeatAt", ""));
let live = union recentHeartbeat, cachedHeartbeat | summarize HeartbeatAt = max(HeartbeatAt) by UtilityId;
let recentPlc = SwuProcess
| where TimeGenerated > ago(1d) and Tag == "PLC" and EventType in ("PlcUnreachable", "Recovered")
| project TimeGenerated, UtilityId, EventType;
let cachedPlc = _GetWatchlist('SourceReceipts')
| where tostring(column_ifexists("Source", "")) == "SwuProcess"
| project TimeGenerated = todatetime(column_ifexists("PlcChangedAt", "")), UtilityId = "__UTILITY__",
          EventType = tostring(column_ifexists("PlcEvent", ""))
| where TimeGenerated > ago(9d) and EventType in ("PlcUnreachable", "Recovered");
let unreachable = union recentPlc, cachedPlc
| summarize arg_max(TimeGenerated, EventType) by UtilityId
| join kind=leftouter live on UtilityId
| where EventType == "PlcUnreachable" and (isnull(HeartbeatAt) or TimeGenerated > HeartbeatAt)
| extend LastSeen = TimeGenerated
| project TimeGenerated = now(), UtilityId, Source = "Edge gateway: PLC unreachable",
          LastSeen, SilentMin = datetime_diff('minute', now(), LastSeen);
let incidentHistory = _GetWatchlist('IncidentHistory')
| project IncidentNumber = tolong(column_ifexists("IncidentNumber", "")),
          ChangedAt = todatetime(column_ifexists("ChangedAt", "")),
          Severity = tostring(column_ifexists("Severity", "")), Status = tostring(column_ifexists("Status", ""));
let recentIncidents = SwuIncidents
| project ChangedAt = TimeGenerated, IncidentNumber = tolong(IncidentNumber), Severity, Status;
let attackOpen = toscalar(union incidentHistory, recentIncidents
| where ChangedAt > ago(9d) | summarize arg_max(ChangedAt, *) by IncidentNumber
| where Status != "Closed" and Severity == "High" | count) > 0;
union silent, unreachable
| extend AlertSeverity = iff(attackOpen, "High", "Low")
''', '__HEALTH_UNION__', loadTextContent('../../generator/swugen/source_health.kql')), '__HEALTH__', string(healthSources)), '__DEPLOYED_AT__', deployedAt), '__UTILITY__', utilityId)
    queryFrequency: 'PT5M'
    queryPeriod: 'P1D'
    triggerOperator: 'GreaterThan'
    triggerThreshold: 0
    suppressionDuration: 'PT1H'
    suppressionEnabled: false
    tactics: [ 'DefenseEvasion' ]
    customDetails: { UtilityId: 'UtilityId', Source: 'Source' }
    alertDetailsOverride: { alertSeverityColumnName: 'AlertSeverity' }
    incidentConfiguration: incidentConfig
  }
}

// Remaining scheduled rules (section 4.1). Each reads only Swu functions and watchlists.
var scheduledRules = [
  {
    id: 'DR-01'
    severityColumn: 'AlertSeverity'
    name: 'DR-01 Unusual remote session'
    description: 'Remote session at night by someone not on call that night, or from a source not seen for that user in 14 days.'
    severity: 'Medium'
    tactics: [ 'InitialAccess' ]
    period: 'PT1H'
    query: '''
let roster = _GetWatchlist('Roster') | project Upn = tolower(tostring(UserPrincipalName)), OnCallDays = tostring(OnCallDays);
let historicalSources = _GetWatchlist('AccessHistory')
| project Upn = tostring(column_ifexists("Upn", "")), SourceIp = tostring(column_ifexists("SourceIp", "")),
          Location = tostring(column_ifexists("Location", "")), SeenAt = todatetime(column_ifexists("SeenAt", ""))
| where SeenAt between (ago(14d) .. ago(15m));
let recentSources = SwuAccess
| where TimeGenerated between (ago(1h) .. ago(15m))
| project Upn = tolower(UserPrincipalName), SourceIp, Location, SeenAt = TimeGenerated;
let seen = union historicalSources, recentSources
| distinct Upn, SourceIp, Location
| extend Seen = true;
let serious = SwuAlerts
| where TimeGenerated > ago(1h) and AlertSeverity == "High" and (AlertName startswith "DR-06" or AlertName startswith "DR-07")
| summarize SeriousAt = max(TimeGenerated);
SwuAccess
| where TimeGenerated > ago(1h)
| extend Upn = tolower(UserPrincipalName), Hour = hourofday(TimeGenerated), Day = tostring(toint(dayofweek(TimeGenerated) / 1d))
| join kind=leftouter roster on Upn
| join kind=leftouter seen on Upn, SourceIp, Location
| extend Night = Hour >= 22 or Hour < 6, OnCallTonight = OnCallDays has Day, NewSource = isnull(Seen)
| where (Night and not(OnCallTonight)) or NewSource
| extend SeriousAt = toscalar(serious)
| extend AlertSeverity = iff(SeriousAt between (TimeGenerated .. TimeGenerated + 60m), "High", "Medium")
| project TimeGenerated, UtilityId, UserPrincipalName, SourceIp, Location, DestinationApp, Night, OnCallTonight, NewSource, AlertSeverity
'''
    entities: [
      { entityType: 'Account', fieldMappings: [ { identifier: 'FullName', columnName: 'UserPrincipalName' } ] }
      { entityType: 'IP', fieldMappings: [ { identifier: 'Address', columnName: 'SourceIp' } ] }
    ]
  }
  {
    id: 'DR-03'
    severityColumn: 'AlertSeverity'
    name: 'DR-03 New internet exposure'
    description: 'New exposure finding of Medium or above, or any finding on an OT port. High for OT ports.'
    severity: 'Medium'
    tactics: [ 'Reconnaissance' ]
    period: 'PT1H'
    query: '''
SwuExposure
| where TimeGenerated > ago(1h) and Status == "New"
| extend OtPort = Port in (502, 102, 20256, 44818, 5900)
| where OtPort or Severity in ("Medium", "High", "Critical")
| extend AlertSeverity = iff(OtPort, "High", "Medium")
| project TimeGenerated, UtilityId, AssetIp, Port, Service, Product, Finding, AlertSeverity
'''
    entities: [ { entityType: 'IP', fieldMappings: [ { identifier: 'Address', columnName: 'AssetIp' } ] } ]
  }
  {
    id: 'DR-04'
    severityColumn: ''
    name: 'DR-04 Inbound traffic to an OT port'
    description: 'Inbound traffic from outside the lab address space to an OT port, whether allowed or denied.'
    severity: 'High'
    tactics: [ 'InitialAccess' ]
    period: 'PT10M'
    query: '''
SwuFirewall
| where TimeGenerated > ago(10m)
| where DestinationPort in (502, 102, 20256, 44818, 5900)
| where not(ipv4_is_in_range(SourceIP, "10.10.0.0/16"))
| project TimeGenerated, UtilityId, SourceIP, DestinationIP, DestinationPort, RuleName
'''
    entities: [ { entityType: 'IP', fieldMappings: [ { identifier: 'Address', columnName: 'SourceIP' } ] } ]
  }
  {
    id: 'DR-05'
    severityColumn: ''
    name: 'DR-05 OT write or program change'
    description: 'Modbus write, program download or controller mode change outside a maintenance window.'
    severity: 'High'
    tactics: [ 'Impact' ]
    period: 'PT10M'
    query: '''
let windows = _GetWatchlist('MaintenanceWindows')
| project Start = todatetime(column_ifexists("Start", "")), End = todatetime(column_ifexists("End", "")), JoinKey = 1;
SwuOtAlerts
| where TimeGenerated > ago(10m) and AlertType in ("ModbusWrite", "ProgramDownload", "ModeChange")
| extend JoinKey = 1
| join kind=leftouter windows on JoinKey
| summarize InWindow = countif(TimeGenerated between (Start .. End)) > 0 by TimeGenerated, UtilityId, AlertType, SourceIp, DestinationIp, Register, Value
| where not(InWindow)
| project TimeGenerated, UtilityId, AlertType, SourceIp, DestinationIp, Register, Value
'''
    entities: [ { entityType: 'IP', fieldMappings: [ { identifier: 'Address', columnName: 'SourceIp' } ] } ]
  }
  {
    id: 'DR-08'
    severityColumn: ''
    name: 'DR-08 Sign-in attempt by a disabled account'
    description: 'Sign-in attempt by an account disabled within the last 90 days and not re-enabled. Durable reference state avoids the scheduler history limit.'
    severity: 'High'
    tactics: [ 'Persistence' ]
    period: 'PT10M'
    query: '''
let state = _GetWatchlist('DisabledAccounts')
| project Upn = tolower(tostring(column_ifexists("UserPrincipalName", ""))),
          DisabledAt = todatetime(column_ifexists("DisabledAt", ""))
| where DisabledAt > ago(90d);
SwuSignins
| where TimeGenerated > ago(10m)
| extend Upn = tolower(UserPrincipalName)
| join kind=inner state on Upn
| where TimeGenerated > DisabledAt
| project TimeGenerated, UtilityId, UserPrincipalName, IPAddress, ResultType, DisabledAt
'''
    entities: [
      { entityType: 'Account', fieldMappings: [ { identifier: 'FullName', columnName: 'UserPrincipalName' } ] }
      { entityType: 'IP', fieldMappings: [ { identifier: 'Address', columnName: 'IPAddress' } ] }
    ]
  }
  {
    id: 'DR-09'
    severityColumn: ''
    name: 'DR-09 Remote access tool on the SCADA PC'
    description: 'A known remote access tool was installed or started on a SCADA-role device.'
    severity: 'High'
    tactics: [ 'CommandAndControl' ]
    period: 'PT10M'
    query: '''
let tools = _GetWatchlist('RemoteTools') | project FileName = tolower(tostring(FileName)), Product = tostring(Product);
SwuEndpoint
| where TimeGenerated > ago(10m) and DeviceRole == "SCADA" and ActionType in ("SoftwareInstalled", "ProcessCreated")
| extend FileName = tolower(FileName)
| join kind=inner tools on FileName
| project TimeGenerated, UtilityId, DeviceName, FileName, Product, AccountName, ProcessCommandLine
'''
    entities: [ { entityType: 'Host', fieldMappings: [ { identifier: 'HostName', columnName: 'DeviceName' } ] } ]
  }
  {
    id: 'DR-10'
    severityColumn: ''
    name: 'DR-10 Ransomware or enterprise-to-control traffic'
    description: 'Ransomware alert on any PC, or traffic from the enterprise zone toward the control zone.'
    severity: 'High'
    tactics: [ 'Impact', 'LateralMovement' ]
    period: 'PT10M'
    query: '''
union
  (SwuEndpoint | where TimeGenerated > ago(10m) and AlertTitle has "ransomware"
   | project TimeGenerated, UtilityId, Evidence = AlertTitle, DeviceName, SourceIP = ""),
  (SwuFirewall | where TimeGenerated > ago(10m) and SourceZone == "Enterprise" and DestinationZone == "Control"
   | project TimeGenerated, UtilityId, Evidence = strcat(DeviceAction, " ", Protocol, "/", DestinationPort), DeviceName = "", SourceIP)
'''
    entities: [
      { entityType: 'Host', fieldMappings: [ { identifier: 'HostName', columnName: 'DeviceName' } ] }
      { entityType: 'IP', fieldMappings: [ { identifier: 'Address', columnName: 'SourceIP' } ] }
    ]
  }
]

resource moreRules 'Microsoft.SecurityInsights/alertRules@2024-03-01' = [for r in scheduledRules: if (deployAnalyticsRules) {
  scope: law
  name: guid(law.id, r.id)
  dependsOn: [ kqlFunctions, wl ]
  kind: 'Scheduled'
  properties: {
    displayName: r.name
    description: '${r.description} ICS ATT&CK: ${join(ruleContract[r.id].techniques, ', ')}.'
    severity: r.severity
    enabled: true
    query: r.query
    queryFrequency: 'PT5M'
    queryPeriod: r.period
    triggerOperator: 'GreaterThan'
    triggerThreshold: 0
    suppressionDuration: 'PT1H'
    suppressionEnabled: false
    tactics: r.tactics
    customDetails: { UtilityId: 'UtilityId' }
    entityMappings: r.entities
    alertDetailsOverride: empty(r.severityColumn) ? null : { alertSeverityColumnName: r.severityColumn }
    incidentConfiguration: incidentConfig
  }
}]
