#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
# deploy-bot-app.sh — Deploy Bot App Service + container image
#
# Runs steps 1–4 to bring the bot online after B1 quota approval:
#   1. Deploy Bicep (App Service Plan + App Service)
#   2. Set the client secret on App Service
#   3. Grant bot UAMI Storage RBAC on worker storage account
#   4. Build & push bot container to ACR
#
# Usage (from repo root):
#   bash bot-app/deployment/deploy-bot-app.sh
#
# Prerequisites:
#   - az login (authenticated)
#   - B1 App Service Plan quota approved for eastus2
#   - bot-app/deployment/.bot-secrets.json exists
# ════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────
SUFFIX="botprd"
BOT_APP_ID="ed77d99f-074b-4ef6-9fbc-55bfeb7b5aef"
TENANT_ID="b22dee98-83da-4207-b9ab-5ba931866f44"
LOCATION="eastus2"

RG_BOT="zolab-bot-${SUFFIX}"
APP_SERVICE_NAME="zolab-bot-app-${SUFFIX}"
BOT_ACR_NAME="zolabbotacr${SUFFIX}"

WORKER_STORAGE_ACCOUNT="zolabworkerst${SUFFIX}"
WORKER_RG="zolab-worker-${SUFFIX}"

UAMI_PRINCIPAL_ID="e9a17b6f-74e3-44f4-ae3e-14dd48d5c251"

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

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Bot App Service Deployment                                 ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── Step 1: Deploy Bicep (App Service Plan + App Service) ────────
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Step 1/4: Deploying App Service infrastructure (Bicep)      │"
echo "└──────────────────────────────────────────────────────────────┘"

az deployment sub create \
  --location "${LOCATION}" \
  --template-file bot-app/deployment/bot-infra.bicep \
  --parameters \
    suffix="${SUFFIX}" \
    botAppId="${BOT_APP_ID}" \
    tenantId="${TENANT_ID}" \
    deployAppService=true \
  --output none

echo "  ✓ App Service Plan + App Service deployed"
echo ""

# ── Step 2: Set client secret on App Service ─────────────────────
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Step 2/4: Setting client secret on App Service              │"
echo "└──────────────────────────────────────────────────────────────┘"

az webapp config appsettings set \
  --name "${APP_SERVICE_NAME}" \
  --resource-group "${RG_BOT}" \
  --settings "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET=${BOT_SECRET}" \
  --output none

echo "  ✓ Client secret configured"
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

# ── Step 4: Build & push bot container to ACR ────────────────────
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Step 4/4: Building bot container image in ACR               │"
echo "└──────────────────────────────────────────────────────────────┘"

az acr build \
  --registry "${BOT_ACR_NAME}" \
  --image zolab-bot:latest \
  --file bot-app/Dockerfile \
  .

echo "  ✓ Bot container image pushed to ${BOT_ACR_NAME}.azurecr.io/zolab-bot:latest"
echo ""

# ── Done ─────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Deployment complete!                                       ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                             ║"
echo "║  App Service: https://${APP_SERVICE_NAME}.azurewebsites.net ║"
echo "║  Bot Endpoint: .../api/messages                             ║"
echo "║                                                             ║"
echo "║  Next steps:                                                ║"
echo "║    1. az webapp restart -n ${APP_SERVICE_NAME} -g ${RG_BOT} ║"
echo "║    2. az webapp log tail -n ${APP_SERVICE_NAME} -g ${RG_BOT}║"
echo "║    3. Open Teams → Bot the Builder → send 'health'         ║"
echo "║                                                             ║"
echo "╚══════════════════════════════════════════════════════════════╝"
