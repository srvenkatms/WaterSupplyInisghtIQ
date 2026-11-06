#!/usr/bin/env bash
# Deploys the Level 1 lab (DEP-03). Usage: ./scripts/deploy.sh [subscription-id]
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD

SUBSCRIPTION="${1:-${AZURE_SUBSCRIPTION_ID:-}}"
: "${SUBSCRIPTION:?Supply the approved Azure subscription ID}"
: "${SWULAB_OWNER:?Set the owner tag}"
: "${SWULAB_BUDGET_AMOUNT:?Set the approved monthly budget}"
: "${SWULAB_BUDGET_EMAILS:?Set a JSON array of budget alert email addresses}"
[[ "$SWULAB_BUDGET_AMOUNT" =~ ^[1-9][0-9]*$ ]] || { echo "Budget must be a positive integer" >&2; exit 1; }
jq -e 'type == "array" and length > 0 and all(.[]; type == "string" and contains("@"))' \
  <<<"$SWULAB_BUDGET_EMAILS" >/dev/null
export SWULAB_PREFIX="${SWULAB_PREFIX:-swulab}"
[[ "$SWULAB_PREFIX" =~ ^[a-z][a-z0-9]{2,7}$ ]] || { echo "Prefix must be 3-8 lowercase alphanumeric characters" >&2; exit 1; }
export SWULAB_BUDGET_START_DATE="${SWULAB_BUDGET_START_DATE:-$(date +%Y-%m-01)}"
az account set --subscription "$SUBSCRIPTION"
: "${SWULAB_SSH_PUBLIC_KEY:?Set SWULAB_SSH_PUBLIC_KEY to the contents of your SSH public key}"
LOCATION="${SWULAB_LOCATION:-eastus2}"
export PYTHONPATH="$ROOT/generator"
python -m swugen.configuration --check
if [[ "${SWULAB_DEPLOY_OPERATOR:-false}" == true ]]; then
  python scripts/deploy-operator.py --check-key
fi
export MSYS_NO_PATHCONV=1
cd "$ROOT/infra"
NAME="swulab-$(date +%Y%m%d%H%M)"
# The signed-in user becomes the lab tester (SEC-05) unless SWULAB_TESTER_ID is set.
TESTER="${SWULAB_TESTER_ID:-}"
if [[ -z "$TESTER" ]]; then
  TESTER=$(az ad signed-in-user show --query id -o tsv)
fi
EXTRA=(--parameters "testerPrincipalId=${TESTER}" "location=${LOCATION}")
if [[ -f "$ROOT/console-deployment.json" ]] && jq -e --arg prefix "$SWULAB_PREFIX" \
  '.prefix == $prefix and .provider == "container-apps" and (.environmentId | type == "string")' \
  "$ROOT/console-deployment.json" >/dev/null; then
  EXTRA+=(--parameters "preserveConsoleSubnet=true")
fi
if [[ -n "${SWULAB_TESTER_TYPE:-}" ]]; then
  EXTRA+=(--parameters "testerPrincipalType=$SWULAB_TESTER_TYPE")
fi

echo "Registering resource providers..."
for ns in Microsoft.OperationalInsights Microsoft.OperationsManagement Microsoft.SecurityInsights Microsoft.Insights \
          Microsoft.Web Microsoft.App Microsoft.Storage Microsoft.KeyVault Microsoft.Network \
          Microsoft.Compute Microsoft.ManagedIdentity Microsoft.DevTestLab Microsoft.Consumption; do
  STATE=$(az provider show --namespace "$ns" --query registrationState -o tsv)
  if [[ "$STATE" != Registered ]]; then
    echo "Registering $ns..."
    az provider register --namespace "$ns" --wait -o none
  fi
done

echo "Validating..."
az deployment sub what-if --location "$LOCATION" --name "$NAME" \
  --template-file main.bicep --parameters main.bicepparam "${EXTRA[@]}"

echo "Deploying $NAME..."
az deployment sub create --location "$LOCATION" --name "$NAME" \
  --template-file main.bicep --parameters main.bicepparam "${EXTRA[@]}" -o none

OUT=$(az deployment sub show --name "$NAME" --query properties.outputs -o json)
FUNC=$(jq -r .functionAppName.value <<<"$OUT")
RG_SHARED=$(jq -r .sharedResourceGroup.value <<<"$OUT")

# Hand each utility's ingestion IDs to the generator (deployment outputs, not secrets).
echo "Configuring generator settings..."
az functionapp config appsettings set -g "$RG_SHARED" -n "$FUNC" -o none --settings \
  "SWU_UTILITIES=$(jq -c .utilityIngestion.value <<<"$OUT")"

# If the Teams playbook was deployed, let Sentinel run it (one-time, tenant-specific).
if az logic workflow show -g "$RG_SHARED" -n logic-swulab-teams -o none 2>/dev/null; then
  SENTINEL_SP=$(az ad sp show --id 98785600-1bb7-4fb9-b9fa-19afe2c8a360 --query id -o tsv)
  az role assignment create --assignee-object-id "$SENTINEL_SP" --assignee-principal-type ServicePrincipal \
    --role "Microsoft Sentinel Automation Contributor" \
    --scope "$(az group show -n "$RG_SHARED" --query id -o tsv)" -o none
  echo "Playbook deployed: authorize the 'teams-swulab' API connection once in the portal."
fi

echo "$OUT" | jq --arg subscription "$SUBSCRIPTION" --arg location "$LOCATION" '{
  subscription: $subscription, location: $location,
  functionApp: .functionAppName.value, dce: .dceEndpoint.value,
  queue: .scenarioQueueUrl.value, utilities: .utilityIngestion.value,
  prefix: .prefix.value, resourceGroups: .resourceGroups.value,
  sharedResourceGroup: .sharedResourceGroup.value, utilityAResourceGroup: .utilityAResourceGroup.value,
  gatewayVmName: .gatewayVmName.value, attackerVmName: .attackerVmName.value,
  hmiIp: .hmiIp.value, deployedAt: .deployedAt.value, agentClientId: .agentClientId.value,
  vmSize: .utilityAVmSize.value,
  operatorVmName: .operatorVmName.value, operatorPrincipalId: .operatorPrincipalId.value,
  sharedWorkspaceId: .sharedWorkspaceId.value, budgetAmount: .approvedBudgetAmount.value
}' > "$ROOT/deployment-outputs.json"
cd "$ROOT"
bash scripts/deploy-generator.sh
bash scripts/deploy-apps.sh
bash scripts/lockdown.sh
bash scripts/verify-operator.sh
echo "Deployment and lockdown completed. Live warm-up and acceptance remain required."
