# 🚀 Microsoft Foundry — Bicep Deployment

Infrastructure-as-Code deployment for an Microsoft Foundry environment with full RBAC, diagnostic settings, and cross-subscription Log Analytics integration.

---

## 📋 Prerequisites

Before running the deployment, ensure the following are in place:

| Requirement | Details |
|---|---|
| **Azure Subscriptions** | Access to both the `zolab` (workload) and `Security` (monitoring) subscriptions |
| **Azure RBAC Permissions** | `Owner` or `Contributor` + `User Access Administrator` on the `zolab` subscription; `Owner` or `User Access Administrator` on the `Security` subscription or the `DIBSecCom` workspace scope because the deployment writes RBAC assignments in both places |
| **Microsoft Entra ID Permissions** | Ability to create security groups and manage members (`Group.ReadWrite.All`, `GroupMember.ReadWrite.All` in Microsoft Graph) |
| **Teams Chat Flow (optional)** | Same-tenant Graph access in `dibsecurity.onmicrosoft.com` with delegated `User.Read`, `Chat.Create`, `Chat.ReadWrite`, and `ChatMessage.Send` |
| **Azure CLI** | Installed and authenticated — [Install Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) |
| **Bicep CLI** | Installed via `az bicep install` — [Install Bicep](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/install) |
| **Az PowerShell Module** | Installed and authenticated — `Install-Module Az -Scope CurrentUser` |
| **Microsoft.Graph PowerShell** | Auto-installed by the script if missing (`Microsoft.Graph.Groups`; `Microsoft.Graph.Teams` when `-UseTeamsChatFlow` is enabled) |

Use `pwsh` (PowerShell 7) to launch the deployment and Teams listener scripts on both macOS and Windows so the same commands, encoding, and module behavior are used everywhere.

---

## 🏗️ What Gets Deployed

### Resources (in `zolab-ai-<suffix>` resource group)

| Resource | Type | Configuration |
|---|---|---|
| 🗄️ **Storage Account** | `StorageV2 / Standard_LRS` | HTTPS-only, TLS 1.2, no public access, no shared key, blob/file encryption |
| 🔐 **Key Vault** | `Standard` | RBAC authorization, purge protection, soft delete (7 days), no public access |
| 📊 **Application Insights** | `Web / LogAnalytics` | 90-day retention, telemetry → DIBSecCom LAW in Security subscription |
| 🤖 **AI Foundry Account** | `AIServices / S0` | System-assigned managed identity, project management enabled |
| 📁 **AI Foundry Project** | `AIServices` | System-assigned managed identity, child of Foundry account |

### Diagnostic Settings

| Resource | Logs | Destination |
|---|---|---|
| 🔐 Key Vault | `allLogs` | DIBSecCom LAW (Security subscription) |
| 🗄️ Blob Storage | `allLogs` | DIBSecCom LAW (Security subscription) |

### Connections

| Connection | Type | Purpose |
|---|---|---|
| 📊 Application Insights | `AppInsights` | Auto-connected to this AI Foundry project only — enables Traces (preview) without manual portal setup |
| 🌐 Foundry Project Endpoint | `AIServices Endpoint` | Project endpoint used as the `endpoint` in `AIProjectClient` |
| 🤖 Azure OpenAI Endpoint | `Cognitive Services Endpoint` | Endpoint used for Azure OpenAI model inference calls |

### Model Deployments

The deployment script requires an AI model selection and only allows these options:

| Menu Choice | Resolved Model Name | Version Selection | SKU Selection | Capacity |
|---|---|---|---|---|
| 🧠 `gpt-4.1-mini` | `gpt-4.1-mini` | Latest available in target region | Prefer `GlobalStandard`, otherwise next deployable SKU | Up to 250 |
| 🧠 `gpt-5.3` | `gpt-5.3*` (for example `gpt-5.3-chat`) | Latest available in target region | Prefer `GlobalStandard`, otherwise next deployable SKU | Up to 250 |
| 🧠 `gpt-5.4` | `gpt-5.4*` (if available in target region) | Latest available in target region | Prefer `GlobalStandard`, otherwise next deployable SKU | Up to 250 |
| 🧠 `grok-4-1-fast-reasoning` | `grok-4-1-fast-reasoning` | Latest available in target region | Prefer `GlobalStandard`, otherwise next deployable SKU | Up to 250 |

### RBAC Role Assignments

**`zolab-ai-dev` Entra Security Group →**

