#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
# deploy-bot-app.sh — Deploy Bot Container App + container image
#
# Runs steps 1–4 to bring the bot online:
#   1. Build & push bot container image to ACR
#   2. Deploy Bicep (Container Apps Env + Container App + Bot Service)
#   3. Grant bot UAMI Storage RBAC on worker storage account
#   4. Verify deployment and show endpoint
#
# Usage (from repo root):
#   bash bot-app/deployment/deploy-bot-app.sh
#
# Prerequisites:
#   - az login (authenticated)
#   - local Docker daemon available
# ════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Configuration ────────────────────────────────────────────────
SUFFIX="botprd"
TEAMS_APP_ID="${TEAMS_APP_ID:-ed77d99f-074b-4ef6-9fbc-55bfeb7b5aef}"
LOCATION="eastus2"

RG_BOT="zolab-bot-${SUFFIX}"
CONTAINER_APP_NAME="${CONTAINER_APP_NAME:-zolab-bot-ca-${SUFFIX}-vnet}"
CONTAINER_ENV_NAME="${CONTAINER_ENV_NAME:-zolab-bot-env-${SUFFIX}-vnet}"
BOT_SERVICE_NAME="zolab-bot-${SUFFIX}"
BOT_ACR_NAME="zolabbotacr${SUFFIX}"

WORKER_STORAGE_ACCOUNT="zolabworkerst${SUFFIX}"
WORKER_RG="zolab-worker-${SUFFIX}"
WORKER_ACI_NAME="zolab-worker-aci-${SUFFIX}"
BOT_MANAGED_IDENTITY_NAME="${BOT_MANAGED_IDENTITY_NAME:-zolab-bot-mi-${SUFFIX}}"
ENABLE_PRIVATE_CONTAINER_APPS_NETWORKING="${ENABLE_PRIVATE_CONTAINER_APPS_NETWORKING:-true}"
CONTAINER_APPS_INFRASTRUCTURE_SUBNET_RESOURCE_ID="${CONTAINER_APPS_INFRASTRUCTURE_SUBNET_RESOURCE_ID:-}"
PREFLIGHT_ONLY="${PREFLIGHT_ONLY:-false}"
REQUIRE_LAW_SHARED_KEY="${REQUIRE_LAW_SHARED_KEY:-true}"

# DIBSecCom LAW in Security subscription — all logs go here
SECURITY_SUB="${SECURITY_SUB:-}"
SECURITY_SUB_NAME="${SECURITY_SUB_NAME:-Security}"
LAW_RG="Sentinel"
LAW_NAME="DIBSecCom"

# ── Resolve repo root ───────────────────────────────────────────
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"
TEAMS_APP_DIR="${REPO_ROOT}/bot-app/teams-app"
TEAMS_MANIFEST_TEMPLATE="${TEAMS_APP_DIR}/manifest.template.json"
TEAMS_MANIFEST_PATH="${TEAMS_APP_DIR}/manifest.json"
TEAMS_ZIP_PATH="${TEAMS_APP_DIR}/Bot-The-Builder.zip"

WEATHER_LLM_MODEL="${WEATHER_LLM_MODEL:-gpt-5.3-chat}"
WEATHER_LLM_API_VERSION="${WEATHER_LLM_API_VERSION:-2024-10-21}"
WEATHER_LLM_MODEL_NAME="${WEATHER_LLM_MODEL_NAME:-gpt-5.3-chat}"
WEATHER_LLM_MODEL_VERSION="${WEATHER_LLM_MODEL_VERSION:-2026-03-03}"
WEATHER_LLM_MODEL_FORMAT="${WEATHER_LLM_MODEL_FORMAT:-OpenAI}"
WEATHER_LLM_SKU_NAME="${WEATHER_LLM_SKU_NAME:-GlobalStandard}"
WEATHER_LLM_SKU_CAPACITY="${WEATHER_LLM_SKU_CAPACITY:-50}"
HEARTBEAT_INTERVAL_SECONDS="${HEARTBEAT_INTERVAL_SECONDS:-21600}"
WORKER_SYNC_MAX_ATTEMPTS="${WORKER_SYNC_MAX_ATTEMPTS:-6}"
WORKER_SYNC_RETRY_DELAY_SECONDS="${WORKER_SYNC_RETRY_DELAY_SECONDS:-20}"
WORKER_SYNC_MAX_RETRY_DELAY_SECONDS="${WORKER_SYNC_MAX_RETRY_DELAY_SECONDS:-90}"
WORKER_SYNC_DEPLOYMENT_NAME="${WORKER_SYNC_DEPLOYMENT_NAME:-worker-infra}"
WORKER_SYNC_RESOURCE_GROUP_DEPLOYMENT_NAME="${WORKER_SYNC_RESOURCE_GROUP_DEPLOYMENT_NAME:-worker-resources-${SUFFIX}}"
WORKER_SYNC_VERIFY_MAX_ATTEMPTS="${WORKER_SYNC_VERIFY_MAX_ATTEMPTS:-12}"
WORKER_SYNC_VERIFY_DELAY_SECONDS="${WORKER_SYNC_VERIFY_DELAY_SECONDS:-10}"

