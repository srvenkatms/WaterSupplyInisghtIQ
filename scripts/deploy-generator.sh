#!/usr/bin/env bash
# Publishes the generator function app code (ARC-04). Run after deploy.sh.
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."
FUNC=$(jq -r .functionApp deployment-outputs.json)
BUILD=$(mktemp -d)
trap 'rm -rf -- "$BUILD"' EXIT
cp -r generator/function_app.py generator/host.json generator/requirements.txt generator/swugen profiles "$BUILD/"
export SWU_PROFILE_DIR="$BUILD/profiles"
find "$BUILD" -type d -name __pycache__ -prune -exec rm -r {} \;
(cd "$BUILD" && python -m zipfile -c generator.zip function_app.py host.json requirements.txt swugen profiles)
PACKAGE=$(cd "$BUILD" && python -c 'from pathlib import Path; print(Path("generator.zip").resolve())')
RG=$(jq -r .sharedResourceGroup deployment-outputs.json)
az functionapp config appsettings set -g "$RG" -n "$FUNC" --settings SWU_PROFILE_DIR=/home/site/wwwroot/profiles -o none
for attempt in {1..6}; do
  if az functionapp deployment source config-zip -g "$RG" -n "$FUNC" \
    --src "$PACKAGE" --build-remote true -o none > "$BUILD/publish.log" 2>&1; then
    cat "$BUILD/publish.log"
    break
  fi
  cat "$BUILD/publish.log" >&2
  if [[ "$attempt" == 6 ]] || ! grep -q 'BlobUploadFailedException.*403' "$BUILD/publish.log"; then
    exit 1
  fi
  echo "Deployment storage access failed (attempt $attempt/6); waiting 60s for private networking/RBAC convergence." >&2
  sleep 60
done
echo "Generator published to $FUNC. It starts writing on the next minute."
