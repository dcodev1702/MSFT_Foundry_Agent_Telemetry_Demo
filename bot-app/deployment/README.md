# 🚀 Bot the Builder — Bot Infrastructure Deployment

Infrastructure-as-Code and deployment automation for the Teams bot surface of this repo. This folder owns the Azure resources that host the bot web server in Azure Container Apps and connect it to Teams through Azure Bot Service.

This deployment is separate from the root Foundry environment deployment in [../../deployment/README.md](../../deployment/README.md), but it depends on the worker/storage side of that broader automation design.

---

## 📋 Prerequisites

| Requirement | Details |
|---|---|
| Azure CLI | Installed and authenticated with access to the `zolab` subscription |
| Bot secrets file | `bot-app/deployment/.bot-secrets.json` must exist and include the bot app password |
| Security subscription access | Needed to read the DIBSecCom Log Analytics workspace keys in `Sentinel` |
| ACR build permissions | Required to build and push `zolab-bot:latest` into the bot ACR |
| Subscription deployment permissions | Required for `az deployment sub create` against bot infrastructure |
| Existing worker resources | The bot deployment expects the worker storage account and worker resource group to exist for RBAC wiring |

---

## 🏗️ What Gets Deployed

The subscription-scoped Bicep entry point in [bot-infra.bicep](bot-infra.bicep) creates a dedicated bot resource group and then deploys the resource-group-scoped module in [modules/bot-resources.bicep](modules/bot-resources.bicep).

### Resources

| Resource | Type | Purpose |
|---|---|---|
| `zolab-bot-<suffix>` | Resource Group | Dedicated resource group for the bot surface |
| `zolabbotacr<suffix>` | Azure Container Registry | Stores the bot image |
| `zolab-bot-mi-<suffix>` | User-Assigned Managed Identity | Used for ACR pulls and bot-side Azure access |
| `zolab-bot-env-<suffix>` | Container Apps Environment | Hosts the Container App and ships logs to DIBSecCom LAW |
| `zolab-bot-ca-<suffix>` | Azure Container App | Hosts the aiohttp/M365 Agents SDK bot runtime |
| `zolab-bot-<suffix>` | Azure Bot Service | Teams-facing bot registration |
| `MsTeamsChannel` | Bot Service channel | Enables Teams integration |

### Logging and Identity

| Capability | Implementation |
|---|---|
| Log destination | Cross-subscription DIBSecCom Log Analytics workspace |
| Container image pull | UAMI with `AcrPull` on the bot ACR |
| Bot Azure access | UAMI attached to the Container App |
| Worker storage access | Additional RBAC assigned by `deploy-bot-app.sh` |

---

## 📂 Files

```text
bot-app/deployment/
├── README.md
├── bot-infra.bicep
├── bot-infra.bicepparam
├── deploy-bot-app.sh
└── modules/
    └── bot-resources.bicep
```

| File | Purpose |
|---|---|
| [deploy-bot-app.sh](deploy-bot-app.sh) | End-to-end bot image build, infra deploy, RBAC wiring, and verification |
| [bot-infra.bicep](bot-infra.bicep) | Subscription-scoped entry point for bot resources |
| [bot-infra.bicepparam](bot-infra.bicepparam) | Parameter defaults for the bot deployment |
| [modules/bot-resources.bicep](modules/bot-resources.bicep) | Container App, ACR, UAMI, and Azure Bot Service resources |

---

## ▶️ Deploy

From the repo root:

```bash
bash bot-app/deployment/deploy-bot-app.sh
```

The script performs four steps:

1. Builds and pushes the bot image to `zolabbotacrbotprd.azurecr.io/zolab-bot:latest`
2. Deploys the bot infrastructure Bicep template
3. Grants Storage Queue and Blob RBAC to the bot UAMI on the worker storage account
4. Verifies the deployed Container App FQDN and prints follow-up commands

---

## 🔄 Update Flow

If only bot runtime code changed and you want to republish the bot surface, rerun:

```bash
bash bot-app/deployment/deploy-bot-app.sh
```

That rebuilds the bot image and refreshes the Container App deployment. The worker image is separate and is built from [../../deployment/Dockerfile.worker](../../deployment/Dockerfile.worker).

---

## 🔐 Required Inputs

The deploy script resolves these values at runtime:

- Bot app ID
- Tenant ID
- Bot app secret from `.bot-secrets.json`
- DIBSecCom Log Analytics customer ID and shared key from the Security subscription
- Worker storage scope for RBAC assignment

The script currently assumes the production-style suffix `botprd` and the matching resource names used across the repo.

---

## ✅ Verification

After deployment, confirm:

- The Container App exists and serves `https://<fqdn>/api/messages`
- The Azure Bot Service endpoint points to the Container App `/api/messages` path
- The bot UAMI has queue/blob access to `zolabworkerstbotprd`
- The Container Apps Environment is forwarding logs to DIBSecCom LAW

Useful commands:

```bash
az containerapp show -n zolab-bot-ca-botprd -g zolab-bot-botprd -o json
az containerapp logs show -n zolab-bot-ca-botprd -g zolab-bot-botprd
az containerapp revision list -n zolab-bot-ca-botprd -g zolab-bot-botprd -o table
```

### View Logs In Log Analytics

The bot Container App logs are sent to the `DIBSecCom` Log Analytics workspace in the Security subscription, resource group `Sentinel`.

Portal path:

1. Open Azure Portal.
2. Switch to the Security subscription.
3. Open resource group `Sentinel`.
4. Open Log Analytics workspace `DIBSecCom`.
5. Open `Logs`.

Useful starting tables for the bot Container App are:

- `ContainerAppConsoleLogs_CL`
- `ContainerAppSystemLogs_CL`

Example filters:

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "zolab-bot-ca-botprd"
| order by TimeGenerated desc
```

```kusto
ContainerAppSystemLogs_CL
| where ContainerAppName_s == "zolab-bot-ca-botprd"
| order by TimeGenerated desc
```

If you are looking for the bot rollout or crash history, start with the system logs table and then filter down to revision `zolab-bot-ca-botprd--0000003` or any older revision you want to inspect.

---

## 🔗 Related Docs

| Document | Use it for |
|---|---|
| [../README.md](../README.md) | Bot workspace overview |
| [../python-teams-bot-sample/README.md](../python-teams-bot-sample/README.md) | Runtime code and local dev |
| [../../deployment/README.md](../../deployment/README.md) | Foundry environment deployment and cleanup |

---

## 📝 Notes

- The bot deploy script currently handles the bot image and bot-side infrastructure only.
- The worker container and worker ACR are defined under the repo-root `deployment/` folder because they execute the shared PowerShell/Bicep automation.
- The bot Container App uses `activeRevisionsMode: Single`, so redeployments naturally roll forward to a single active revision.