targetScope = 'resourceGroup'

param location string = 'eastus2'
param prefix string = 'swulab'
param environmentName string
param image string
param registryServer string
param identityName string
param storageUrl string
param tenantId string
param adminObjectIds array
param enableLiveControl bool = false
param routeWorkspaceIds object = {}

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' existing = {
  name: environmentName
}
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}
resource console 'Microsoft.App/containerApps@2025-01-01' = {
  name: 'ca-${prefix}-console'
  location: location
  tags: { project: 'swu-lab', deployment: prefix, role: 'Authenticated lab console' }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: false, targetPort: 8000, allowInsecure: false, transport: 'auto' }
      registries: [{ server: registryServer, identity: identity.id }]
    }
    template: {
      containers: [{
        name: 'console'
        image: image
        resources: { cpu: json('0.25'), memory: '0.5Gi' }
        env: [
          { name: 'SWU_CONSOLE_ENVIRONMENT', value: 'azure' }
          { name: 'SWU_CONSOLE_HOSTING_PROVIDER', value: 'container-apps' }
          { name: 'SWU_CONSOLE_HOSTNAME', value: 'ca-${prefix}-console.${environment.properties.defaultDomain}' }
          { name: 'SWU_CONSOLE_TENANT_ID', value: tenantId }
          { name: 'SWU_CONSOLE_ADMIN_OBJECT_IDS', value: join(adminObjectIds, ',') }
          { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
          { name: 'SWU_CONSOLE_STORAGE_URL', value: storageUrl }
          { name: 'SWU_CONSOLE_LIVE', value: enableLiveControl ? 'true' : 'false' }
          { name: 'SWU_ROUTE_WORKSPACES', value: string(routeWorkspaceIds) }
        ]
        probes: [
          { type: 'Liveness', tcpSocket: { port: 8000 }, initialDelaySeconds: 10, periodSeconds: 30 }
          { type: 'Readiness', tcpSocket: { port: 8000 }, initialDelaySeconds: 5, periodSeconds: 10 }
        ]
      }]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}
output siteName string = console.name
output siteId string = console.id
output hostname string = 'ca-${prefix}-console.${environment.properties.defaultDomain}'