| Role | Scope |
|---|---|
| Azure AI Developer | Resource Group |
| Azure AI User | Resource Group |
| Reader | Resource Group |
| Storage Blob Data Contributor | Resource Group |
| Key Vault Secrets Officer | Resource Group |
| Key Vault Crypto Officer | Resource Group |
| Log Analytics Reader | DIBSecCom workspace (Security subscription) |

**AI Foundry Managed Identity →**

| Role | Scope |
|---|---|
| Key Vault Secrets Officer | Resource Group |
| Key Vault Contributor | Resource Group |
| Storage Blob Data Contributor | Resource Group |
| Contributor | Resource Group |

---

## 📂 File Structure

```
deployment/
├── deploy-foundry-env.ps1           # Orchestration script (deploy + cleanup)
├── teams-command-dispatch.ps1       # Teams chat command listener for build/teardown
├── teams-chat.ps1                   # Teams chat helpers for Graph-based selection/notifications
├── main.bicep                       # Subscription-scoped entry point (Foundry env)
├── law-rbac.bicep                   # Cross-sub LAW RBAC (Security subscription)
├── Dockerfile.worker                # Worker container (Python + PowerShell + Az CLI + Bicep)
├── deploy-worker-app.sh             # Worker deploy script (local Docker build + ACR push + Bicep)
├── modules/
│   ├── resources.bicep              # Foundry resources, diagnostics, and RBAC
│   ├── law-rbac.bicep               # LAW role assignment module
│   └── worker-resources.bicep       # ACI worker + Storage (Queue + Blob)
├── worker-infra.bicep               # Worker infrastructure entry point
└── README.md                        # You are here

bot-app/
├── Dockerfile                       # Bot container (Python + M365 Agents SDK)
├── teams-app/
│   ├── manifest.json                # Teams app manifest (sideloading)
│   ├── color.png                    # 192x192 bot icon
│   └── outline.png                  # 32x32 outline icon
├── deployment/
│   ├── deploy-bot-app.sh            # Bot deploy script (local Docker build + Bicep + RBAC)
│   ├── bot-infra.bicep              # Subscription-scoped bot infrastructure
│   └── modules/
│       └── bot-resources.bicep      # Container App + ACR + Bot Service + UAMI
└── runtime/
  ├── src/
  │   ├── app.py                   # aiohttp host + M365 Agents SDK adapter
  │   ├── bot.py                   # Teams message/event handlers
  │   ├── worker.py                # Background queue worker
  │   ├── worker_standalone.py     # Standalone worker entry point
  │   ├── proactive.py             # Proactive messaging service
  │   ├── heartbeat.py             # Periodic heartbeat broadcaster
  │   ├── command_parser.py        # Command parser
  │   └── conversation_store.py    # Azure Blob conversation store
    ├── job_dispatcher.py            # Azure Queue job dispatcher
    ├── storage_config.py            # Shared Azure credential config
    ├── models.py                    # Data models
    └── requirements.txt             # Python dependencies
```

---

## ▶️ Deploy

```powershell
cd deployment
pwsh ./deploy-foundry-env.ps1
```

Optional Teams-driven flow:

```powershell
cd deployment
pwsh ./deploy-foundry-env.ps1 -UseTeamsChatFlow
```

The script will:

1. 🔑 Resolve subscription IDs dynamically by name (`zolab` and `Security`)
2. 👥 Create the `zolab-ai-dev` Entra security group (or reuse if it exists)
3. 👤 Add the deploying user to the `zolab-ai-dev` group (if not already a member)
4. 🧠 Prompt the user to choose one of four allowed AI model choices (`gpt-4.1-mini`, `gpt-5.3`, `gpt-5.4`, `grok-4-1-fast-reasoning`) from a menu
5. ✅ Validate the selected model against regional availability before deployment, prompting again from the remaining options if needed
6. 🎲 Generate a random 6-char alphanumeric suffix (supports multiple deployments per subscription)
7. 🏗️ Deploy all Foundry resources via Bicep to a new `zolab-ai-<suffix>` resource group
8. 🔒 Assign RBAC roles to the `zolab-ai-dev` group and the Foundry managed identity
9. 📊 Configure diagnostic settings on Key Vault and Blob Storage
10. 📡 Assign Log Analytics Reader on DIBSecCom workspace in the Security subscription

When this deployment runs under the worker managed identity, that identity must have enough RBAC to do two distinct things:

