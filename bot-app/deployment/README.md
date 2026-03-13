# 🚀 Bot the Builder — Bot Infrastructure Deployment

Infrastructure-as-Code and deployment automation for the Teams bot surface of this repo. This folder owns the Azure resources that host the bot web server in Azure Container Apps and connect it to Teams through Azure Bot Service.

This deployment is separate from the root Foundry environment deployment in [../../deployment/README.md](../../deployment/README.md), but it depends on the worker/storage side of that broader automation design.

---

## 📋 Prerequisites

| Requirement | Details |
|---|---|
| Azure CLI | Installed and authenticated with access to the `zolab` subscription |
| Docker | Local Docker daemon available for clean local image builds and pushes |
| Bot identity | User-assigned managed identity is the only supported bot auth model |
| Security subscription access | Optional for reading the DIBSecCom Log Analytics workspace keys in `Sentinel`; deployment can proceed without them |
| ACR push permissions | Required to log in and push bot images into the bot ACR |
| Subscription deployment permissions | Required for `az deployment sub create` against bot infrastructure |
| Existing worker resources | The bot deployment expects the worker storage account, worker resource group, and shared worker VNet/subnet path to exist for storage access |

---

## 🏗️ What Gets Deployed

The subscription-scoped Bicep entry point in [bot-infra.bicep](bot-infra.bicep) creates a dedicated bot resource group and then deploys the resource-group-scoped module in [modules/bot-resources.bicep](modules/bot-resources.bicep).

### Resources

| Resource | Type | Purpose |
|---|---|---|
| `zolab-bot-<suffix>` | Resource Group | Dedicated resource group for the bot surface |
| `zolabbotacr<suffix>` | Azure Container Registry | Stores the bot image |
| `zolab-bot-mi-<suffix>` | User-Assigned Managed Identity | Used for ACR pulls and bot-side Azure access |
| `zolab-bot-env-<suffix>-vnet` | Container Apps Environment | Delegated-subnet environment for the public bot app |
| `zolab-bot-ca-<suffix>-vnet` | Azure Container App | Hosts the aiohttp/M365 Agents SDK bot runtime with public ingress |
| `zolab-bot-<suffix>` | Azure Bot Service | Teams-facing bot registration |
| `MsTeamsChannel` | Bot Service channel | Enables Teams integration |

Azure Container Apps also creates a separate infrastructure resource group for each managed environment. In production this appears as `ME_zolab-bot-env-botprd-vnet_zolab-bot-botprd_eastus2`.

- The bot environment resource itself lives in `zolab-bot-botprd`.
- The `ME_...` resource group is Azure-managed infrastructure for that environment.
- It is expected to stay separate and should not be used for customer-managed resources.
- It is removed by Azure when the corresponding Container Apps environment is fully deleted.

### Logging and Identity

| Capability | Implementation |
|---|---|
| Log destination | Cross-subscription DIBSecCom Log Analytics workspace when workspace keys are accessible at deploy time |
| Container image pull | UAMI with `AcrPull` on the bot ACR |
| Bot Azure access | UAMI attached to the Container App and used by Azure Bot Service as `UserAssignedMSI` |
| Worker storage access | RBAC plus private endpoint routing through the worker-owned VNet |

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
| [bot-infra.bicepparam](bot-infra.bicepparam) | Parameter defaults for the MI-only bot deployment |
| [modules/bot-resources.bicep](modules/bot-resources.bicep) | Container App, ACR, UAMI, and Azure Bot Service resources |
| [../../deployment/remove-teams-app.ps1](../../deployment/remove-teams-app.ps1) | Removes the previous Teams catalog entry and current-user install before uploading a refreshed app package |

---

## ▶️ Deploy

From the repo root:

```bash
bash bot-app/deployment/deploy-bot-app.sh
```

The script performs four steps:

1. Builds the bot image locally with Docker using a clean `--no-cache` build, then pushes `zolabbotacrbotprd.azurecr.io/zolab-bot:<immutable-tag>` and refreshes `:latest`
2. Deploys the bot infrastructure Bicep template into the VNet-backed Container Apps environment by default
3. Grants Storage Queue and Blob RBAC to the bot UAMI on the worker storage account
4. Regenerates the Teams manifest and refreshes the in-repo Teams app zip with the live bot identity and domain

