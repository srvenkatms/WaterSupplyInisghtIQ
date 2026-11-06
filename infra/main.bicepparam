using 'main.bicep'

param location = readEnvironmentVariable('SWULAB_LOCATION', 'eastus2')
param prefix = readEnvironmentVariable('SWULAB_PREFIX', 'swulab')
param owner = readEnvironmentVariable('SWULAB_OWNER', 'replace-with-owner')
param costCenter = 'lab'
param adminSshPublicKey = readEnvironmentVariable('SWULAB_SSH_PUBLIC_KEY', '')
param deployUtilityANetwork = true
param allowBuildOutbound = true
param deployHmi = true
param deployOperatorHost = bool(readEnvironmentVariable('SWULAB_DEPLOY_OPERATOR', 'false'))
param utilityAVmSize = readEnvironmentVariable('SWULAB_VM_SIZE', 'Standard_B2s')
param budgetAmount = int(readEnvironmentVariable('SWULAB_BUDGET_AMOUNT', '300'))
param budgetStartDate = readEnvironmentVariable('SWULAB_BUDGET_START_DATE', '2026-11-01')
param budgetContactEmails = json(readEnvironmentVariable('SWULAB_BUDGET_EMAILS', '[]'))
