#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
# deploy-worker-app.sh — Build and deploy the worker container
#
# Runs steps 1–3 to refresh the worker runtime:
#   1. Build & push worker container image locally with Docker
#   2. Deploy worker infrastructure Bicep pinned to the new image tag
#   3. Verify the live ACI image and state
#
# Usage (from repo root):
#   bash deployment/deploy-worker-app.sh
# ════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/bot-secret-common.sh"

SUFFIX="botprd"
LOCATION="eastus2"
BOT_APP_ID="ed77d99f-074b-4ef6-9fbc-55bfeb7b5aef"
TENANT_ID="b22dee98-83da-4207-b9ab-5ba931866f44"

WORKER_ACR_NAME="zolabworkeracr${SUFFIX}"
WORKER_ACI_NAME="zolab-worker-aci-${SUFFIX}"
WORKER_RG="zolab-worker-${SUFFIX}"

MANAGED_IDENTITY_RESOURCE_ID="/subscriptions/08fdc492-f5aa-4601-84ae-03a37449c2ba/resourcegroups/zolab-bot-botprd/providers/Microsoft.ManagedIdentity/userAssignedIdentities/zolab-bot-mi-botprd"
MANAGED_IDENTITY_PRINCIPAL_ID="e9a17b6f-74e3-44f4-ae3e-14dd48d5c251"
MANAGED_IDENTITY_CLIENT_ID="59bffc04-c429-4580-9833-8ce88c088877"

BOT_SECRET_SUFFIX="${SUFFIX}"
BOT_SECRET_NAME="${BOT_SECRET_NAME:-bot-app-client-secret}"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

if ! az account show &>/dev/null; then
    echo "ERROR: Not logged in. Run 'az login' first." >&2
    exit 1
fi

if ! docker version &>/dev/null; then
    echo "ERROR: Docker is not available. Start Docker Desktop and retry." >&2
    exit 1
fi

BOT_SECRET="$(resolve_bot_secret)"
WORKER_IMAGE_TAG="workerfix-$(date -u +%Y%m%d%H%M%S)-$(git rev-parse --short HEAD)"
WORKER_BUILD_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
WORKER_BUILD_COMMIT=$(git rev-parse HEAD)

echo "  ✓ Resolved bot app secret from ${BOT_SECRET_RESOLUTION}"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Worker Container Deployment                                ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Step 1/3: Building worker container image locally           │"
echo "└──────────────────────────────────────────────────────────────┘"

az acr login --name "${WORKER_ACR_NAME}"

docker build --no-cache \
  --pull \
  --build-arg WORKER_BUILD_UTC="${WORKER_BUILD_UTC}" \
  --build-arg WORKER_BUILD_COMMIT="${WORKER_BUILD_COMMIT}" \
  --build-arg WORKER_BUILD_SOURCE="local-docker" \
  -t "${WORKER_ACR_NAME}.azurecr.io/zolab-worker:${WORKER_IMAGE_TAG}" \
  -t "${WORKER_ACR_NAME}.azurecr.io/zolab-worker:latest" \
  -f deployment/Dockerfile.worker \
  .

docker push "${WORKER_ACR_NAME}.azurecr.io/zolab-worker:${WORKER_IMAGE_TAG}"
docker push "${WORKER_ACR_NAME}.azurecr.io/zolab-worker:latest"

echo "  ✓ Worker container images pushed to ${WORKER_ACR_NAME}.azurecr.io/zolab-worker:${WORKER_IMAGE_TAG} and :latest"
echo ""

echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Step 2/3: Deploying worker infrastructure (Bicep)          │"
echo "└──────────────────────────────────────────────────────────────┘"

az deployment sub create \
  --location "${LOCATION}" \
  --template-file deployment/worker-infra.bicep \
  --parameters \
    suffix="${SUFFIX}" \
    botAppId="${BOT_APP_ID}" \
    tenantId="${TENANT_ID}" \
    managedIdentityResourceId="${MANAGED_IDENTITY_RESOURCE_ID}" \
    managedIdentityPrincipalId="${MANAGED_IDENTITY_PRINCIPAL_ID}" \
    managedIdentityClientId="${MANAGED_IDENTITY_CLIENT_ID}" \
    workerCpu=2 \
    workerMemoryInGb=4 \
    botAppSecret="${BOT_SECRET}" \
    workerImageTag="${WORKER_IMAGE_TAG}" \
  --output none

echo "  ✓ Worker infrastructure deployed"
echo ""

echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Step 3/3: Verifying deployment                              │"
echo "└──────────────────────────────────────────────────────────────┘"

az container show \
  --name "${WORKER_ACI_NAME}" \
  --resource-group "${WORKER_RG}" \
  --query '{image:containers[0].image,state:instanceView.state,provisioning:provisioningState}' \
  -o table

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Deployment complete!                                       ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                             ║"
echo "║  Worker Image Tag: ${WORKER_IMAGE_TAG}"
echo "║  Worker ACI:      ${WORKER_ACI_NAME}"
echo "║  Worker RG:       ${WORKER_RG}"
echo "║                                                             ║"
echo "╚══════════════════════════════════════════════════════════════╝"