1. Create resources in the `zolab` subscription.
2. Create and remove `Microsoft.Authorization/roleAssignments` resources in both the `zolab` subscription and on the `DIBSecCom` workspace scope in the `Security` subscription.

`Contributor` alone is not enough for the RBAC portion because it explicitly excludes `Microsoft.Authorization/*/Write`.

For worker rollouts, avoid relying on a retagged `latest` image if you need deterministic pickup in Azure Container Instances. `worker-infra.bicep` now accepts `workerImageTag`, and `deployment/deploy-worker-app.sh` performs a local Docker `--no-cache --pull` build, pushes both an immutable tag and `latest`, and then deploys ACI pinned to the immutable tag.

Worker local deployment:

```bash
cd /path/to/repo
bash deployment/deploy-worker-app.sh
```

When `-UseTeamsChatFlow` is enabled, the script creates or reuses a self-owned Teams group chat named `Microsoft Foundry Deployments`, prompts for the model selection in that chat, waits for a valid reply, and posts the final build success/failure notification there. Reply with either the menu number or the model name.
The same Teams chat also receives a complete teardown status report after cleanup operations when `-UseTeamsChatFlow` is used.

Microsoft Graph requirements for this flow:

- Sign in to Microsoft Graph with the DIB tenant account that will operate the chat flow (`lireland@DibSecurity.onmicrosoft.com`).
- Ensure the **Microsoft Graph Command Line Tools** enterprise application in the tenant has tenant-wide admin consent for the delegated scopes used by this workflow.
- Teams chat permissions required for both the build script and the listener:
  - `User.Read`
  - `Chat.Create`
  - `Chat.ReadWrite`
  - `ChatMessage.Send`
- Additional deployment permissions still required by `deploy-foundry-env.ps1` because it manages the `zolab-ai-dev` Entra group:
  - `Group.ReadWrite.All`
  - `GroupMember.ReadWrite.All`

Recommended admin-consent baseline for `Microsoft Graph Command Line Tools`:

- `User.Read`
- `Chat.Create`
- `Chat.ReadWrite`
- `ChatMessage.Send`
- `Group.ReadWrite.All`
- `GroupMember.ReadWrite.All`

Helpful notes:

- Treat the local `pwsh ./deploy-foundry-env.ps1` path and the bot or worker managed identity path as two different operator modes. When you run the script from your Mac or Windows workstation, Azure CLI and Az PowerShell evaluate your desktop identity, not the Azure managed identity attached to the live worker. That means local runs are sensitive to stale PIM state, stale `az login` tokens, and mismatched Az PowerShell context even when the Azure-hosted automation is healthy.
- Prefer Teams-triggered or queue-driven builds for normal operations. Those flows run under the Azure-hosted managed identity and are the most reliable path for production-like build and teardown work.
- Reserve direct local deployment for development, debugging, or break-glass administration. After PIM elevation or any RBAC change, refresh both auth stacks before rerunning the script: reconnect Azure CLI, reconnect Az PowerShell, and then retry the deployment so the local session picks up the new role assignments.
- The scripts default to `Connect-MgGraph -ContextScope CurrentUser` on every platform so auth state can be reused consistently. If a macOS/Linux shell cannot write the Graph auth cache, either fix the cache directory permissions or override the session with `FOUNDRY_GRAPH_CONTEXT_SCOPE=Process`.
- Admin consent is persistent, but the signed-in Graph token is not. After PIM elevation, new consent, or any role/scope change, reconnect with `Connect-MgGraph` and restart the listener so it picks up a fresh token.
- If the chat scopes are missing, or the token was issued before the latest consent/PIM state, Teams commands can appear to arrive but the listener can fail to respond. The most common symptom is `403 Forbidden` / `InsufficientPrivileges` from `New-MgChatMessage`, followed by a missing heartbeat/build-status/build reply in the chat.
- Teams-triggered runs now validate both the Az PowerShell context and the Azure CLI sign-in before deployment work starts; if either one is using the wrong account, the build fails fast with a remediation message instead of creating a partially visible environment.
- The automation intentionally uses a self-owned **group chat** named `Microsoft Foundry Deployments`; that is the supported pattern for this workflow.
- `zolab-ai-dev` now receives `Reader` on each deployment resource group so the App Insights resource and Foundry App Insights connection remain visible after Teams-triggered builds.
- If you change listener command handling, restart the detached listener so the running process picks up the new code.
- Use `listener status` after startup to confirm the listener is online with the expected Graph account, Azure PowerShell account, Azure CLI account, subscription, chat topic, PID, current UTC time, and whether mutating commands are enabled.