fail_access_preflight() {
  local message="$1"
  echo "ERROR: ${message}" >&2
  echo "Resume after you have the required Azure permissions or PIM elevation, then rerun bash bot-app/deployment/deploy-bot-app.sh." >&2
  exit 1
}

resolve_law_subscription_id() {
  local subscription_id

  subscription_id="$(az account list --all --query "[?name=='${SECURITY_SUB_NAME}'].id | [0]" -o tsv 2>/dev/null || true)"
  if [[ -n "${subscription_id}" ]]; then
    echo "${subscription_id}"
    return 0
  fi

  while IFS=$'\t' read -r subscription_id _; do
    [[ -z "${subscription_id}" ]] && continue

    if az monitor log-analytics workspace show \
      --subscription "${subscription_id}" \
      --resource-group "${LAW_RG}" \
      --workspace-name "${LAW_NAME}" \
      --query id -o tsv >/dev/null 2>&1; then
      echo "${subscription_id}"
      return 0
    fi
  done < <(az account list --all --query "[].{id:id,name:name}" -o tsv)

  return 1
}

get_current_azure_role_check_assignee() {
  if [[ -n "${AZURE_CLIENT_ID:-}" ]]; then
    printf '%s\n' "${AZURE_CLIENT_ID}"
    return 0
  fi

  az account show --query 'user.name' -o tsv 2>/dev/null || true
}

get_azure_role_names_for_assignee() {
  local assignee="$1"
  local scope="$2"

  az role assignment list \
    --assignee "${assignee}" \
    --scope "${scope}" \
    --query '[].roleDefinitionName' \
    -o tsv 2>/dev/null || true
}

test_azure_role_assignment_write_access() {
  local assignee="$1"
  shift

  local scope
  local role_name
  for scope in "$@"; do
    [[ -z "${scope}" ]] && continue
    while IFS= read -r role_name; do
      [[ -z "${role_name}" ]] && continue
      if [[ "${role_name}" == "Owner" || "${role_name}" == "User Access Administrator" ]]; then
        return 0
      fi
    done < <(get_azure_role_names_for_assignee "${assignee}" "${scope}")
  done

  return 1
}

