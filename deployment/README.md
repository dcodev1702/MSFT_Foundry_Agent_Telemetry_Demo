# 🚀 Microsoft Foundry — Bicep Deployment

Infrastructure-as-Code deployment for an Microsoft Foundry environment with full RBAC, diagnostic settings, and cross-subscription Log Analytics integration.

---

## 📋 Prerequisites

Before running the deployment, ensure the following are in place:

| Requirement | Details |
|---|---|
| **Azure Subscriptions** | Access to both the `zolab` (workload) and `Security` (monitoring) subscriptions |
| **Azure RBAC Permissions** | `Owner` or `Contributor` + `User Access Administrator` on the `zolab` subscription; `Contributor` on the `Security` subscription's `Sentinel` resource group |
| **Microsoft Entra ID Permissions** | Ability to create security groups (`Group.ReadWrite.All` in Microsoft Graph) |
| **Azure CLI** | Installed and authenticated — [Install Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) |
| **Bicep CLI** | Installed via `az bicep install` — [Install Bicep](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/install) |
| **Az PowerShell Module** | Installed and authenticated — `Install-Module Az -Scope CurrentUser` |
| **Microsoft.Graph PowerShell** | Auto-installed by the script if missing (`Microsoft.Graph.Groups`) |

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
| 📊 Application Insights | `AppInsights` | Auto-connected to AI Foundry — enables Traces (preview) without manual portal setup |

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

The script will:

1. 🔑 Resolve subscription IDs dynamically by name (`zolab` and `Security`)
2. 👥 Create the `zolab-ai-dev` Entra security group (or reuse if it exists)
3. 👤 Add the deploying user to the `zolab-ai-dev` group (if not already a member)
4. 🎲 Generate a random 6-char alphanumeric suffix (supports multiple deployments per subscription)
5. 🏗️ Deploy all Foundry resources via Bicep to a new `zolab-ai-<suffix>` resource group
6. 🔒 Assign RBAC roles to the `zolab-ai-dev` group and the Foundry managed identity
7. 📊 Configure diagnostic settings on Key Vault and Blob Storage
8. 📡 Assign Log Analytics Reader on DIBSecCom workspace in the Security subscription

A unique 6-character suffix is generated deterministically from the subscription ID, ensuring consistent naming across re-deployments.

---

## 🧹 Cleanup

```powershell
cd deployment
.\deploy-foundry-env.ps1 -Cleanup
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
