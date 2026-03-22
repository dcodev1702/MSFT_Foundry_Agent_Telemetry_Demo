# Bot the Builder — Teams Bot for Azure AI Foundry

A Microsoft Teams bot built on the M365 Agents SDK that manages Azure AI Foundry deployments via chat commands. Runs as a **public Azure Container App on a VNet-backed environment** with a separate **subnet-integrated ACI worker** for long-running PowerShell operations.

## Related Docs

| Document | Use it for |
|----------|------------|
| [../README.md](../README.md) | Bot workspace overview and folder-level navigation |
| [../deployment/README.md](../deployment/README.md) | Bot infrastructure deployment, ACR build flow, and verification |
| [../../deployment/README.md](../../deployment/README.md) | Root Foundry environment deployment and teardown automation |

## Architecture

```
                   Teams
                     |
                     v
            ┌────────────────┐
            │   Azure Bot    │
            │   Service (F0) │
            └───────┬────────┘
                    |
                    v
  ┌──────────────────────────────────┐
  │  Azure Container App             │
  │  (zolab-bot-ca-botprd-vnet)      │
  │                                  │
  │  - aiohttp web server (:8000)    │
  │  - M365 Agents SDK handlers      │
  │  - JWT auth middleware            │
  │  - Proactive messaging           │
     │  - Heartbeat service (2 hours)   │
  │                                  │
  │  Identity: UAMI (zolab-bot-mi)   │
  └──────────┬───────────────────────┘
             |
     ┌───────┴────────┐
     v                v
┌───────────────┐    ┌──────────────────┐
│ Private Queue │    │   Private Blob   │
│   endpoint    │    │    endpoint      │
│  (botjobs)    │    │   (botstate)     │
└────┬──────────┘    └──────────────────┘
     |
     v
┌──────────────────────────────────┐
│  ACI Worker Container            │
│  (zolab-worker-aci-botprd)       │
│                                  │
│  - Polls queue every 5s          │
│  - Executes PowerShell/Bicep     │
│  - Sends results via proactive   │
│    messaging back to Teams       │
│                                  │
│  Identity: UAMI (zolab-bot-mi)   │
└──────────────────────────────────┘
```

### Key Design Decisions

- **Azure Container Apps** instead of App Service (subscription has zero `Microsoft.Web` quota on `Internal_2014-09-01` offer)
- **Azure Queue Storage** for job dispatch (RBAC-only, `allowSharedKeyAccess: false`)
- **Azure Blob Storage** for conversation state (`conversations.json`, `references.json`, `identities.json`)
- **Private endpoints + private DNS** for queue and blob access while keeping Teams ingress public
- **User-Assigned Managed Identity** for all Azure access (ACR pull, Storage, Az PowerShell)
- **Cross-subscription logging** to `DIBSecCom` LAW in the Security subscription
- **DefaultAzureCredential** everywhere — no connection strings or storage keys

## Commands

| Command | Description |
|---------|-------------|
| `build it` | Deploy a new Foundry environment (prompts for model selection) |
| `build it <model>` | Deploy with a specific model |
| `weather <city>` | Get current weather for a city, phrased by an LLM but grounded in live weather data |
| `msft_docs <question>` | Search Microsoft Learn docs through a configured MCP server |
| `list builds` | List all active Foundry deployments |
| `build status <rg>` | Check a specific deployment |
| `teardown <rg>` | Remove a Foundry deployment |
| `heartbeat` | Bot health, uptime, memory, queue depth |
| `listener status` | Worker and queue status |
| `help` | Show command list |

## Files

| File | Purpose |
|------|---------|
| `src/app.py` | aiohttp host, M365 Agents SDK adapter, JWT middleware |
| `src/bot.py` | Teams message/event handlers, command routing |
| `src/command_parser.py` | Regex command parser |
| `src/conversation_store.py` | Azure Blob-backed conversation reference store |
| `src/job_dispatcher.py` | Azure Queue Storage job dispatcher |
| `src/models.py` | Command, job, and session models |
| `src/weather_service.py` | Live weather lookup plus grounded LLM narration fallback |
| `src/msft_docs_service.py` | Microsoft Learn MCP-backed docs lookup service |
| `src/worker.py` | Background queue worker (used in ACI container) |
| `src/worker_standalone.py` | Standalone entry point for the worker container |
| `src/proactive.py` | Proactive messaging via stored conversation references |
| `src/heartbeat.py` | Periodic heartbeat broadcast service |
| `src/storage_config.py` | Shared Azure credential and client configuration |
| `requirements.txt` | Python dependencies |

