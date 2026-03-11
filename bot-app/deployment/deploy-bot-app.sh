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
#   - bot-app/deployment/.bot-secrets.json exists
# ════════════════════════════════════════════════════════════════
set -euo pipefail

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

SECRETS_FILE="bot-app/deployment/.bot-secrets.json"

# ── Resolve repo root ───────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

# ── Validate prerequisites ──────────────────────────────────────
if ! az account show &>/dev/null; then
    echo "ERROR: Not logged in. Run 'az login' first." >&2
    exit 1
fi

if [[ ! -f "${SECRETS_FILE}" ]]; then
    echo "ERROR: ${SECRETS_FILE} not found." >&2
    exit 1
fi

BOT_SECRET=$(python3 -c "import json; print(json.load(open('${SECRETS_FILE}'))['password'])")
BOT_IMAGE_TAG="botfix-$(date -u +%Y%m%d%H%M%S)-$(git rev-parse --short HEAD)"
BOT_APP_REGISTRATION_NAME=$(az ad app show --id "${BOT_APP_ID}" --query displayName -o tsv)

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
echo "│ Step 1/4: Building bot container image in ACR               │"
echo "└──────────────────────────────────────────────────────────────┘"

az acr build \
  --registry "${BOT_ACR_NAME}" \
  --image "zolab-bot:${BOT_IMAGE_TAG}" \
  --image zolab-bot:latest \
  --file bot-app/Dockerfile \
  .

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

echo "  ✓ Container App FQDN: ${CA_FQDN}"
echo ""

# ── Done ─────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Deployment complete!                                       ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                             ║"
echo "║  Bot Image Tag: ${BOT_IMAGE_TAG}"
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