assert_bot_deployment_authorization() {
  local assignee
  assignee="$(get_current_azure_role_check_assignee)"

  if [[ -z "${assignee}" ]]; then
    fail_access_preflight "Unable to resolve the current Azure principal for deployment authorization preflight. Sign in with the deployment identity and retry."
  fi

  local subscription_scope="/subscriptions/${SUBSCRIPTION_ID}"
  local bot_resource_group_scope="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RG_BOT}"
  local worker_resource_group_scope="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${WORKER_RG}"
  local storage_scope="${STORAGE_SCOPE}"

  if ! test_azure_role_assignment_write_access "${assignee}" "${storage_scope}" "${worker_resource_group_scope}" "${bot_resource_group_scope}" "${subscription_scope}"; then
    fail_access_preflight "Azure principal '${assignee}' does not have permission to create the RBAC role assignments required by this rollout. Grant 'Owner' or 'User Access Administrator' on ${storage_scope}, ${worker_resource_group_scope}, ${bot_resource_group_scope}, or ${subscription_scope}."
  fi

  local validation_output
  set +e
  validation_output=$(az deployment sub validate \
    --location "${LOCATION}" \
    --template-file bot-app/deployment/bot-infra.bicep \
    --parameters \
      suffix="${SUFFIX}" \
      tenantId="${TENANT_ID}" \
      logAnalyticsCustomerId="${LAW_CUSTOMER_ID}" \
      logAnalyticsSharedKey="${LAW_SHARED_KEY}" \
      botImageTag="${BOT_IMAGE_TAG}" \
      weatherLlmModel="${WEATHER_LLM_MODEL}" \
      weatherLlmApiVersion="${WEATHER_LLM_API_VERSION}" \
      weatherLlmModelName="${WEATHER_LLM_MODEL_NAME}" \
      weatherLlmModelVersion="${WEATHER_LLM_MODEL_VERSION}" \
      weatherLlmModelFormat="${WEATHER_LLM_MODEL_FORMAT}" \
      weatherLlmSkuName="${WEATHER_LLM_SKU_NAME}" \
      weatherLlmSkuCapacity="${WEATHER_LLM_SKU_CAPACITY}" \
      heartbeatIntervalSeconds="${HEARTBEAT_INTERVAL_SECONDS}" \
      containerEnvName="${CONTAINER_ENV_NAME}" \
      containerAppName="${CONTAINER_APP_NAME}" \
      enablePrivateContainerAppsNetworking="${ENABLE_PRIVATE_CONTAINER_APPS_NETWORKING}" \
      containerAppsInfrastructureSubnetResourceId="${CONTAINER_APPS_INFRASTRUCTURE_SUBNET_RESOURCE_ID}" \
    --output none 2>&1)
  local validation_exit=$?
  set -e

  if (( validation_exit != 0 )); then
    if grep -Eqi 'AuthorizationFailed|LinkedAuthorizationFailed|does not have authorization|insufficient privileges|permission' <<<"${validation_output}"; then
      fail_access_preflight "Azure principal '${assignee}' does not have the required deployment access for bot-app/deployment/bot-infra.bicep at ${subscription_scope}. Validation failed before deployment with: ${validation_output}"
    fi

    echo "ERROR: Subscription deployment preflight validation failed." >&2
    echo "${validation_output}" >&2
    exit "${validation_exit}"
  fi

  echo "  ✓ Deployment authorization preflight passed for ${assignee}"
}

show_worker_sync_deployment_diagnostics() {
  local subscription_failures
  local resource_group_failures

  subscription_failures="$(az deployment sub operation list \
    --name "${WORKER_SYNC_DEPLOYMENT_NAME}" \
    --query "[?properties.provisioningState=='Failed'].{code:properties.statusMessage.error.code,message:properties.statusMessage.error.message}" \
    -o json 2>/dev/null || true)"

  resource_group_failures="$(az deployment group operation list \
    --resource-group "${WORKER_RG}" \
    --name "${WORKER_SYNC_RESOURCE_GROUP_DEPLOYMENT_NAME}" \
    --query "[?properties.provisioningState=='Failed'].{code:properties.statusMessage.error.code,message:properties.statusMessage.error.message}" \
    -o json 2>/dev/null || true)"

  if [[ -n "${subscription_failures}" && "${subscription_failures}" != "[]" ]]; then
    echo "  ! Worker sync subscription deployment failures: ${subscription_failures}"
  fi

  if [[ -n "${resource_group_failures}" && "${resource_group_failures}" != "[]" ]]; then
    echo "  ! Worker sync resource-group deployment failures: ${resource_group_failures}"
  fi
}

