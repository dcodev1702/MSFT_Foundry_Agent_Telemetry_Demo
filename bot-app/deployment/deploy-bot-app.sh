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
TENANT_ID="b22dee98-83da-4207-b9ab-5ba931866f44"
LOCATION="eastus2"

RG_BOT="zolab-bot-${SUFFIX}"
CONTAINER_APP_NAME="${CONTAINER_APP_NAME:-zolab-bot-ca-${SUFFIX}-vnet}"
CONTAINER_ENV_NAME="${CONTAINER_ENV_NAME:-zolab-bot-env-${SUFFIX}-vnet}"
BOT_SERVICE_NAME="zolab-bot-${SUFFIX}"
BOT_ACR_NAME="zolabbotacr${SUFFIX}"

WORKER_STORAGE_ACCOUNT="zolabworkerst${SUFFIX}"
WORKER_RG="zolab-worker-${SUFFIX}"
WORKER_ACI_NAME="zolab-worker-aci-${SUFFIX}"
ENABLE_PRIVATE_CONTAINER_APPS_NETWORKING="${ENABLE_PRIVATE_CONTAINER_APPS_NETWORKING:-true}"
CONTAINER_APPS_INFRASTRUCTURE_SUBNET_RESOURCE_ID="${CONTAINER_APPS_INFRASTRUCTURE_SUBNET_RESOURCE_ID:-}"

UAMI_PRINCIPAL_ID="e9a17b6f-74e3-44f4-ae3e-14dd48d5c251"

# DIBSecCom LAW in Security subscription — all logs go here
SECURITY_SUB="192ad012-896e-4f14-8525-c37a2a9640f9"
LAW_RG="Sentinel"
LAW_NAME="DIBSecCom"

LAW_WORKSPACE_RESOURCE_ID="/subscriptions/${SECURITY_SUB}/resourceGroups/${LAW_RG}/providers/Microsoft.OperationalInsights/workspaces/${LAW_NAME}"

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

# ── Validate prerequisites ──────────────────────────────────────
if ! az account show &>/dev/null; then
    echo "ERROR: Not logged in. Run 'az login' first." >&2
    exit 1
fi

if [[ -z "${CONTAINER_APPS_INFRASTRUCTURE_SUBNET_RESOURCE_ID}" ]]; then
  SUBSCRIPTION_ID=$(az account show --query id -o tsv)
  CONTAINER_APPS_INFRASTRUCTURE_SUBNET_RESOURCE_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${WORKER_RG}/providers/Microsoft.Network/virtualNetworks/zolab-worker-vnet-${SUFFIX}/subnets/snet-containerapps"
fi

if ! docker version &>/dev/null; then
  echo "ERROR: Docker is not available. Start Docker Desktop and retry." >&2
  exit 1
fi

BOT_IMAGE_TAG="botfix-$(date -u +%Y%m%d%H%M%S)-$(git rev-parse --short HEAD)"

# Fetch DIBSecCom LAW credentials (cross-subscription)
LAW_CUSTOMER_ID=$(az monitor log-analytics workspace show \
  --resource-group "${LAW_RG}" --workspace-name "${LAW_NAME}" \
  --subscription "${SECURITY_SUB}" --query customerId -o tsv 2>/dev/null || true)
LAW_SHARED_KEY=$(az monitor log-analytics workspace get-shared-keys \
  --resource-group "${LAW_RG}" --workspace-name "${LAW_NAME}" \
  --subscription "${SECURITY_SUB}" --query primarySharedKey -o tsv 2>/dev/null || true)
if [[ -n "${LAW_CUSTOMER_ID}" && -n "${LAW_SHARED_KEY}" ]]; then
  echo "  ✓ Retrieved DIBSecCom LAW credentials (customer ID: ${LAW_CUSTOMER_ID})"
else
  echo "  ! DIBSecCom LAW shared key not available; deploying without explicit Container Apps log wiring"
fi

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Bot Container App Deployment                               ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  ✓ Stable bot LLM deployment: ${WEATHER_LLM_MODEL}"
echo "  ✓ Backing model: ${WEATHER_LLM_MODEL_NAME} ${WEATHER_LLM_MODEL_VERSION}"
echo "  ✓ Deployment SKU: ${WEATHER_LLM_SKU_NAME} (${WEATHER_LLM_SKU_CAPACITY})"
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

STORAGE_SCOPE="/subscriptions/$(az account show --query id -o tsv)/resourceGroups/${WORKER_RG}/providers/Microsoft.Storage/storageAccounts/${WORKER_STORAGE_ACCOUNT}"

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
  az deployment sub create \
    --location "${LOCATION}" \
    --template-file deployment/worker-infra.bicep \
    --parameters \
      suffix="${SUFFIX}" \
      botClientId="59bffc04-c429-4580-9833-8ce88c088877" \
      tenantId="${TENANT_ID}" \
      managedIdentityResourceId="/subscriptions/08fdc492-f5aa-4601-84ae-03a37449c2ba/resourcegroups/zolab-bot-botprd/providers/Microsoft.ManagedIdentity/userAssignedIdentities/zolab-bot-mi-botprd" \
      managedIdentityPrincipalId="${UAMI_PRINCIPAL_ID}" \
      managedIdentityClientId="59bffc04-c429-4580-9833-8ce88c088877" \
      workerCpu=2 \
      workerMemoryInGb=4 \
      workerImageTag="${CURRENT_WORKER_IMAGE_TAG}" \
      botFqdn="${CA_FQDN}" \
      enablePrivateStorageAccess=true \
    --output none
  echo "  ✓ Worker download host synced to ${CA_FQDN}"
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
