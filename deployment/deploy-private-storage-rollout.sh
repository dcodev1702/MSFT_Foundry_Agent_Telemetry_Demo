#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

SUFFIX="${SUFFIX:-botprd}"
LOCATION="${LOCATION:-eastus2}"

BOT_RG="zolab-bot-${SUFFIX}"
WORKER_RG="zolab-worker-${SUFFIX}"
BOT_SERVICE_NAME="zolab-bot-${SUFFIX}"
LEGACY_CONTAINER_APP_NAME="${LEGACY_CONTAINER_APP_NAME:-zolab-bot-ca-${SUFFIX}}"
NEW_CONTAINER_APP_NAME="${NEW_CONTAINER_APP_NAME:-zolab-bot-ca-${SUFFIX}-vnet}"
NEW_CONTAINER_ENV_NAME="${NEW_CONTAINER_ENV_NAME:-zolab-bot-env-${SUFFIX}-vnet}"
WORKER_CONTAINER_NAME="zolab-worker-aci-${SUFFIX}"
WORKER_VNET_NAME="${WORKER_VNET_NAME:-zolab-worker-vnet-${SUFFIX}}"
WORKER_STORAGE_ACCOUNT="zolabworkerst${SUFFIX}"
BOT_MANAGED_IDENTITY_NAME="${BOT_MANAGED_IDENTITY_NAME:-zolab-bot-mi-${SUFFIX}}"

SECURITY_SUB="${SECURITY_SUB:-}"
LAW_RG="${LAW_RG:-Sentinel}"
LAW_NAME="${LAW_NAME:-DIBSecCom}"
TEAMS_APP_ID="${TEAMS_APP_ID:-ed77d99f-074b-4ef6-9fbc-55bfeb7b5aef}"

WEATHER_LLM_MODEL="${WEATHER_LLM_MODEL:-gpt-5.3-chat}"
WEATHER_LLM_API_VERSION="${WEATHER_LLM_API_VERSION:-2024-10-21}"
WEATHER_LLM_MODEL_NAME="${WEATHER_LLM_MODEL_NAME:-gpt-5.3-chat}"
WEATHER_LLM_MODEL_VERSION="${WEATHER_LLM_MODEL_VERSION:-2026-03-03}"
WEATHER_LLM_MODEL_FORMAT="${WEATHER_LLM_MODEL_FORMAT:-OpenAI}"
WEATHER_LLM_SKU_NAME="${WEATHER_LLM_SKU_NAME:-GlobalStandard}"
WEATHER_LLM_SKU_CAPACITY="${WEATHER_LLM_SKU_CAPACITY:-50}"

SUBSCRIPTION_ID=""
CONTAINER_APPS_SUBNET_ID=""
BOT_IMAGE_TAG=""
WORKER_IMAGE_TAG=""
LEGACY_FQDN=""
ROLLOUT_FAILED="true"
WORKER_DEPLOYMENT_NAME="private-storage-worker-$(date -u +%Y%m%d%H%M%S)"
BOT_DEPLOYMENT_NAME="private-storage-bot-$(date -u +%Y%m%d%H%M%S)"

TEAMS_APP_DIR="${REPO_ROOT}/bot-app/teams-app"
TEAMS_MANIFEST_TEMPLATE="${TEAMS_APP_DIR}/manifest.template.json"
TEAMS_MANIFEST_PATH="${TEAMS_APP_DIR}/manifest.json"
TEAMS_ZIP_PATH="${TEAMS_APP_DIR}/Bot-The-Builder.zip"

log() {
	printf '\n[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"
}