sync_worker_download_host() {
  local worker_image_tag="$1"
  local bot_fqdn="$2"
  local attempt
  local retry_delay="${WORKER_SYNC_RETRY_DELAY_SECONDS}"
  local sync_output
  local synced_worker_bot_fqdn

  for (( attempt = 1; attempt <= WORKER_SYNC_MAX_ATTEMPTS; attempt++ )); do
    if sync_output="$(az deployment sub create \
      --location "${LOCATION}" \
      --name "${WORKER_SYNC_DEPLOYMENT_NAME}" \
      --template-file deployment/worker-infra.bicep \
      --parameters \
        suffix="${SUFFIX}" \
        botClientId="${UAMI_CLIENT_ID}" \
        workerCpu=2 \
        workerMemoryInGb=4 \
        workerImageTag="${worker_image_tag}" \
        botFqdn="${bot_fqdn}" \
        enablePrivateStorageAccess=true \
      --output none 2>&1)"; then
      synced_worker_bot_fqdn="$(get_worker_bot_fqdn)"
      if verify_worker_bot_fqdn "${bot_fqdn}"; then
        echo "  ✓ Worker download host synced to ${bot_fqdn}"
        return 0
      fi

      echo "ERROR: Worker download-host sync deployment succeeded, but live BOT_FQDN is '${synced_worker_bot_fqdn:-<empty>}' instead of '${bot_fqdn}'." >&2
      return 1
    fi

    if [[ "${sync_output}" == *"AnotherOperationInProgress"* ]] && (( attempt < WORKER_SYNC_MAX_ATTEMPTS )); then
      echo "  ! Worker sync attempt ${attempt}/${WORKER_SYNC_MAX_ATTEMPTS} hit Azure network reconciliation in progress."
      show_worker_sync_deployment_diagnostics
      echo "  ! Retrying worker download-host sync in ${retry_delay}s"
      sleep "${retry_delay}"
      if (( retry_delay < WORKER_SYNC_MAX_RETRY_DELAY_SECONDS )); then
        retry_delay=$(( retry_delay * 2 ))
        if (( retry_delay > WORKER_SYNC_MAX_RETRY_DELAY_SECONDS )); then
          retry_delay="${WORKER_SYNC_MAX_RETRY_DELAY_SECONDS}"
        fi
      fi
      continue
    fi

    echo "ERROR: Worker download-host sync failed." >&2
    echo "${sync_output}" >&2
    show_worker_sync_deployment_diagnostics >&2
    return 1
  done

  echo "ERROR: Worker download-host sync did not complete after ${WORKER_SYNC_MAX_ATTEMPTS} attempts." >&2
  show_worker_sync_deployment_diagnostics >&2
  return 1
}

get_worker_bot_fqdn() {
  az container show \
    --name "${WORKER_ACI_NAME}" \
    --resource-group "${WORKER_RG}" \
    --query "containers[0].environmentVariables[?name=='BOT_FQDN'].value | [0]" \
    -o tsv 2>/dev/null || true
}

verify_worker_bot_fqdn() {
  local expected_bot_fqdn="$1"
  local observed_bot_fqdn
  local attempt

  for (( attempt = 1; attempt <= WORKER_SYNC_VERIFY_MAX_ATTEMPTS; attempt++ )); do
    observed_bot_fqdn="$(get_worker_bot_fqdn)"
    if [[ "${observed_bot_fqdn}" == "${expected_bot_fqdn}" ]]; then
      return 0
    fi

    if (( attempt < WORKER_SYNC_VERIFY_MAX_ATTEMPTS )); then
      echo "  ! Worker BOT_FQDN verification attempt ${attempt}/${WORKER_SYNC_VERIFY_MAX_ATTEMPTS} saw '${observed_bot_fqdn:-<empty>}' instead of '${expected_bot_fqdn}'."
      sleep "${WORKER_SYNC_VERIFY_DELAY_SECONDS}"
    fi
  done

  return 1
}

# ── Validate prerequisites ──────────────────────────────────────
if ! az account show &>/dev/null; then
    echo "ERROR: Not logged in. Run 'az login' first." >&2
    exit 1
fi

SUBSCRIPTION_ID="$(az account show --query id -o tsv)"

STORAGE_SCOPE="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${WORKER_RG}/providers/Microsoft.Storage/storageAccounts/${WORKER_STORAGE_ACCOUNT}"

TENANT_ID="${TENANT_ID:-$(az account show --query tenantId -o tsv)}"
if [[ -z "${SECURITY_SUB}" ]]; then
  SECURITY_SUB="$(resolve_law_subscription_id || true)"
fi

if [[ -z "${CONTAINER_APPS_INFRASTRUCTURE_SUBNET_RESOURCE_ID}" ]]; then
  CONTAINER_APPS_INFRASTRUCTURE_SUBNET_RESOURCE_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${WORKER_RG}/providers/Microsoft.Network/virtualNetworks/zolab-worker-vnet-${SUFFIX}/subnets/snet-containerapps"
