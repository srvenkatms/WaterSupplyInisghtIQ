#!/usr/bin/env bash
# Builds and starts Utility A's containers on its VMs (FR-25 to FR-30).
# Run after deploy.sh and BEFORE lockdown.sh: the VMs download the Python
# base image and packages while build egress is still open.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=deployment-outputs.json
RG=$(jq -r .utilityAResourceGroup "$OUT")
DCE=$(jq -r .dce "$OUT")
DCR=$(jq -r '.utilities[] | select(.utilityId=="uta") | .dcrImmutableId' "$OUT")
PLC_IP=10.10.1.10

STAGE=$(mktemp -d)
trap 'rm -rf -- "$STAGE"' EXIT
cp -r apps/Dockerfile apps/requirements.txt apps/swuapps apps/openplc generator/swugen profiles "$STAGE/"
if [[ "$(jq -r '.operatorVmName // ""' "$OUT")" != "" ]]; then
  cp -r harness rules "$STAGE/"
  mkdir -p "$STAGE/infra"
  cp infra/tables.json "$STAGE/infra/"
fi
mkdir -p "$STAGE/wheelhouse"
SRC=$(tar -C "$STAGE" --exclude=__pycache__ -czf - . | base64 -w0)

command_on() {
  local vm=$1 script=$2 result
  result=$(az vm run-command invoke -g "$RG" -n "$vm" --command-id RunShellScript \
    --scripts "bash -s -- <<'SWULAB_APPLICATION_SCRIPT'
set -euo pipefail
$script
echo __SWULAB_COMMAND_OK__
SWULAB_APPLICATION_SCRIPT" -o json)
  if ! jq -e '[.value[].message] | join("\n") | contains("__SWULAB_COMMAND_OK__")' <<<"$result" >/dev/null; then
    jq -r '.value[].message' <<<"$result" >&2
    return 1
  fi
}
run_on() {
  local vm=$1; shift
  local build="docker build -q -t swu-apps /opt/swu/src"
  if [[ "$vm" == vm-uta-operator ]]; then
    build="docker build -q -f /opt/swu/src/harness/Dockerfile -t swu-operator /opt/swu/src"
  fi
  echo "== $vm"
  az vm start -g "$RG" -n "$vm" -o none
  command_on "$vm" "install -d /opt/swu/src; : > /opt/swu/src.b64; cloud-init status --wait"
  for ((offset=0; offset<${#SRC}; offset+=16000)); do
    command_on "$vm" "printf '%s' '${SRC:offset:16000}' >> /opt/swu/src.b64"
  done
  command_on "$vm" "base64 -d /opt/swu/src.b64 | tar -xzf - -C /opt/swu/src
$build
$*"
}
start() {    # start <name> <docker run options and command>
  local name=$1; shift
  echo "docker rm -f $name >/dev/null 2>&1 || true; docker run -d --name $name --restart unless-stopped --network host $*"
}

run_on vm-uta-plc "install -d /opt/swu/sensors
docker build -q -t swu-openplc /opt/swu/src/openplc
$(start swu-plant -v /opt/swu/sensors:/sensors -e SWU_SENSOR_FILE=/sensors/inputs.bin -e SWU_PLC_HOST=127.0.0.1 -e SWU_MODBUS_PORT=1502 swu-apps python -m swuapps.plant)
docker rm -f swu-plc >/dev/null 2>&1 || true
docker run -d --name swu-plc --restart unless-stopped -p 127.0.0.1:1502:1502 -v /opt/swu/sensors:/sensors:ro -v /opt/swu/src/profiles/uta.json:/config/profile.json:ro swu-openplc
$(start swu-proxy -e SWU_MODBUS_WRITERS=10.10.1.10,10.10.1.11,10.10.4.10 swu-apps python -m swuapps.modbus_proxy)
for i in \$(seq 1 60); do
  [ \"\$(docker inspect -f '{{.State.Health.Status}}' swu-plc)\" = healthy ] && break
  sleep 2
done
[ \"\$(docker inspect -f '{{.State.Health.Status}}' swu-plc)\" = healthy ]"

HMI_IP=$(jq -r .hmiIp "$OUT")
if [[ "$HMI_IP" != "$PLC_IP" ]]; then
  run_on vm-uta-hmi "$(start swu-hmi -e SWU_PLC_HOST=$PLC_IP swu-apps python -m swuapps.hmi)"
fi
if [[ "$HMI_IP" == "$PLC_IP" ]]; then
  command_on vm-uta-plc "$(start swu-hmi -e SWU_PLC_HOST=$PLC_IP swu-apps python -m swuapps.hmi)"
fi

run_on vm-uta-gw "install -d /opt/swu/gateway
$(start swu-gateway -v /opt/swu/gateway:/state -e SWU_PLC_HOST=$PLC_IP -e SWU_DCE_ENDPOINT=$DCE -e SWU_DCR_IMMUTABLE_ID=$DCR -e SWU_STATE_DIR=/state swu-apps python -m swuapps.gateway)"

run_on vm-uta-attack "$(start swu-attack -e SWU_PLC_HOST=$PLC_IP -e SWU_HMI_URL=http://$HMI_IP:1881 swu-apps sleep infinity)
docker exec swu-attack python -m swuapps.attack scan >/dev/null
docker exec swu-attack python -m swuapps.attack hmi-scan >/dev/null"

if [[ "$(jq -r '.operatorVmName // ""' "$OUT")" != "" ]]; then
  MANIFEST=$(base64 -w0 "$OUT")
  HOST_KEYS=""
  for role in attack gateway plc; do
    VM=vm-uta-attack
    [[ "$role" == gateway ]] && VM=vm-uta-gw
    [[ "$role" == plc ]] && VM=vm-uta-plc
    RESULT=$(az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
      --scripts 'cat /etc/ssh/ssh_host_ed25519_key.pub' -o json)
    KEY=$(jq -r '.value[].message' <<<"$RESULT" | grep '^ssh-ed25519 ' | head -n1)
    [[ -n "$KEY" ]] || { echo "Missing SSH host key for $role" >&2; exit 1; }
    HOST_KEYS+="swu-${role} ${KEY}"$'\n'
  done
  HOST_KEYS_B64=$(printf '%s' "$HOST_KEYS" | base64 -w0)
  run_on vm-uta-operator "install -d -m 700 /opt/swu/operator /opt/swu/operator/state
printf '%s' '$MANIFEST' | base64 -d > /opt/swu/operator/deployment-outputs.json
printf '%s' '$HOST_KEYS_B64' | base64 -d > /opt/swu/operator/known_hosts
chmod 600 /opt/swu/operator/known_hosts"
  python scripts/deploy-operator.py
fi

echo "Utility A apps running. Check: az vm run-command invoke -g $RG -n vm-uta-gw --command-id RunShellScript --scripts 'docker logs --tail 20 swu-gateway'"
echo "Next: ./scripts/lockdown.sh"
