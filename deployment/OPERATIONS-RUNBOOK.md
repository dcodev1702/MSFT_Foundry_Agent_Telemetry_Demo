# Operations Runbook

This runbook captures the shortest path for build, deploy, rollback, verification, and smoke checking across the bot and worker runtimes.

For Foundry environment operations, prefer Teams-triggered or queue-driven builds over direct local `pwsh ./deploy-foundry-env.ps1` runs. Those operational flows use the Azure-hosted managed identity; local runs use your desktop identity and can fail after PIM changes until both Azure CLI and Az PowerShell tokens are refreshed. The legacy local Teams listener in `deployment/teams-command-dispatch.ps1` now starts in diagnostics-only mode by default and blocks local build and teardown unless explicitly started with `-AllowMutatingCommands` for non-production testing.

## Bot

Set the environment-specific values once before running the commands below:

```bash
SUFFIX="${SUFFIX:-botprd}"
BOT_NAME="${BOT_NAME:-zolab-bot-ca-${SUFFIX}-vnet}"
BOT_RG="${BOT_RG:-zolab-bot-${SUFFIX}}"
WORKER_NAME="${WORKER_NAME:-zolab-worker-aci-${SUFFIX}}"
WORKER_RG="${WORKER_RG:-zolab-worker-${SUFFIX}}"
SECURITY_SUB="${SECURITY_SUB:-<security-subscription-id>}"
BOT_MI_NAME="${BOT_MI_NAME:-zolab-bot-mi-${SUFFIX}}"
BOT_CLIENT_ID=$(az identity show --name "${BOT_MI_NAME}" --resource-group "${BOT_RG}" --query clientId -o tsv)
```

Build and deploy the latest bot image locally:

```bash
cd /path/to/repo
bash bot-app/deployment/deploy-bot-app.sh
```

These bot and worker deploy paths rely on the same static `zolab-ai-dev` group used by the Foundry environment deployment. The group persists, but the Foundry build lifecycle adds the requesting user when a build starts and removes that user again when no managed builds remain.

Verify the live bot revision:

```bash
az containerapp revision list \
  --name "${BOT_NAME}" \
  --resource-group "${BOT_RG}" \
  --query "[].{name:name,active:properties.active,traffic:properties.trafficWeight,health:properties.healthState,image:properties.template.containers[0].image}" \
  -o table
```

Rollback the bot to a prior immutable image tag:

```bash
LAW_CUSTOMER_ID=$(az monitor log-analytics workspace show --resource-group Sentinel --workspace-name DIBSecCom --subscription "${SECURITY_SUB}" --query customerId -o tsv)
LAW_SHARED_KEY=$(az monitor log-analytics workspace get-shared-keys --resource-group Sentinel --workspace-name DIBSecCom --subscription "${SECURITY_SUB}" --query primarySharedKey -o tsv)

az deployment sub create \
  --location eastus2 \
  --template-file bot-app/deployment/bot-infra.bicep \
  --parameters \
    suffix="${SUFFIX}" \
    logAnalyticsCustomerId="$LAW_CUSTOMER_ID" \
    logAnalyticsSharedKey="$LAW_SHARED_KEY" \
    botImageTag="<prior-bot-tag>"
```

## Worker

Build and deploy the latest worker image locally:

```bash
cd /path/to/repo
bash deployment/deploy-worker-app.sh
```

Verify the live worker image and build metadata:

```bash
az container show \
  --name "${WORKER_NAME}" \
  --resource-group "${WORKER_RG}" \
  --query '{image:containers[0].image,state:instanceView.state,provisioning:provisioningState,startTime:containers[0].instanceView.currentState.startTime}' \
  -o table

az container exec \
  --name "${WORKER_NAME}" \
  --resource-group "${WORKER_RG}" \
  --exec-command "cat /app/worker-build-info.json"
```

Rollback the worker to a prior immutable image tag:

```bash
az deployment sub create \
  --location eastus2 \
  --template-file deployment/worker-infra.bicep \
  --parameters \
    suffix="${SUFFIX}" \
    botClientId="${BOT_CLIENT_ID}" \
    workerCpu=2 \
    workerMemoryInGb=4 \
    workerImageTag="<prior-worker-tag>"
```

## Smoke Checks

Run the automated Azure-side checks plus the manual Teams checklist:

```bash
cd /path/to/repo
bash deployment/run-smoke-checks.sh
```

Manual Teams validation sequence:

1. Send `health`
2. Send `listener status`
3. Send `list builds`
4. Send `build status <resource-group>`
5. Send `teardown <resource-group>` only when you want to validate the preview/confirmation flow

If Teams still has the previous package cached before reinstalling the refreshed archive:

```powershell
pwsh -NoProfile -File deployment/remove-teams-app.ps1
```

## Useful Tag Discovery

List recent bot tags:

```bash
az acr repository show-tags --name "zolabbotacr${SUFFIX}" --repository zolab-bot --orderby time_desc --top 10 -o tsv
```

List recent worker tags:

```bash
az acr repository show-tags --name "zolabworkeracr${SUFFIX}" --repository zolab-worker --orderby time_desc --top 10 -o tsv
```