fi

if ! docker version &>/dev/null; then
  echo "ERROR: Docker is not available. Start Docker Desktop and retry." >&2
  exit 1
fi

BOT_IMAGE_TAG="botfix-$(date -u +%Y%m%d%H%M%S)-$(git rev-parse --short HEAD)"

# Fetch DIBSecCom LAW credentials (cross-subscription)
LAW_CUSTOMER_ID=""
LAW_SHARED_KEY=""
if [[ -n "${SECURITY_SUB}" ]]; then
  LAW_CUSTOMER_ID=$(az monitor log-analytics workspace show \
    --resource-group "${LAW_RG}" --workspace-name "${LAW_NAME}" \
    --subscription "${SECURITY_SUB}" --query customerId -o tsv 2>/dev/null || true)
  LAW_SHARED_KEY=$(az monitor log-analytics workspace get-shared-keys \
    --resource-group "${LAW_RG}" --workspace-name "${LAW_NAME}" \
    --subscription "${SECURITY_SUB}" --query primarySharedKey -o tsv 2>/dev/null || true)
fi
if [[ -n "${LAW_CUSTOMER_ID}" && -n "${LAW_SHARED_KEY}" ]]; then
  echo "  ✓ Retrieved DIBSecCom LAW credentials (customer ID: ${LAW_CUSTOMER_ID})"
else
  if [[ "${REQUIRE_LAW_SHARED_KEY}" == "true" ]]; then
    echo "ERROR: Required DIBSecCom LAW shared key could not be retrieved from subscription ${SECURITY_SUB:-${SECURITY_SUB_NAME}}." >&2
    echo "Grant a role such as Monitoring Contributor on the DIBSecCom workspace, Sentinel resource group, or Security subscription, refresh Azure credentials, and rerun the deployment." >&2
    exit 1
  fi

  echo "  ! DIBSecCom LAW shared key not available; deploying without explicit Container Apps log wiring"
fi

echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Preflight: Validating environment and deployment access     │"
echo "└──────────────────────────────────────────────────────────────┘"

bash deployment/run-worker-private-dns-preflight.sh
assert_bot_deployment_authorization

UAMI_PRINCIPAL_ID="${UAMI_PRINCIPAL_ID:-$(az identity show --name "${BOT_MANAGED_IDENTITY_NAME}" --resource-group "${RG_BOT}" --query principalId -o tsv)}"
UAMI_CLIENT_ID="${UAMI_CLIENT_ID:-$(az identity show --name "${BOT_MANAGED_IDENTITY_NAME}" --resource-group "${RG_BOT}" --query clientId -o tsv)}"

if [[ "${PREFLIGHT_ONLY}" == "true" ]]; then
  echo "  ✓ Preflight checks completed successfully. No deployment actions were performed."
  exit 0
fi

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Bot Container App Deployment                               ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  ✓ Stable bot LLM deployment: ${WEATHER_LLM_MODEL}"
echo "  ✓ Backing model: ${WEATHER_LLM_MODEL_NAME} ${WEATHER_LLM_MODEL_VERSION}"
echo "  ✓ Deployment SKU: ${WEATHER_LLM_SKU_NAME} (${WEATHER_LLM_SKU_CAPACITY})"
echo "  ✓ Heartbeat interval: ${HEARTBEAT_INTERVAL_SECONDS} seconds"
echo "  ✓ Container App target: ${CONTAINER_APP_NAME}"
echo "  ✓ Container Apps environment target: ${CONTAINER_ENV_NAME}"
echo "  ✓ Private Container Apps networking: ${ENABLE_PRIVATE_CONTAINER_APPS_NETWORKING}"
echo ""

# ── Step 1: Build & push bot container to ACR ────────────────────
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Step 1/4: Building bot container image locally              │"
echo "└──────────────────────────────────────────────────────────────┘"

az acr login --name "${BOT_ACR_NAME}"

docker build --no-cache \
  --pull \
  -t "${BOT_ACR_NAME}.azurecr.io/zolab-bot:${BOT_IMAGE_TAG}" \
  -t "${BOT_ACR_NAME}.azurecr.io/zolab-bot:latest" \
  -f bot-app/Dockerfile \
  .

