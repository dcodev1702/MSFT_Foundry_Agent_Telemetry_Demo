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

Use `pwsh` (PowerShell 7) to launch the deployment and Teams listener scripts on Windows so UTF-8 output renders and parses consistently.

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

- The scripts reconnect with `Connect-MgGraph -ContextScope CurrentUser`, so once tenant-wide admin consent is in place the delegated scopes can be reused by later sessions under the same Windows profile.
- Admin consent is persistent, but the signed-in Graph token is not. After PIM elevation, new consent, or any role/scope change, reconnect with `Connect-MgGraph` and restart the listener so it picks up a fresh token.
- If the chat scopes are missing, or the token was issued before the latest consent/PIM state, Teams commands can appear to arrive but the listener can fail to respond. The most common symptom is `403 Forbidden` / `InsufficientPrivileges` from `New-MgChatMessage`, followed by a missing heartbeat/build-status/build reply in the chat.
- Teams-triggered runs now validate both the Az PowerShell context and the Azure CLI sign-in before deployment work starts; if either one is using the wrong account, the build fails fast with a remediation message instead of creating a partially visible environment.
- The automation intentionally uses a self-owned **group chat** named `Microsoft Foundry Deployments`; that is the supported pattern for this workflow.
- `zolab-ai-dev` now receives `Reader` on each deployment resource group so the App Insights resource and Foundry App Insights connection remain visible after Teams-triggered builds.
- If you change listener command handling, restart the detached listener so the running process picks up the new code.
- Use `listener status` after startup to confirm the listener is online with the expected Graph account, Azure PowerShell account, Azure CLI account, subscription, chat topic, PID, and current UTC time.

### Teams Command Listener

```powershell
cd deployment
pwsh .\teams-command-dispatch.ps1
```

Once the listener is running, send one of these commands in the Teams chat:

- `build it`
- `heartbeat`
- `list builds`
- `build status 'zolab-ai-6bmycg'`
- `teardown 'zolab-ai-6bmycg'`
- `listener status`
- `?`
- `stop listener`

The listener validates the request, then asks for confirmation:

- Build: reply `1` to build or `2` to abort
- Heartbeat: no confirmation required
- List builds: no confirmation required
- Build status: reply `1` to confirm or `2` to abort
- Teardown: reply `1` to confirm teardown or `2` to abort
- Listener status / `?`: no confirmation required

By default, the build, build-status, and teardown confirmation prompts stay open for 10 minutes before they expire.
The listener also posts an automatic heartbeat to the Teams chat every 30 minutes while it remains online.
While a build is actively running, the automation posts `🚧 One moment ..the Bob's are still building! 🚧` every 1 minute. During teardown, it posts `🚧 Pls hold while we teardown: <resource-group> 🚧` every 1 minute until the cleanup finishes.
After each confirmed build or teardown, the listener sends the full status report back to the Teams chat.
The listener stays online until you explicitly send `stop listener` in the Teams chat.
Use `?` any time to get the current command list, use `listener status` for a quick health snapshot, and use `heartbeat` for a detailed per-line readout that includes the pwsh version, uptime, memory usage, script name, PID, last Teams response, Graph API connectivity, chat topic, and running identity.

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
.\deploy-foundry-env.ps1 -Cleanup
```

Target a single deployment resource group:

```powershell
cd deployment
.\deploy-foundry-env.ps1 -Cleanup -CleanupResourceGroup zolab-ai-6bmycg
```

Cleanup will:

1. 🗑️ Delete the requested `zolab-ai-<suffix>` resource group (or all managed Foundry resource groups when `-CleanupResourceGroup` is omitted)
2. 🧼 Purge soft-deleted Cognitive Services accounts (prevents redeploy conflicts)
3. 📋 Remove the matching subscription deployment records
4. 🔐 Keep shared LAW Reader RBAC in place while any other managed build still exists
5. 👤 Keep the current user in `zolab-ai-dev` while they still own another active build (tracked via `requested_by` in `build_info-<suffix>.json`)
6. ✅ Preserve the `zolab-ai-dev` Entra group itself (not deleted)

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