### Legacy Teams Command Listener

This listener is no longer the preferred operational path. The production path is the Teams bot plus queue-backed worker. `teams-command-dispatch.ps1` runs under the desktop identity, so it now starts in diagnostics-only mode by default.

```powershell
cd deployment
pwsh ./teams-command-dispatch.ps1
```

That starts the listener in diagnostics-only mode. `build it` and `teardown` will be blocked unless you explicitly opt into the legacy local-execution path for non-production testing:

```powershell
cd deployment
pwsh ./teams-command-dispatch.ps1 -AllowMutatingCommands
```

Once the listener is running, send one of these commands in the Teams chat:

- `build it`
- `heartbeat`
- `list builds`
- `build status 'zolab-ai-6bmycg'`
- `teardown`
- `teardown 'zolab-ai-6bmycg'`
- `listener status`
- `?`
- `stop listener`

The listener validates the request, then asks for confirmation:

- Build: blocked by default; only available when the listener is started with `-AllowMutatingCommands`
- Heartbeat: no confirmation required
- List builds: no confirmation required
- Build status: reply `1` to confirm or `2` to abort
- Teardown: blocked by default; only available when the listener is started with `-AllowMutatingCommands`
- Listener status / `?`: no confirmation required

By default, the build-status confirmation prompts stay open for 10 minutes before they expire.
The listener also posts an automatic heartbeat to the Teams chat every 30 minutes while it remains online.
When mutating commands are explicitly enabled, the listener posts `🚧 👷 The Bobs Are Still Building 👷🚧 ` every 1 minute during builds and `🚧 👷 The Bobs Are Still Tearing Down: <resource-group> 👷🚧` every 1 minute during teardown until completion.
After each confirmed build, build-status, or teardown command, the listener sends the full status report back to the Teams chat.
The listener stays online until you explicitly send `stop listener` in the Teams chat.
Use `?` any time to get the current command list, use `listener status` for a quick health snapshot, and use `heartbeat` for a detailed per-line readout that includes the pwsh version, uptime, memory usage, script name, PID, last Teams response, Graph API connectivity, chat topic, and running identity.

Post-deploy smoke checks:

```bash
cd /path/to/repo
bash deployment/run-smoke-checks.sh
```

That script verifies the live bot revision, worker runtime, and current worker build metadata, then prints the manual Teams commands to exercise after rollout.

Upon completion, the script outputs all resource names and writes `build_info-<suffix>.json` at the repo root for notebook configuration.

- 🌐 **Foundry Project Endpoint** — stored in `build_info-<suffix>.json` as `foundry_project_endpoint` and loaded by the Win11 notebook into `foundry_proj_ep`
- 🤖 **Model Endpoint** — stored in `build_info-<suffix>.json` as `azure_openai_endpoint`
- 🧠 **Model Name** — stored in `build_info-<suffix>.json` as `genai_model` and used by the notebook when creating the agent

The generated `build_info-<suffix>.json` includes:

- `rg`
- `appinsights`
- `foundry_project_endpoint`
- `azure_openai_endpoint`
- `storage_account`
- `key_vault`
- `genai_model`
- `foundry_name`
- `foundry_project_name`
- `requested_by`

---

## 🧹 Cleanup

```powershell
cd deployment
pwsh ./deploy-foundry-env.ps1 -Cleanup
```

Target a single deployment resource group:

```powershell
cd deployment
pwsh ./deploy-foundry-env.ps1 -Cleanup -CleanupResourceGroup zolab-ai-6bmycg
```

Preview a targeted teardown without removing anything:

```powershell
cd deployment
pwsh ./deploy-foundry-env.ps1 -Cleanup -CleanupResourceGroup zolab-ai-6bmycg -PreviewCleanup
```

Cleanup will:

1. 🗑️ Delete the requested `zolab-ai-<suffix>` resource group (or all managed Foundry resource groups when `-CleanupResourceGroup` is omitted)
2. 🧼 Purge soft-deleted Cognitive Services accounts (prevents redeploy conflicts)
3. 📋 Remove the matching subscription deployment records
4. 🔐 Keep shared LAW Reader RBAC in place while any other managed build still exists
5. 👤 Keep the current user in `zolab-ai-dev` while any other managed build still exists
6. ✅ Preserve the `zolab-ai-dev` Entra group itself (not deleted)

