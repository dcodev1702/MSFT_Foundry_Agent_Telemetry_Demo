#!/usr/bin/env bash
set -euo pipefail

if command -v az.cmd >/dev/null 2>&1 && command -v cmd.exe >/dev/null 2>&1; then
    az() {
        cmd.exe /c az.cmd "$@"
    }
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

SUFFIX="${SUFFIX:-botprd}"
BOT_NAME="${BOT_NAME:-zolab-bot-ca-${SUFFIX}-vnet}"
BOT_RG="${BOT_RG:-zolab-bot-${SUFFIX}}"
WORKER_NAME="${WORKER_NAME:-zolab-worker-aci-${SUFFIX}}"
WORKER_RG="${WORKER_RG:-zolab-worker-${SUFFIX}}"
RUN_UNIT_TESTS="${RUN_UNIT_TESTS:-true}"
SMOKE_PUBLISH_AND_DEPLOY="${SMOKE_PUBLISH_AND_DEPLOY:-false}"
SMOKE_PUBLISH_BOT="${SMOKE_PUBLISH_BOT:-${SMOKE_PUBLISH_AND_DEPLOY}}"
SMOKE_PUBLISH_WORKER="${SMOKE_PUBLISH_WORKER:-${SMOKE_PUBLISH_AND_DEPLOY}}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --run-unit-tests)
            RUN_UNIT_TESTS="true"
            ;;
        --skip-unit-tests)
            RUN_UNIT_TESTS="false"
            ;;
        --publish-and-deploy)
            SMOKE_PUBLISH_AND_DEPLOY="true"
            SMOKE_PUBLISH_BOT="true"
            SMOKE_PUBLISH_WORKER="true"
            ;;
        --publish-bot)
            SMOKE_PUBLISH_BOT="true"
            ;;
        --publish-worker)
            SMOKE_PUBLISH_WORKER="true"
            ;;
        --no-publish-bot)
            SMOKE_PUBLISH_BOT="false"
            ;;
        --no-publish-worker)
            SMOKE_PUBLISH_WORKER="false"
            ;;
        *)
            echo "ERROR: Unknown argument '$1'" >&2
            echo "Usage: bash deployment/run-smoke-checks.sh [--run-unit-tests|--skip-unit-tests] [--publish-and-deploy|--publish-bot|--publish-worker|--no-publish-bot|--no-publish-worker]" >&2
            exit 1
            ;;
    esac
    shift
done

normalize_azure_cli_config_dir() {
    if [[ -n "${AZURE_CONFIG_DIR:-}" ]]; then
        return 0
    fi

    local windows_profile=""
    local unix_profile=""

    if command -v powershell.exe >/dev/null 2>&1; then
        windows_profile="$(powershell.exe -NoProfile -Command '[Environment]::GetFolderPath("UserProfile")' 2>/dev/null | tr -d '\r')"
    elif command -v cmd.exe >/dev/null 2>&1; then
        windows_profile="$(cmd.exe /c "echo %USERPROFILE%" 2>/dev/null | tr -d '\r')"
    fi

    if [[ -z "${windows_profile}" ]]; then
        return 0
    fi

    if command -v wslpath >/dev/null 2>&1; then
        unix_profile="$(wslpath -u "${windows_profile}")"
    elif command -v cygpath >/dev/null 2>&1; then
        unix_profile="$(cygpath "${windows_profile}")"
    else
        unix_profile="$(printf '%s' "${windows_profile}" | sed -E 's#^([A-Za-z]):#/\L\1#; s#\\#/#g')"
    fi

    if [[ -n "${unix_profile}" && -d "${unix_profile}/.azure" ]]; then
        export AZURE_CONFIG_DIR="${unix_profile}/.azure"
    fi
}

normalize_azure_cli_config_dir

resolve_python() {
    if [[ -x "${REPO_ROOT}/.venv/Scripts/python.exe" ]]; then
        printf '%s\n' "${REPO_ROOT}/.venv/Scripts/python.exe"
        return 0
    fi

    if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
        printf '%s\n' "${REPO_ROOT}/.venv/bin/python"
        return 0
    fi

    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return 0
    fi

    if command -v python >/dev/null 2>&1; then
        command -v python
        return 0
    fi

    echo "ERROR: Could not find a Python interpreter for smoke-test unit tests." >&2
    exit 1
}

if ! az account show >/dev/null 2>&1; then
    echo "ERROR: Not logged in. Run 'az login' first." >&2
    exit 1
fi

PYTHON_BIN="$(resolve_python)"

if [[ "${RUN_UNIT_TESTS}" == "true" ]]; then
    echo "== Runtime Unit Tests =="
    "${PYTHON_BIN}" -m unittest discover -s bot-app/runtime/tests -p 'test_*.py'
    echo ""
fi

if [[ "${SMOKE_PUBLISH_BOT}" == "true" ]]; then
    echo "== Publish And Deploy Bot =="
    bash bot-app/deployment/deploy-bot-app.sh
    echo ""
fi

if [[ "${SMOKE_PUBLISH_WORKER}" == "true" ]]; then
    echo "== Publish And Deploy Worker =="
    bash deployment/deploy-worker-app.sh
    echo ""
fi

latest_build_info=""
if ls build_info-*.json >/dev/null 2>&1; then
    latest_build_info="$(ls -t build_info-*.json | head -n 1)"
fi

latest_rg=""
if [[ -n "${latest_build_info}" ]]; then
    latest_rg="$("${PYTHON_BIN}" - <<'PY' | tr -d '\r'
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

bot_fqdn="$(az containerapp show --name "${BOT_NAME}" --resource-group "${BOT_RG}" --query 'properties.configuration.ingress.fqdn' -o tsv 2>/dev/null | tr -d '\r' || true)"
if [[ -n "${bot_fqdn}" ]]; then
        echo ""
        echo "== Bot HTTP Health =="
        curl --fail --silent --show-error --retry 5 --retry-all-errors "https://${bot_fqdn}/api/messages"
fi

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
echo "1. Send 'heartbeat'"
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