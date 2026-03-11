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
#   - Key Vault secret bot-app-client-secret exists in zolabbotkv<suffix>
# ════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../deployment/bot-secret-common.sh"

# ── Configuration ────────────────────────────────────────────────
SUFFIX="botprd"
BOT_APP_ID="ed77d99f-074b-4ef6-9fbc-55bfeb7b5aef"
TENANT_ID="b22dee98-83da-4207-b9ab-5ba931866f44"
LOCATION="eastus2"

RG_BOT="zolab-bot-${SUFFIX}"
CONTAINER_APP_NAME="zolab-bot-ca-${SUFFIX}"
BOT_ACR_NAME="zolabbotacr${SUFFIX}"

WORKER_STORAGE_ACCOUNT="zolabworkerst${SUFFIX}"
WORKER_RG="zolab-worker-${SUFFIX}"

UAMI_PRINCIPAL_ID="e9a17b6f-74e3-44f4-ae3e-14dd48d5c251"

# DIBSecCom LAW in Security subscription — all logs go here
SECURITY_SUB="192ad012-896e-4f14-8525-c37a2a9640f9"
LAW_RG="Sentinel"
LAW_NAME="DIBSecCom"

BOT_SECRET_SUFFIX="${SUFFIX}"
BOT_SECRET_NAME="${BOT_SECRET_NAME:-bot-app-client-secret}"
BOT_KEY_VAULT_NAME="${BOT_SECRET_KEYVAULT_NAME:-zolabbotkv${SUFFIX}}"
BOT_OPERATOR_GROUP_DISPLAY_NAME="${BOT_OPERATOR_GROUP_DISPLAY_NAME:-zolab-ai-dev}"
LAW_WORKSPACE_RESOURCE_ID="/subscriptions/${SECURITY_SUB}/resourceGroups/${LAW_RG}/providers/Microsoft.OperationalInsights/workspaces/${LAW_NAME}"

# ── Resolve repo root ───────────────────────────────────────────
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

# ── Validate prerequisites ──────────────────────────────────────
if ! az account show &>/dev/null; then
    echo "ERROR: Not logged in. Run 'az login' first." >&2
    exit 1
fi

if ! docker version &>/dev/null; then
  echo "ERROR: Docker is not available. Start Docker Desktop and retry." >&2
  exit 1
fi

BOT_SECRET_OVERRIDE_PRESENT=0
if [[ -n "${BOT_SECRET:-}" ]]; then
  BOT_SECRET_OVERRIDE_PRESENT=1
fi

BOT_SECRET="$(resolve_bot_secret)"
BOT_SECRET_RESOLUTION="$(resolve_bot_secret_source)"
BOT_IMAGE_TAG="botfix-$(date -u +%Y%m%d%H%M%S)-$(git rev-parse --short HEAD)"
BOT_APP_REGISTRATION_NAME="Bot-The-Builder"
OPERATOR_GROUP_OBJECT_ID="$(az ad group list --filter "displayName eq '${BOT_OPERATOR_GROUP_DISPLAY_NAME}'" --query '[0].id' -o tsv 2>/dev/null || true)"

echo "  ✓ Resolved bot app secret from ${BOT_SECRET_RESOLUTION}"
if [[ -n "${OPERATOR_GROUP_OBJECT_ID}" ]]; then
  echo "  ✓ Shared operator group ${BOT_OPERATOR_GROUP_DISPLAY_NAME}: ${OPERATOR_GROUP_OBJECT_ID}"
else
  echo "ERROR: Shared operator group ${BOT_OPERATOR_GROUP_DISPLAY_NAME} was not found in Entra ID." >&2
  echo "Set BOT_OPERATOR_GROUP_DISPLAY_NAME to the correct group and retry so new deployments preserve shared Key Vault access." >&2
  exit 1
fi

CURRENT_BOT_APP_REGISTRATION_NAME=$(az ad app show --id "${BOT_APP_ID}" --query displayName -o tsv)
if [[ "${CURRENT_BOT_APP_REGISTRATION_NAME}" != "${BOT_APP_REGISTRATION_NAME}" ]]; then
  az ad app update --id "${BOT_APP_ID}" --display-name "${BOT_APP_REGISTRATION_NAME}" --output none
  echo "  ✓ Updated bot app registration display name to ${BOT_APP_REGISTRATION_NAME}"
fi

# Fetch DIBSecCom LAW credentials (cross-subscription)
LAW_CUSTOMER_ID=$(az monitor log-analytics workspace show \
  --resource-group "${LAW_RG}" --workspace-name "${LAW_NAME}" \
  --subscription "${SECURITY_SUB}" --query customerId -o tsv)
LAW_SHARED_KEY=$(az monitor log-analytics workspace get-shared-keys \
  --resource-group "${LAW_RG}" --workspace-name "${LAW_NAME}" \
  --subscription "${SECURITY_SUB}" --query primarySharedKey -o tsv)
echo "  ✓ Retrieved DIBSecCom LAW credentials (customer ID: ${LAW_CUSTOMER_ID})"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Bot Container App Deployment                               ║"
echo "╚══════════════════════════════════════════════════════════════╝"
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

az deployment sub create \
  --location "${LOCATION}" \
  --template-file bot-app/deployment/bot-infra.bicep \
  --parameters \
    suffix="${SUFFIX}" \
    botAppId="${BOT_APP_ID}" \
    tenantId="${TENANT_ID}" \
    botAppSecret="${BOT_SECRET}" \
    logAnalyticsCustomerId="${LAW_CUSTOMER_ID}" \
    logAnalyticsSharedKey="${LAW_SHARED_KEY}" \
    logAnalyticsWorkspaceResourceId="${LAW_WORKSPACE_RESOURCE_ID}" \
    operatorGroupPrincipalId="${OPERATOR_GROUP_OBJECT_ID}" \
    botAppRegistrationName="${BOT_APP_REGISTRATION_NAME}" \
    botImageTag="${BOT_IMAGE_TAG}" \
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

KEY_VAULT_SECRET_ID=$(az keyvault secret show \
  --vault-name "${BOT_KEY_VAULT_NAME}" \
  --name "${BOT_SECRET_NAME}" \
  --query id \
  -o tsv)

echo "  ✓ Container App FQDN: ${CA_FQDN}"
echo "  ✓ Bot Key Vault secret: ${KEY_VAULT_SECRET_ID}"
echo ""

# ── Done ─────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Deployment complete!                                       ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                             ║"
echo "║  Bot Image Tag: ${BOT_IMAGE_TAG}"
echo "║  Bot Key Vault: ${BOT_KEY_VAULT_NAME}"
echo "║  Container App: https://${CA_FQDN}                          "
echo "║  Bot Endpoint:  https://${CA_FQDN}/api/messages              "
echo "║                                                             ║"
echo "║  Useful commands:                                           ║"
echo "║    az containerapp logs show -n ${CONTAINER_APP_NAME} -g ${RG_BOT}"
echo "║    az containerapp revision list -n ${CONTAINER_APP_NAME} -g ${RG_BOT} -o table"
echo "║                                                             ║"
echo "║  Next steps:                                                ║"
echo "║    1. Open Teams → Bot the Builder → send 'health'         ║"
echo "║    2. Test 'list builds' and 'build it' commands            ║"
echo "║                                                             ║"
echo "╚══════════════════════════════════════════════════════════════╝"