`-PreviewCleanup` is limited to targeted teardown. It reports which of the six managed RG role assignments would be removed, which non-managed RG assignments would be preserved, whether shared LAW RBAC would be retained or removed, and whether the current user would stay in or be removed from `zolab-ai-dev`.

---

## 🔧 Configuration

Key values are resolved at runtime in `deploy-foundry-env.ps1`:

```powershell
$subscriptionId         = (Get-AzSubscription -SubscriptionName "zolab").Id
$securitySubscriptionId = (Get-AzSubscription -SubscriptionName "Security").Id
$location               = "eastus2"
$groupDisplayName       = "zolab-ai-dev"
```

No hardcoded subscription GUIDs — subscriptions are looked up by display name.

---

## 🤖 Bot Infrastructure (Container Apps)

The `bot-app/` directory contains a separate deployment for **Bot the Builder**, a Teams bot that manages Foundry deployments via chat commands.

### Bot-The-Builder Architecture

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="./images/foundry-bot-arch-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="./images/foundry-bot-arch-light.png">
  <img alt="Architecture Diagram" src="./images/architecture-dark.png">
</picture>


```
Teams ──► Azure Bot Service (F0, UserAssignedMSI)
              │
              ▼
  Azure Container App (zolab-bot-ca-botprd-vnet)
     ┌─────────────────────────────────────────┐
     │  M365 Agents SDK  │  aiohttp (:8000)    │
     │  JWT Auth          │  Heartbeat (15m)    │
     │  Proactive Msgs    │  UAMI Auth          │
     └────────┬───────────┬────────────────────┘
              │           │
    ┌─────────▼──────────┐  ┌────▼──────────────┐
    │ Private Queue PE   │  │ Private Blob PE   │
    │  (botjobs)         │  │  (botstate)       │
    └──────┬─────────────┘  └───────────────┬──┘
           │
           ▼
     ACI Worker (zolab-worker-aci-botprd)
     ┌─────────────────────────────────────────┐
     │  Polls queue ──► PowerShell/Bicep        │
     │  Sends results ──► Proactive messaging   │
     └─────────────────────────────────────────┘

  Shared VNet (zolab-worker-vnet-botprd)
  ├── snet-containerapps
  ├── snet-worker-aci
  └── snet-storage-private-endpoints
```

### Key Components

| Component | Resource | Details |
|-----------|----------|---------|
| Bot Server | Azure Container App | M365 Agents SDK, auto-TLS, public ingress on a VNet-backed environment |
| Worker | Azure Container Instance | PowerShell 7.4 + Az CLI + Bicep, polls Azure Queue from a delegated subnet |
| Queue | Azure Queue Storage | RBAC-only (`allowSharedKeyAccess: false`) via private endpoint |
| State | Azure Blob Storage | Conversation refs + identities for proactive messaging via private endpoint |
| Network | Shared worker VNet | Dedicated subnets for ACA infrastructure, ACI, and storage private endpoints |
| Identity | User-Assigned MI | Single UAMI for bot + worker (ACR, Storage, Az ops) |
| Logging | DIBSecCom LAW | Cross-subscription logging to Security sub when workspace keys are available |

Azure Container Apps also provisions a separate Azure-managed infrastructure resource group for each managed environment. For the live bot environment this appears as `ME_zolab-bot-env-botprd-vnet_zolab-bot-botprd_eastus2`.

- The managed environment resource still lives in the main bot resource group.
- The `ME_...` resource group is owned by the Container Apps service and remains separate by design.
- Do not place application resources in that group or try to consolidate it into `zolab-bot-botprd`.

### Bot Deploy

```bash
bash bot-app/deployment/deploy-bot-app.sh
```

### Private-Storage Rollout

```bash
bash deployment/deploy-private-storage-rollout.sh
```

That staged rollout script exists for migrations or break-glass redeployments where the worker subnet integration, storage private endpoints, bot cutover, and rollback behavior need to be orchestrated together.

See [`bot-app/runtime/README.md`](../bot-app/runtime/README.md) for full bot documentation.

---

## 📝 Notes

- The resource group suffix is **randomly generated** at deploy time — each run creates a unique environment, allowing multiple deployments within a single subscription.
- The deploying user is automatically added to the `zolab-ai-dev` Entra group.
- The `zolab-ai-dev` group is created once and persists across cleanup/redeploy cycles.
- Diagnostic settings send **all logs** (not metrics) to the centralized DIBSecCom Log Analytics Workspace for security observability.
- Key Vault uses `AzureDiagnostics` destination type for compatibility with Sentinel analytics rules.