docker push "${BOT_ACR_NAME}.azurecr.io/zolab-bot:${BOT_IMAGE_TAG}"
docker push "${BOT_ACR_NAME}.azurecr.io/zolab-bot:latest"

echo "  ✓ Bot container images pushed to ${BOT_ACR_NAME}.azurecr.io/zolab-bot:${BOT_IMAGE_TAG} and :latest"
echo ""

# ── Step 2: Deploy Bicep (Container App + Bot Service) ────────────
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Step 2/4: Deploying Container App infrastructure (Bicep)    │"
echo "└──────────────────────────────────────────────────────────────┘"

echo "  ✓ Managed identity only cutover: no app registration or Key Vault secret will be used"

az deployment sub create \
  --location "${LOCATION}" \
  --template-file bot-app/deployment/bot-infra.bicep \
  --parameters \
    suffix="${SUFFIX}" \
    tenantId="${TENANT_ID}" \
    logAnalyticsCustomerId="${LAW_CUSTOMER_ID}" \
    logAnalyticsSharedKey="${LAW_SHARED_KEY}" \
    botImageTag="${BOT_IMAGE_TAG}" \
    weatherLlmModel="${WEATHER_LLM_MODEL}" \
    weatherLlmApiVersion="${WEATHER_LLM_API_VERSION}" \
    weatherLlmModelName="${WEATHER_LLM_MODEL_NAME}" \
    weatherLlmModelVersion="${WEATHER_LLM_MODEL_VERSION}" \
    weatherLlmModelFormat="${WEATHER_LLM_MODEL_FORMAT}" \
    weatherLlmSkuName="${WEATHER_LLM_SKU_NAME}" \
    weatherLlmSkuCapacity="${WEATHER_LLM_SKU_CAPACITY}" \
    heartbeatIntervalSeconds="${HEARTBEAT_INTERVAL_SECONDS}" \
    containerEnvName="${CONTAINER_ENV_NAME}" \
    containerAppName="${CONTAINER_APP_NAME}" \
    enablePrivateContainerAppsNetworking="${ENABLE_PRIVATE_CONTAINER_APPS_NETWORKING}" \
    containerAppsInfrastructureSubnetResourceId="${CONTAINER_APPS_INFRASTRUCTURE_SUBNET_RESOURCE_ID}" \
  --output none

echo "  ✓ Container App + Bot Service deployed"
echo ""

# ── Step 3: Grant bot UAMI Storage RBAC on worker storage ────────
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Step 3/4: Granting Storage RBAC to bot UAMI                 │"
echo "└──────────────────────────────────────────────────────────────┘"

# Storage Queue Data Contributor
az role assignment create \
  --assignee-object-id "${UAMI_PRINCIPAL_ID}" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Queue Data Contributor" \
  --scope "${STORAGE_SCOPE}" \
  --output none 2>/dev/null || echo "  (Queue role already assigned)"

# Storage Blob Data Contributor
az role assignment create \
  --assignee-object-id "${UAMI_PRINCIPAL_ID}" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope "${STORAGE_SCOPE}" \
  --output none 2>/dev/null || echo "  (Blob role already assigned)"

echo "  ✓ Storage Queue + Blob RBAC granted on ${WORKER_STORAGE_ACCOUNT}"
echo "  ✓ Bot LLM RBAC is handled inside bot infrastructure Bicep"
echo ""

# ── Step 4: Verify deployment ─────────────────────────────────────
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Step 4/4: Verifying deployment                              │"
echo "└──────────────────────────────────────────────────────────────┘"

CA_FQDN=$(az containerapp show \
  --name "${CONTAINER_APP_NAME}" \
  --resource-group "${RG_BOT}" \
  --query "properties.configuration.ingress.fqdn" \
  -o tsv)

BOT_CLIENT_ID=$(az bot show \
  --name "${BOT_SERVICE_NAME}" \
  --resource-group "${RG_BOT}" \
  --query "properties.msaAppId" \
  -o tsv)

BOT_LLM_ENDPOINT=$(az deployment sub show \
  --name "bot-resources-${SUFFIX}" \
  --query "properties.outputs.botLlmEndpoint.value" \
  -o tsv 2>/dev/null || true)

