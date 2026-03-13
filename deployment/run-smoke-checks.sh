#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

BOT_NAME="${BOT_NAME:-zolab-bot-ca-botprd-vnet}"
BOT_RG="zolab-bot-botprd"
WORKER_NAME="zolab-worker-aci-botprd"
WORKER_RG="zolab-worker-botprd"

if ! az account show >/dev/null 2>&1; then
    echo "ERROR: Not logged in. Run 'az login' first." >&2
    exit 1
fi

latest_build_info=""
if ls build_info-*.json >/dev/null 2>&1; then
    latest_build_info="$(ls -t build_info-*.json | head -n 1)"
fi

latest_rg=""
if [[ -n "${latest_build_info}" ]]; then
    latest_rg="$(python3 - <<'PY'
import json
from pathlib import Path

paths = sorted(Path('.').glob('build_info-*.json'), key=lambda path: path.stat().st_mtime, reverse=True)
if paths:
    payload = json.loads(paths[0].read_text(encoding='utf-8'))
    print(payload.get('rg', ''))
PY
    )"
fi

echo "== Bot Container App =="
az containerapp revision list \
  --name "${BOT_NAME}" \
  --resource-group "${BOT_RG}" \
  --query "[].{name:name,active:properties.active,traffic:properties.trafficWeight,health:properties.healthState,image:properties.template.containers[0].image}" \
  -o table

echo ""
echo "== Worker Container Instance =="
az container show \
  --name "${WORKER_NAME}" \
  --resource-group "${WORKER_RG}" \
  --query '{image:containers[0].image,state:instanceView.state,provisioning:provisioningState,startTime:containers[0].instanceView.currentState.startTime}' \
  -o table

echo ""
echo "== Worker Build Metadata =="
az container exec \
  --name "${WORKER_NAME}" \
  --resource-group "${WORKER_RG}" \
  --exec-command "cat /app/worker-build-info.json"

echo ""
echo "== Manual Teams Smoke Checks =="
echo "1. Send 'health'"
echo "2. Send 'listener status'"
echo "3. Send 'list builds'"
if [[ -n "${latest_rg}" ]]; then
    echo "4. Send 'build status ${latest_rg}'"
    echo "5. Optional: send 'teardown ${latest_rg}' to verify the preview/confirmation flow"
else
    echo "4. Send 'build status <resource-group>' for one active deployment"
    echo "5. Optional: send 'teardown <resource-group>' to verify the preview/confirmation flow"
fi

if [[ -n "${latest_build_info}" ]]; then
    echo ""
    echo "Latest build info file: ${latest_build_info}"
fi