---

## 🔄 Update Flow

If only bot runtime code changed and you want to republish the bot surface, rerun:

```bash
bash bot-app/deployment/deploy-bot-app.sh
```

That rebuilds the bot image and refreshes the VNet-backed Container App deployment. The worker image is separate and is built from [../../deployment/Dockerfile.worker](../../deployment/Dockerfile.worker).

The deploy script now generates an immutable bot image tag from the current UTC timestamp and Git commit, builds locally with Docker using `--no-cache --pull`, and passes that tag into Bicep so Azure Container Apps creates a new revision for each rollout. This avoids both stale revisions and flaky remote build-task failures.

---

## 🔐 Required Inputs

The deploy script resolves these values at runtime:

- Teams app manifest ID
- Tenant ID
- DIBSecCom Log Analytics customer ID and shared key from the Security subscription when available
- Worker storage scope for RBAC assignment
- Worker-owned Container Apps infrastructure subnet resource ID when not explicitly supplied

Grounded weather narration is intentionally separate from the ephemeral Foundry environments created by `build it`.

- The bot deployment now provisions its own long-lived Azure AI Services account inside the bot resource group.
- That account hosts a dedicated `gpt-5.3-chat` deployment for bot-side LLM features.
- `weather` uses that stable endpoint for narration; it no longer depends on `build_info-*.json` or any user-triggered Foundry build.
- `msft_docs` is already decoupled from Foundry builds because it queries the Microsoft Learn MCP endpoint directly.
- The bot managed identity gets `Azure AI User` on the bot-owned AI Services account from Bicep during deployment.

The deploy script does not use an app registration or Key Vault secret. The bot runtime and Azure Bot Service both use the bot UAMI.

The script currently assumes the production-style suffix `botprd` and the matching resource names used across the repo.

---

## ✅ Verification

After deployment, confirm:

- The Container App exists and serves `https://<fqdn>/api/messages`
- The Azure Bot Service endpoint points to the Container App `/api/messages` path
- The bot UAMI has queue/blob access to `zolabworkerstbotprd`
- The storage account remains `publicNetworkAccess: Disabled`
- The bot app is attached to the VNet-backed environment and the worker is attached to the delegated worker subnet
- The Container Apps Environment is forwarding logs to DIBSecCom LAW when workspace keys were available during deploy

Useful commands:

```bash
az containerapp show -n zolab-bot-ca-botprd-vnet -g zolab-bot-botprd -o json
az containerapp logs show -n zolab-bot-ca-botprd-vnet -g zolab-bot-botprd
az containerapp revision list -n zolab-bot-ca-botprd-vnet -g zolab-bot-botprd -o table
```

To remove a stale Teams catalog package before reinstalling the refreshed archive:

```powershell
pwsh -NoProfile -File deployment/remove-teams-app.ps1
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
| where ContainerAppName_s == "zolab-bot-ca-botprd-vnet"
| order by TimeGenerated desc
```

```kusto
ContainerAppSystemLogs_CL
| where ContainerAppName_s == "zolab-bot-ca-botprd-vnet"
| order by TimeGenerated desc
```

If you are looking for the bot rollout or crash history, start with the system logs table and then filter down to the current `zolab-bot-ca-botprd-vnet` revision names shown by `az containerapp revision list`.

---

## 🔗 Related Docs

| Document | Use it for |
|---|---|
| [../README.md](../README.md) | Bot workspace overview |
| [../runtime/README.md](../runtime/README.md) | Runtime code and local dev |
| [../../deployment/README.md](../../deployment/README.md) | Foundry environment deployment and cleanup |

---

## 📝 Notes

- The bot deploy script currently handles the bot image and bot-side infrastructure only.
- The worker container and worker ACR are defined under the repo-root `deployment/` folder because they execute the shared PowerShell/Bicep automation.
- The bot Container App uses `activeRevisionsMode: Single`, so redeployments naturally roll forward to a single active revision.
- The initial private-storage migration path is captured in [../../deployment/deploy-private-storage-rollout.sh](../../deployment/deploy-private-storage-rollout.sh), which stages the worker first, then the new public bot endpoint, and only falls back if validation fails.
- The `ME_...` resource group attached to the Container Apps environment is service-managed Azure infrastructure and is not intended to be moved into the main bot resource group.