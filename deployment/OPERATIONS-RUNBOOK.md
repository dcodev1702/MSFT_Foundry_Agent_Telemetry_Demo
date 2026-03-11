# Operations Runbook

This runbook captures the shortest path for build, deploy, rollback, verification, and smoke checking across the bot and worker runtimes.

## Bot

Build and deploy the latest bot image locally:

```bash
cd /path/to/repo
bash bot-app/deployment/deploy-bot-app.sh
```

These bot and worker deploy paths rely on the same static `zolab-ai-dev` group used by the Foundry environment deployment. The group persists, but the Foundry build lifecycle adds the requesting user when a build starts and removes that user again when no managed builds remain.

Verify the live bot revision:

```bash
az containerapp revision list \
  --name zolab-bot-ca-botprd \
  --resource-group zolab-bot-botprd \
  --query "[].{name:name,active:properties.active,traffic:properties.trafficWeight,health:properties.healthState,image:properties.template.containers[0].image}" \
  -o table
```

Rollback the bot to a prior immutable image tag:

```bash
BOT_SECRET=$(bash deployment/get-bot-secret.sh)
LAW_CUSTOMER_ID=$(az monitor log-analytics workspace show --resource-group Sentinel --workspace-name DIBSecCom --subscription 192ad012-896e-4f14-8525-c37a2a9640f9 --query customerId -o tsv)
LAW_SHARED_KEY=$(az monitor log-analytics workspace get-shared-keys --resource-group Sentinel --workspace-name DIBSecCom --subscription 192ad012-896e-4f14-8525-c37a2a9640f9 --query primarySharedKey -o tsv)
BOT_APP_REGISTRATION_NAME=$(az ad app show --id ed77d99f-074b-4ef6-9fbc-55bfeb7b5aef --query displayName -o tsv)
OPERATOR_GROUP_OBJECT_ID=$(az ad group list --filter "displayName eq 'zolab-ai-dev'" --query '[0].id' -o tsv)

az deployment sub create \
  --location eastus2 \
  --template-file bot-app/deployment/bot-infra.bicep \
  --parameters \
    suffix=botprd \
    botAppId=ed77d99f-074b-4ef6-9fbc-55bfeb7b5aef \
    tenantId=b22dee98-83da-4207-b9ab-5ba931866f44 \
    botAppSecret="$BOT_SECRET" \
    logAnalyticsCustomerId="$LAW_CUSTOMER_ID" \
    logAnalyticsSharedKey="$LAW_SHARED_KEY" \
    logAnalyticsWorkspaceResourceId=/subscriptions/192ad012-896e-4f14-8525-c37a2a9640f9/resourceGroups/Sentinel/providers/Microsoft.OperationalInsights/workspaces/DIBSecCom \
    operatorGroupPrincipalId="$OPERATOR_GROUP_OBJECT_ID" \
    botAppRegistrationName="$BOT_APP_REGISTRATION_NAME" \
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
  --name zolab-worker-aci-botprd \
  --resource-group zolab-worker-botprd \
  --query '{image:containers[0].image,state:instanceView.state,provisioning:provisioningState,startTime:containers[0].instanceView.currentState.startTime}' \
  -o table

az container exec \
  --name zolab-worker-aci-botprd \
  --resource-group zolab-worker-botprd \
  --exec-command "cat /app/worker-build-info.json"
```

Rollback the worker to a prior immutable image tag:

```bash
BOT_SECRET=$(bash deployment/get-bot-secret.sh)

az deployment sub create \
  --location eastus2 \
  --template-file deployment/worker-infra.bicep \
  --parameters \
    suffix=botprd \
    botAppId=ed77d99f-074b-4ef6-9fbc-55bfeb7b5aef \
    tenantId=b22dee98-83da-4207-b9ab-5ba931866f44 \
    managedIdentityResourceId=/subscriptions/08fdc492-f5aa-4601-84ae-03a37449c2ba/resourcegroups/zolab-bot-botprd/providers/Microsoft.ManagedIdentity/userAssignedIdentities/zolab-bot-mi-botprd \
    managedIdentityPrincipalId=e9a17b6f-74e3-44f4-ae3e-14dd48d5c251 \
    managedIdentityClientId=59bffc04-c429-4580-9833-8ce88c088877 \
    workerCpu=2 \
    workerMemoryInGb=4 \
    botAppSecret="$BOT_SECRET" \
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

## Useful Tag Discovery

List recent bot tags:

```bash
az acr repository show-tags --name zolabbotacrbotprd --repository zolab-bot --orderby time_desc --top 10 -o tsv
```

List recent worker tags:

```bash
az acr repository show-tags --name zolabworkeracrbotprd --repository zolab-worker --orderby time_desc --top 10 -o tsv
```