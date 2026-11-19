#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=deployment-outputs.json
[[ -f "$OUT" ]] || { echo "Missing deployment manifest; refusing tag-wide deletion" >&2; exit 1; }
SUBSCRIPTION=$(jq -er .subscription "$OUT")
PREFIX=$(jq -er .prefix "$OUT")
[[ "$PREFIX" =~ ^[a-z][a-z0-9]{2,7}$ ]] || { echo "Invalid prefix in manifest" >&2; exit 1; }
[[ "${1:-}" == "$SUBSCRIPTION" ]] || { echo "Confirm the deployment subscription: bash scripts/teardown.sh $SUBSCRIPTION" >&2; exit 1; }
az account set --subscription "$SUBSCRIPTION"
GROUPS_JSON=$(jq -ec '.resourceGroups | select(type == "array" and length > 0 and all(.[]; type == "string"))' "$OUT")
mapfile -t GROUPS_TO_DELETE < <(jq -r '.[]' <<<"$GROUPS_JSON")
for rg in "${GROUPS_TO_DELETE[@]}"; do
  [[ "$rg" == "rg-$PREFIX-"* ]] || { echo "Unexpected group in manifest: $rg" >&2; exit 1; }
  EXISTS=$(az group exists -n "$rg" -o tsv)
  if [[ "$EXISTS" == false ]]; then
    continue
  fi
  TAG=$(az group show -n "$rg" --query tags.project -o tsv)
  [[ "$TAG" == swu-lab ]] || { echo "Refusing to delete untagged group $rg" >&2; exit 1; }
  DEPLOYMENT=$(az group show -n "$rg" --query tags.deployment -o tsv)
  [[ "$DEPLOYMENT" == "$PREFIX" ]] || { echo "Refusing group from another deployment: $rg" >&2; exit 1; }
done
for rg in "${GROUPS_TO_DELETE[@]}"; do
  if [[ "$(az group exists -n "$rg" -o tsv)" == true ]]; then
    az group delete -n "$rg" --yes
  fi
  if az group exists -n "$rg" -o tsv | grep -qx true; then
    echo "Resource group still exists: $rg" >&2
    exit 1
  fi
done
BUDGETS=$(az consumption budget list -o json)
if jq -e --arg name "budget-$PREFIX" 'any(.[]; .name == $name)' <<<"$BUDGETS" >/dev/null; then
  az consumption budget delete --budget-name "budget-$PREFIX"
fi
echo "Manifest-scoped teardown completed. Soft-deleted Key Vault remains recoverable; purge is a separate explicit action."