resolve_law_subscription_id() {
	local subscription_id

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

rollback() {
	local status=$?

	if [[ "${ROLLOUT_FAILED}" != "true" ]]; then
		return ${status}
	fi

	log "Rollback: re-enabling public network access on ${WORKER_STORAGE_ACCOUNT}"
	az storage account update \
		--name "${WORKER_STORAGE_ACCOUNT}" \
		--resource-group "${WORKER_RG}" \
		--public-network-access Enabled \
		--output none || true

	if [[ -n "${BOT_IMAGE_TAG}" && -n "${LEGACY_FQDN}" ]]; then
		log "Rollback: pointing Bot Service back to ${LEGACY_CONTAINER_APP_NAME}"
		az deployment sub create \
			--name "rollback-bot-${SUFFIX}-$(date -u +%Y%m%d%H%M%S)" \
			--location "${LOCATION}" \
			--template-file bot-app/deployment/bot-infra.bicep \
			--parameters \
				suffix="${SUFFIX}" \
				tenantId="${TENANT_ID}" \
				logAnalyticsCustomerId="${LAW_CUSTOMER_ID}" \
				logAnalyticsSharedKey="${LAW_SHARED_KEY}" \
				botImageTag="${BOT_IMAGE_TAG}" \
				containerEnvName="zolab-bot-env-${SUFFIX}" \
				containerAppName="${LEGACY_CONTAINER_APP_NAME}" \
				enablePrivateContainerAppsNetworking=false \
				containerAppsInfrastructureSubnetResourceId='' \
				weatherLlmModel="${WEATHER_LLM_MODEL}" \
				weatherLlmApiVersion="${WEATHER_LLM_API_VERSION}" \
				weatherLlmModelName="${WEATHER_LLM_MODEL_NAME}" \
				weatherLlmModelVersion="${WEATHER_LLM_MODEL_VERSION}" \
				weatherLlmModelFormat="${WEATHER_LLM_MODEL_FORMAT}" \
				weatherLlmSkuName="${WEATHER_LLM_SKU_NAME}" \
				weatherLlmSkuCapacity="${WEATHER_LLM_SKU_CAPACITY}" \
			--output none || true
	fi

	exit ${status}
}

trap rollback EXIT

if ! az account show &>/dev/null; then
	echo "ERROR: Not logged in. Run 'az login' first." >&2
	exit 1
fi

SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
TENANT_ID="${TENANT_ID:-$(az account show --query tenantId -o tsv)}"
MANAGED_IDENTITY_CLIENT_ID="${MANAGED_IDENTITY_CLIENT_ID:-$(az identity show --name "${BOT_MANAGED_IDENTITY_NAME}" --resource-group "${BOT_RG}" --query clientId -o tsv)}"
if [[ -z "${SECURITY_SUB}" ]]; then
	SECURITY_SUB="$(resolve_law_subscription_id || true)"
fi
CONTAINER_APPS_SUBNET_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${WORKER_RG}/providers/Microsoft.Network/virtualNetworks/${WORKER_VNET_NAME}/subnets/snet-containerapps"

BOT_IMAGE="$(az containerapp show --name "${LEGACY_CONTAINER_APP_NAME}" --resource-group "${BOT_RG}" --query 'properties.template.containers[0].image' -o tsv)"
WORKER_IMAGE="$(az container show --name "${WORKER_CONTAINER_NAME}" --resource-group "${WORKER_RG}" --query 'containers[0].image' -o tsv)"
BOT_IMAGE_TAG="${BOT_IMAGE##*:}"
WORKER_IMAGE_TAG="${WORKER_IMAGE##*:}"
LEGACY_FQDN="$(az containerapp show --name "${LEGACY_CONTAINER_APP_NAME}" --resource-group "${BOT_RG}" --query 'properties.configuration.ingress.fqdn' -o tsv)"

LAW_CUSTOMER_ID=""
LAW_SHARED_KEY=""
if [[ -n "${SECURITY_SUB}" ]]; then
	LAW_CUSTOMER_ID="$(az monitor log-analytics workspace show \
		--resource-group "${LAW_RG}" \
		--workspace-name "${LAW_NAME}" \
		--subscription "${SECURITY_SUB}" \
		--query customerId -o tsv 2>/dev/null || true)"
	LAW_SHARED_KEY="$(az monitor log-analytics workspace get-shared-keys \
		--resource-group "${LAW_RG}" \
		--workspace-name "${LAW_NAME}" \
		--subscription "${SECURITY_SUB}" \
		--query primarySharedKey -o tsv 2>/dev/null || true)"
fi

if [[ -z "${LAW_CUSTOMER_ID}" || -z "${LAW_SHARED_KEY}" ]]; then
	log "Proceeding without explicit Log Analytics credentials for the new Container Apps environment"
fi

log "Using current bot image tag ${BOT_IMAGE_TAG} and worker image tag ${WORKER_IMAGE_TAG}"
CURRENT_WORKER_SUBNET_ID="$(az container show --name "${WORKER_CONTAINER_NAME}" --resource-group "${WORKER_RG}" --query 'subnetIds[0].id' -o tsv 2>/dev/null || true)"

if [[ -z "${CURRENT_WORKER_SUBNET_ID}" ]]; then
	log "Recreating ${WORKER_CONTAINER_NAME} so it can be attached to the private worker subnet"
	az container delete \
		--name "${WORKER_CONTAINER_NAME}" \
		--resource-group "${WORKER_RG}" \
		--yes \
		--output none

	while az container show --name "${WORKER_CONTAINER_NAME}" --resource-group "${WORKER_RG}" --output none 2>/dev/null; do
		sleep 5
	done
fi

log "Deploying worker infrastructure with private storage access"

az deployment sub create \
	--name "${WORKER_DEPLOYMENT_NAME}" \
	--location "${LOCATION}" \
	--template-file deployment/worker-infra.bicep \
	--parameters \
		suffix="${SUFFIX}" \
		botClientId="${MANAGED_IDENTITY_CLIENT_ID}" \
		workerCpu=2 \
		workerMemoryInGb=4 \
		workerImageTag="${WORKER_IMAGE_TAG}" \
		enablePrivateStorageAccess=true \
	--output none

log "Waiting for the worker container group to report Running"
for _ in {1..30}; do
	worker_state="$(az container show --name "${WORKER_CONTAINER_NAME}" --resource-group "${WORKER_RG}" --query 'instanceView.state' -o tsv 2>/dev/null || true)"
	worker_provisioning="$(az container show --name "${WORKER_CONTAINER_NAME}" --resource-group "${WORKER_RG}" --query 'provisioningState' -o tsv 2>/dev/null || true)"
	if [[ "${worker_state}" == "Running" && "${worker_provisioning}" == "Succeeded" ]]; then
		break
	fi
	sleep 10
done

worker_logs="$(az container logs --name "${WORKER_CONTAINER_NAME}" --resource-group "${WORKER_RG}" 2>&1 || true)"
if printf '%s' "${worker_logs}" | grep -q 'AuthorizationFailure\|Worker poll error'; then
	ROLLOUT_FAILED="true"
	echo "ERROR: Worker validation failed after enabling private storage access." >&2
	exit 1
fi

log "Deploying VNet-backed bot Container Apps environment and app"

az deployment sub create \
	--name "${BOT_DEPLOYMENT_NAME}" \
	--location "${LOCATION}" \
	--template-file bot-app/deployment/bot-infra.bicep \
	--parameters \
		suffix="${SUFFIX}" \
		tenantId="${TENANT_ID}" \
		logAnalyticsCustomerId="${LAW_CUSTOMER_ID}" \
		logAnalyticsSharedKey="${LAW_SHARED_KEY}" \
		botImageTag="${BOT_IMAGE_TAG}" \
		containerEnvName="${NEW_CONTAINER_ENV_NAME}" \
		containerAppName="${NEW_CONTAINER_APP_NAME}" \
		enablePrivateContainerAppsNetworking=true \
		containerAppsInfrastructureSubnetResourceId="${CONTAINER_APPS_SUBNET_ID}" \
		weatherLlmModel="${WEATHER_LLM_MODEL}" \
		weatherLlmApiVersion="${WEATHER_LLM_API_VERSION}" \
		weatherLlmModelName="${WEATHER_LLM_MODEL_NAME}" \
		weatherLlmModelVersion="${WEATHER_LLM_MODEL_VERSION}" \
		weatherLlmModelFormat="${WEATHER_LLM_MODEL_FORMAT}" \
		weatherLlmSkuName="${WEATHER_LLM_SKU_NAME}" \
		weatherLlmSkuCapacity="${WEATHER_LLM_SKU_CAPACITY}" \
	--output none

NEW_FQDN="$(az containerapp show --name "${NEW_CONTAINER_APP_NAME}" --resource-group "${BOT_RG}" --query 'properties.configuration.ingress.fqdn' -o tsv)"

log "Validating the new public bot endpoint https://${NEW_FQDN}/api/messages"
curl --fail --silent --show-error --retry 12 --retry-delay 10 "https://${NEW_FQDN}/api/messages" >/tmp/bot-health.json

BOT_CLIENT_ID="$(az bot show --name "${BOT_SERVICE_NAME}" --resource-group "${BOT_RG}" --query 'properties.msaAppId' -o tsv)"

if [[ ! -f "${TEAMS_MANIFEST_TEMPLATE}" ]]; then
	ROLLOUT_FAILED="true"
	echo "ERROR: Teams manifest template not found at ${TEAMS_MANIFEST_TEMPLATE}" >&2
	exit 1
fi

TEAMS_APP_ID="${TEAMS_APP_ID}" \
BOT_CLIENT_ID="${BOT_CLIENT_ID}" \
CA_FQDN="${NEW_FQDN}" \
TEAMS_MANIFEST_TEMPLATE="${TEAMS_MANIFEST_TEMPLATE}" \
TEAMS_MANIFEST_PATH="${TEAMS_MANIFEST_PATH}" \
python3 - <<'PY'
from pathlib import Path
import os

template = Path(os.environ['TEAMS_MANIFEST_TEMPLATE']).read_text(encoding='utf-8')
rendered = template.replace('__TEAMS_APP_ID__', os.environ['TEAMS_APP_ID'])
rendered = rendered.replace('__BOT_CLIENT_ID__', os.environ['BOT_CLIENT_ID'])
rendered = rendered.replace('__BOT_DOMAIN__', os.environ['CA_FQDN'])
Path(os.environ['TEAMS_MANIFEST_PATH']).write_text(rendered, encoding='utf-8')
PY

rm -f "${TEAMS_ZIP_PATH}"
(
	cd "${TEAMS_APP_DIR}"
	zip -q -r "${TEAMS_ZIP_PATH}" manifest.json color.png outline.png
)

log "Rollout complete"
echo "New bot FQDN: ${NEW_FQDN}"
echo "Legacy bot FQDN retained for rollback: ${LEGACY_FQDN}"
echo "Updated Teams package: ${TEAMS_ZIP_PATH}"
echo "Run deployment/run-smoke-checks.sh for the manual Teams turn validation."
ROLLOUT_FAILED="false"
