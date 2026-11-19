#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=deployment-outputs.json
[[ "$(jq -r '.operatorVmName // ""' "$OUT")" != "" ]] || exit 0
SUB=$(jq -r .subscription "$OUT")
RG=$(jq -r .utilityAResourceGroup "$OUT")
PREFIX=$(jq -r .prefix "$OUT")
[[ "$PREFIX" =~ ^[a-z][a-z0-9]{2,7}$ && "$RG" == "rg-$PREFIX-uta" ]] || { echo "Unexpected operator manifest" >&2; exit 1; }
az account set --subscription "$SUB"
[[ "$(az group show -n "$RG" --query tags.project -o tsv)" == swu-lab ]] || { echo "Not a lab resource group" >&2; exit 1; }
[[ "$(az group show -n "$RG" --query tags.deployment -o tsv)" == "$PREFIX" ]] || { echo "Deployment tag mismatch" >&2; exit 1; }
COUNT=$(az network nsg rule list -g "$RG" --nsg-name "nsg-${PREFIX}-uta-operator" \
  --query "[?name=='allow-build-egress'] | length(@)" -o tsv)
[[ "$COUNT" == 0 ]] || { echo "Operator bootstrap egress has not been removed" >&2; exit 1; }

# Restart establishes new identity/API/tunnel connections, rather than relying
# on NSG state retained by connections opened before lockdown.
RESULT=$(az vm run-command invoke -g "$RG" -n vm-uta-operator --command-id RunShellScript --scripts '
bash -s -- <<'"'"'SWULAB_VERIFICATION_SCRIPT'"'"'
set -euo pipefail
docker restart swu-operator >/dev/null
READY=false
for i in $(seq 1 90); do
  if [ "$(docker inspect -f "{{.State.Health.Status}}" swu-operator)" = healthy ]; then READY=true; break; fi
  sleep 5
done
[ "$READY" = true ] || { echo "Post-lockdown Bastion/SSH readiness failed" >&2; exit 1; }
for i in $(seq 1 10); do
  if (
    set -e
    for command in schema-check content-check history-check health network-check; do
      docker exec swu-operator /opt/harness/bin/python /lab/harness/swuharness.py "$command" || exit $?
    done
  ) > /opt/swu/operator/state/post-lockdown.log 2>&1; then
    cat /opt/swu/operator/state/post-lockdown.log
    python3 - <<'"'"'SWULAB_WARMUP'"'"'
import json
from datetime import datetime, timezone
from pathlib import Path
path = Path("/opt/swu/operator/deployment-outputs.json")
manifest = json.loads(path.read_text())
stamp = manifest.setdefault("warmupStartedAt", datetime.now(timezone.utc).isoformat())
path.write_text(json.dumps(manifest, indent=2) + "\n")
print("__SWULAB_POST_LOCKDOWN_OK__ " + stamp)
SWULAB_WARMUP
    exit 0
  fi
  cat /opt/swu/operator/state/post-lockdown.log >&2
  echo "Live schema/content/health check attempt $i failed; retrying initial deployment convergence" >&2
  sleep 30
done
echo "Post-lockdown query/ingestion checks did not converge" >&2
exit 1
SWULAB_VERIFICATION_SCRIPT
' -o json)
if ! jq -e '[.value[].message] | join("\n") | contains("__SWULAB_POST_LOCKDOWN_OK__")' <<<"$RESULT" >/dev/null; then
  jq -r '.value[].message' <<<"$RESULT" >&2
  exit 1
fi
jq -r '.value[].message' <<<"$RESULT"
STAMP=$(jq -r '.value[].message' <<<"$RESULT" | sed -n 's/^__SWULAB_POST_LOCKDOWN_OK__ //p' | head -n1)
[[ -n "$STAMP" ]] || { echo "Private proof returned no warm-up start timestamp" >&2; exit 1; }
python - "$STAMP" <<'SWULAB_WARMUP'
import json
import sys
from datetime import datetime
from pathlib import Path
stamp = datetime.fromisoformat(sys.argv[1])
if stamp.tzinfo is None:
    raise ValueError("Warm-up start requires a timezone-aware timestamp")
path = Path("deployment-outputs.json")
manifest = json.loads(path.read_text())
manifest["warmupStartedAt"] = stamp.isoformat()
path.write_text(json.dumps(manifest, indent=2) + "\n")
SWULAB_WARMUP
echo "Private operator post-lockdown proof recorded; warm-up and scenario acceptance remain separate."