if [[ ! -f "${TEAMS_MANIFEST_TEMPLATE}" ]]; then
  echo "ERROR: Teams manifest template not found at ${TEAMS_MANIFEST_TEMPLATE}" >&2
  exit 1
fi

TEAMS_APP_ID="${TEAMS_APP_ID}" BOT_CLIENT_ID="${BOT_CLIENT_ID}" CA_FQDN="${CA_FQDN}" \
TEAMS_MANIFEST_TEMPLATE="${TEAMS_MANIFEST_TEMPLATE}" TEAMS_MANIFEST_PATH="${TEAMS_MANIFEST_PATH}" \
python3 - <<'PY'
from pathlib import Path
import os

template = Path(os.environ["TEAMS_MANIFEST_TEMPLATE"]).read_text(encoding="utf-8")
rendered = template.replace("__TEAMS_APP_ID__", os.environ["TEAMS_APP_ID"])
rendered = rendered.replace("__BOT_CLIENT_ID__", os.environ["BOT_CLIENT_ID"])
rendered = rendered.replace("__BOT_DOMAIN__", os.environ["CA_FQDN"])
Path(os.environ["TEAMS_MANIFEST_PATH"]).write_text(rendered, encoding="utf-8")
PY

echo "  ✓ Container App FQDN: ${CA_FQDN}"
echo "  ✓ Bot client ID: ${BOT_CLIENT_ID}"
if [[ -n "${BOT_LLM_ENDPOINT}" ]]; then
  echo "  ✓ Bot LLM endpoint: ${BOT_LLM_ENDPOINT}"
fi

CURRENT_WORKER_IMAGE="$(az container show \
  --name "${WORKER_ACI_NAME}" \
  --resource-group "${WORKER_RG}" \
  --query 'containers[0].image' \
  -o tsv 2>/dev/null || true)"

if [[ -n "${CURRENT_WORKER_IMAGE}" ]]; then
  CURRENT_WORKER_IMAGE_TAG="${CURRENT_WORKER_IMAGE##*:}"
  sync_worker_download_host "${CURRENT_WORKER_IMAGE_TAG}" "${CA_FQDN}"
else
  echo "  ! Worker container not found; skipped worker download-host sync"
fi

echo "  ✓ Teams manifest updated: ${TEAMS_MANIFEST_PATH}"

if [[ ! -f "${TEAMS_APP_DIR}/color.png" || ! -f "${TEAMS_APP_DIR}/outline.png" ]]; then
  echo "ERROR: Teams app icons not found in ${TEAMS_APP_DIR}" >&2
  exit 1
fi

rm -f "${TEAMS_ZIP_PATH}"
(
  cd "${TEAMS_APP_DIR}"
  zip -q -r "${TEAMS_ZIP_PATH}" manifest.json color.png outline.png
)

echo "  ✓ Teams package updated in place: ${TEAMS_ZIP_PATH}"
echo ""

# ── Done ─────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Deployment complete!                                       ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                             ║"
echo "║  Bot Image Tag: ${BOT_IMAGE_TAG}"
echo "║  Bot Client ID: ${BOT_CLIENT_ID}"
echo "║  Teams App ID: ${TEAMS_APP_ID}"
echo "║  Container App: https://${CA_FQDN}                          "
echo "║  Bot Endpoint:  https://${CA_FQDN}/api/messages              "
echo "║  Teams Zip:     ${TEAMS_ZIP_PATH}"
echo "║                                                             ║"
echo "║  Useful commands:                                           ║"
echo "║    az containerapp logs show -n ${CONTAINER_APP_NAME} -g ${RG_BOT}"
echo "║    az containerapp revision list -n ${CONTAINER_APP_NAME} -g ${RG_BOT} -o table"
echo "║                                                             ║"
echo "║  Next steps:                                                ║"
echo "║    1. Re-upload the updated Teams app package                ║"
echo "║    2. Reinstall it in Teams if the bot identity changed      ║"
echo "║    3. Test 'health' and 'list builds' in Teams              ║"
echo "║                                                             ║"
echo "╚══════════════════════════════════════════════════════════════╝"
