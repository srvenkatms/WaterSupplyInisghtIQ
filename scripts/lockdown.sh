#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=deployment-outputs.json
az account set --subscription "$(jq -r .subscription "$OUT")"
RG=$(jq -r .utilityAResourceGroup "$OUT")
PREFIX=$(jq -r .prefix "$OUT")
[[ "$PREFIX" =~ ^[a-z][a-z0-9]{2,7}$ && "$RG" == "rg-$PREFIX-uta" ]] || { echo "Unexpected network manifest" >&2; exit 1; }
[[ "$(az group show -n "$RG" --query tags.project -o tsv)" == swu-lab ]] || { echo "Not a lab resource group" >&2; exit 1; }
[[ "$(az group show -n "$RG" --query tags.deployment -o tsv)" == "$PREFIX" ]] || { echo "Deployment tag mismatch" >&2; exit 1; }
DENY_ROUTES=$(az network route-table route list -g "$RG" --route-table-name "rt-${PREFIX}-uta-isolated" \
  --query "[?addressPrefix=='0.0.0.0/0' && nextHopType=='None'] | length(@)" -o tsv)
[[ "$DENY_ROUTES" == 1 ]] || { echo "Isolated route table lacks the null Internet route" >&2; exit 1; }
ZONES=(boundary control test)
[[ "$(jq -r '.operatorVmName // ""' "$OUT")" == "" ]] || ZONES+=(operator)
for zone in "${ZONES[@]}"; do
  NSG="nsg-${PREFIX}-uta-${zone}"
  if az network nsg rule show -g "$RG" --nsg-name "$NSG" -n allow-build-egress -o none >/dev/null 2>&1; then
    az network nsg rule delete -g "$RG" --nsg-name "$NSG" -n allow-build-egress
  fi
  COUNT=$(az network nsg rule list -g "$RG" --nsg-name "$NSG" --query "[?name=='allow-build-egress'] | length(@)" -o tsv)
  [[ "$COUNT" == 0 ]] || { echo "Lockdown failed for $NSG" >&2; exit 1; }
done
for zone in control test; do
  UPDATE=(az network vnet subnet update -g "$RG" --vnet-name "vnet-${PREFIX}-uta" -n "snet-$zone"
          --route-table "rt-${PREFIX}-uta-isolated" -o none)
  BEFORE=$(az network vnet subnet show -g "$RG" --vnet-name "vnet-${PREFIX}-uta" -n "snet-$zone" --query natGateway.id -o tsv)
  if [[ -n "$BEFORE" ]]; then
    UPDATE+=(--remove natGateway)
  fi
  "${UPDATE[@]}"
  NAT=$(az network vnet subnet show -g "$RG" --vnet-name "vnet-${PREFIX}-uta" -n "snet-$zone" --query natGateway.id -o tsv)
  [[ -z "$NAT" ]] || { echo "NAT still attached to $zone" >&2; exit 1; }
  ROUTE=$(az network vnet subnet show -g "$RG" --vnet-name "vnet-${PREFIX}-uta" -n "snet-$zone" --query routeTable.id -o tsv)
  [[ "$ROUTE" == *"/rt-${PREFIX}-uta-isolated" ]] || { echo "Public-egress deny route missing on $zone" >&2; exit 1; }
done
echo "Temporary VM build egress removed."