The bot web app sends a proactive heartbeat to stored conversations every 2 hours by default. This runs independently from the queue worker so heartbeat messages continue even when the bot container is configured with `WORKER_ENABLED=false` and Azure Container Instances handle job execution. Override this with `HEARTBEAT_INTERVAL_SECONDS` when you need a different cadence.

## Azure Resources

| Resource | Type | Purpose |
|----------|------|---------|
| `zolab-bot-ca-botprd-vnet` | Container App | Bot web server |
| `zolab-bot-env-botprd-vnet` | Container Apps Environment | VNet-backed hosting environment |
| `zolabbotacrbotprd` | Container Registry | Bot container images |
| `zolab-bot-mi-botprd` | Managed Identity (UAMI) | All Azure auth |
| `zolab-bot-botprd` | Bot Service (F0) | Teams channel registration |
| `zolab-worker-aci-botprd` | Container Instance | Worker for PowerShell jobs |
| `zolabworkeracrbotprd` | Container Registry | Worker container images |
| `zolabworkerst${SUFFIX}` | Storage Account | Queue (`botjobs`) + Blob (`botstate`), private access only |
| `zolab-worker-vnet-botprd` | Virtual Network | Shared private path for bot storage access and worker subnet integration |

### RBAC Roles (UAMI)

| Role | Scope |
|------|-------|
| AcrPull | Bot ACR |
| Contributor | Bot resource group |
| Storage Queue Data Contributor | Worker storage account |
| Storage Blob Data Contributor | Worker storage account |
| Contributor | Subscription (for PowerShell Az operations) |

## Run Locally

```bash
cd bot-app/runtime
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set environment variables
export SUFFIX="${SUFFIX:-botprd}"
export CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE="UserManagedIdentity"
export CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID="<bot-app-id>"
export CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID="<tenant-id>"
export TEAMS_APP_ID="<teams-app-manifest-id>"
# Required for queue/blob access:
export AZURE_STORAGE_ACCOUNT="zolabworkerst${SUFFIX}"
export AZURE_QUEUE_NAME="botjobs"
export AZURE_BLOB_CONTAINER="botstate"
export HEARTBEAT_INTERVAL_SECONDS="7200"
export MSFT_LEARN_MCP_URL="https://learn.microsoft.com/api/mcp"
export MSFT_LEARN_MCP_TIMEOUT_SECONDS="20"
export WEATHER_LLM_AZURE_OPENAI_ENDPOINT="https://<bot-owned-stable-endpoint>.cognitiveservices.azure.com/"
export WEATHER_LLM_MODEL="gpt-5.3-chat"
export WEATHER_LLM_API_VERSION="2024-10-21"

cd src
python app.py
```

The bot listens on `http://localhost:8000/api/messages`. `DefaultAzureCredential` falls through to `az login` locally.

When `WEATHER_LLM_AZURE_OPENAI_ENDPOINT` is set, the bot fetches live current conditions first and then asks the stable bot-owned `gpt-5.3-chat` deployment, or whatever `WEATHER_LLM_MODEL` overrides it with, to summarize only those supplied facts. This endpoint should belong to long-lived bot infrastructure, not to an ephemeral `build it` environment. If the model path is unavailable, the command falls back to the deterministic weather formatter.

## Deploy

```bash
# From repo root
bash bot-app/deployment/deploy-bot-app.sh
```

This builds the bot container image, deploys the bot-side Bicep infrastructure into the VNet-backed environment, grants RBAC, and verifies the deployment. The worker image is deployed separately from the repo-root `deployment/` folder. See `bot-app/deployment/deploy-bot-app.sh` for details.

For the full bot infrastructure walkthrough, resource inventory, and rollout notes, see [../deployment/README.md](../deployment/README.md).

## Teams App

The Teams app manifest is in `bot-app/teams-app/`. To sideload:

1. Zip the contents of `bot-app/teams-app/` (manifest.json + icons)
2. In Teams: Apps > Manage your apps > Upload a custom app
3. Select the zip file
