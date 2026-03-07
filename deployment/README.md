# 🚀 Microsoft Foundry — Bicep Deployment

Infrastructure-as-Code deployment for an Microsoft Foundry environment with full RBAC, diagnostic settings, and cross-subscription Log Analytics integration.

---

## 📋 Prerequisites

Before running the deployment, ensure the following are in place:

| Requirement | Details |
|---|---|
| **Azure Subscriptions** | Access to both the `zolab` (workload) and `Security` (monitoring) subscriptions |
| **Azure RBAC Permissions** | `Owner` or `Contributor` + `User Access Administrator` on the `zolab` subscription; `Contributor` on the `Security` subscription's `Sentinel` resource group |
| **Microsoft Entra ID Permissions** | Ability to create security groups and manage members (`Group.ReadWrite.All`, `GroupMember.ReadWrite.All` in Microsoft Graph) |
| **Teams Chat Flow (optional)** | Same-tenant Graph access in `dibsecurity.onmicrosoft.com` with delegated `User.Read`, `Chat.Create`, `Chat.ReadWrite`, and `ChatMessage.Send` |
| **Azure CLI** | Installed and authenticated — [Install Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) |
| **Bicep CLI** | Installed via `az bicep install` — [Install Bicep](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/install) |
| **Az PowerShell Module** | Installed and authenticated — `Install-Module Az -Scope CurrentUser` |
| **Microsoft.Graph PowerShell** | Auto-installed by the script if missing (`Microsoft.Graph.Groups`; `Microsoft.Graph.Teams` when `-UseTeamsChatFlow` is enabled) |

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
├── deploy-foundry-env.ps1        # Orchestration script (deploy + cleanup)
├── teams-command-dispatch.ps1    # Teams chat command listener for build/teardown
├── teams-chat.ps1                # Teams chat helpers for Graph-based selection/notifications
├── main.bicep                    # Subscription-scoped entry point
├── law-rbac.bicep                # Cross-sub LAW RBAC (Security subscription)
├── modules/
│   ├── resources.bicep           # All resources, diagnostics, and RBAC
│   └── law-rbac.bicep            # LAW role assignment module
└── README.md                     # You are here 📍
```

---

## ▶️ Deploy

```powershell
cd deployment
.\deploy-foundry-env.ps1
```

Optional Teams-driven flow:

```powershell
cd deployment
.\deploy-foundry-env.ps1 -UseTeamsChatFlow
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

When `-UseTeamsChatFlow` is enabled, the script creates or reuses a self-owned Teams group chat named `Microsoft Foundry Deployments`, prompts for the model selection in that chat, waits for a valid reply, and posts the final build success/failure notification there. Reply with either the menu number or the model name.
The same Teams chat also receives a complete teardown status report after cleanup operations when `-UseTeamsChatFlow` is used.

### Teams Command Listener

```powershell
cd deployment
.\teams-command-dispatch.ps1
```

Once the listener is running, send one of these commands in the Teams chat:

- `build it`
- `list builds`
- `build status 'zolab-ai-6bmycg'`
- `teardown 'zolab-ai-6bmycg'`
- `listener status`
- `?`
- `stop listener`

The listener validates the request, then asks for confirmation:

- Build: reply `1` to build or `2` to abort
- List builds: no confirmation required
- Build status: reply `1` to confirm or `2` to abort
- Teardown: reply `1` to confirm teardown or `2` to abort
- Listener status / `?`: no confirmation required

By default, the confirmation prompt stays open for 30 minutes before it expires.
After each confirmed build or teardown, the listener sends the full status report back to the Teams chat.
The listener stays online until you explicitly send `stop listener` in the Teams chat.
Use `?` any time to get the current command list, and use `listener status` for a quick health snapshot that reports the online indicator, account, chat topic, PID, and current UTC time.

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

---

## 🧹 Cleanup

```powershell
cd deployment
.\deploy-foundry-env.ps1 -Cleanup
```

Target a single deployment resource group:

```powershell
cd deployment
.\deploy-foundry-env.ps1 -Cleanup -CleanupResourceGroup zolab-ai-6bmycg
```

Cleanup will:

1. ❌ Remove all RBAC role assignments for `zolab-ai-dev` (both subscriptions)
2. 🗑️ Delete the `zolab-ai-<suffix>` resource group and all resources within it
3. 🧼 Purge soft-deleted Cognitive Services accounts (prevents redeploy conflicts)
4. 📋 Remove subscription-level deployment records from both subscriptions
5. ✅ Preserve the `zolab-ai-dev` Entra group (not deleted)

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

## 📝 Notes

- The resource group suffix is **randomly generated** at deploy time — each run creates a unique environment, allowing multiple deployments within a single subscription.
- The deploying user is automatically added to the `zolab-ai-dev` Entra group.
- The `zolab-ai-dev` group is created once and persists across cleanup/redeploy cycles.
- Diagnostic settings send **all logs** (not metrics) to the centralized DIBSecCom Log Analytics Workspace for security observability.
- Key Vault uses `AzureDiagnostics` destination type for compatibility with Sentinel analytics rules.
