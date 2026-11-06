// Include every charge in the deployment's dedicated groups, including untagged VM disks.
targetScope = 'subscription'

param prefix string
param amount int
param startDate string
param contactEmails array
param resourceGroups array

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'budget-${prefix}'
  properties: {
    category: 'Cost'
    amount: amount
    timeGrain: 'Monthly'
    timePeriod: { startDate: startDate }
    filter: {
      dimensions: { name: 'ResourceGroupName', operator: 'In', values: resourceGroups }
    }
    notifications: {
      actual50: { enabled: true, operator: 'GreaterThan', threshold: 50, thresholdType: 'Actual', contactEmails: contactEmails }
      actual80: { enabled: true, operator: 'GreaterThan', threshold: 80, thresholdType: 'Actual', contactEmails: contactEmails }
      actual100: { enabled: true, operator: 'GreaterThan', threshold: 100, thresholdType: 'Actual', contactEmails: contactEmails }
    }
  }
}
