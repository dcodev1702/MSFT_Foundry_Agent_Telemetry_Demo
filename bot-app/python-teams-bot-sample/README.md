# Foundry Teams Bot (M365 Agents SDK)

A Python Teams bot that manages Azure AI Foundry deployments from a Microsoft Teams channel. Built on the **Microsoft 365 Agents SDK** (`microsoft-agents-*` v0.8.0).

## Supported Commands

| Command | Description |
|---------|-------------|
| `build it` | Deploy with default model selection |
| `build it <model>` | Deploy with a specific model (e.g. `gpt-4.1-mini`) |
| `list builds` | List all active Foundry deployments |
| `build status <resource-group>` | Check a specific deployment |
| `teardown <resource-group>` | Remove a Foundry deployment |
| `heartbeat` | Check bot health (PID, memory, uptime, queue depth) |
| `listener status` | Check worker and queue status |
| `help` | Show available commands |

## Architecture

```text
┌──────────────────────┐       POST /api/messages       ┌───────────────────┐
│  Teams / Playground   │ ────────────────────────────▶  │   app.py          │
│  (chat UI)            │ ◀────────────────────────────  │   (aiohttp + SDK) │
└──────────────────────┘       JSON response             └─────────┬─────────┘
                                                                   │
                                         ┌─────────────────────────┼──────────────────┐
                                         │                         │                  │
                                   ┌─────▼─────┐           ┌──────▼──────┐    ┌──────▼──────┐
                                   │  bot.py    │           │  worker.py  │    │ heartbeat.py│
                                   │  handlers  │           │  job exec   │    │ 15-min ping │
                                   └─────┬──────┘           └──────┬──────┘    └─────────────┘
                                         │                         │
                                   ┌─────▼──────┐          ┌──────▼──────────┐
                                   │ .queue/     │          │ deploy-foundry- │
                                   │ pending/    │          │ env.ps1         │
                                   └─────────────┘          └─────────────────┘
```

## Files

| File | Purpose |
|------|---------|
| `app.py` | Entry point — aiohttp server, CloudAdapter, AgentApplication, lifecycle hooks |
| `bot.py` | Message and event handler registration (decorator pattern) |
| `command_parser.py` | Regex-based command parser |
| `conversation_store.py` | JSON-file conversation metadata + reference persistence |
| `proactive.py` | Proactive messaging via stored ConversationReference |
| `worker.py` | Background job processor — executes PowerShell, sends progress updates |
| `heartbeat.py` | Automatic 15-minute health broadcast (PID, memory, uptime) |
| `job_dispatcher.py` | File-backed queue writer |
| `models.py` | FoundryCommand and QueuedJob dataclasses |
| `graph_setup.py` | Microsoft Graph helpers for Team + channel creation |
| `setup_team.py` | CLI tool to bootstrap the Team and channel |
| `.env.example` | Environment variable template |
| `requirements.txt` | Python dependencies |

## Local Development Setup

### Prerequisites

- **Python 3.10+** (3.11+ recommended)
- **Node.js** (for the Agents Playground test client)
- **PowerShell Core** (`pwsh`) — only needed if testing build/teardown commands

### 1. Create virtual environment and install dependencies

```bash
cd bot-app/python-teams-bot-sample
python3 -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\Activate.ps1     # Windows PowerShell
pip install -r requirements.txt
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

For **anonymous local testing** (no Entra app registration needed):

```env
# .env
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED=True
PORT=3978
```

For **authenticated testing** (with an Entra app registration):

```env
# .env
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID=<your-bot-app-client-id>
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET=<your-bot-app-secret>
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID=<your-entra-tenant-id>
PORT=3978
```

### 3. Start the bot

```bash
source .venv/bin/activate
python app.py
```

You should see:

```
2026-03-09 [INFO] __main__: Starting Foundry Teams Bot on port 3978 …
2026-03-09 [INFO] worker: BackgroundWorker started — polling …
2026-03-09 [INFO] heartbeat: HeartbeatService started — interval: 900s
```

### 4. Install and run the Agents Playground (test client)

In a **separate terminal**:

```bash
npm install -g @microsoft/m365agentsplayground
agentsplayground -e "http://localhost:3978/api/messages" -c "emulator"
```

This opens a Teams-like chat UI in your browser connected to your local bot. Try:

- `help` — see all commands
- `heartbeat` — live bot health metrics
- `listener status` — worker and queue info
- `build it gpt-4.1-mini` — queue a build job

### Quick Start (copy-paste)

```bash
# Terminal 1 — start the bot
cd bot-app/python-teams-bot-sample
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set ANONYMOUS_ALLOWED=True for local testing
python app.py

# Terminal 2 — start the test client
npm install -g @microsoft/m365agentsplayground
agentsplayground -e "http://localhost:3978/api/messages" -c "emulator"
```

## Queue Handoff

When the bot receives `build it`, `teardown`, `list builds`, or `build status`, it writes a JSON job file into `.queue/pending/`. The background worker (`worker.py`) picks it up automatically, moves it to `.queue/running/`, executes PowerShell, and moves it to `.queue/completed/` or `.queue/failed/`.

```text
.queue/
├── pending/     ← bot writes jobs here
├── running/     ← worker moves jobs here during execution
├── completed/   ← successful jobs land here
└── failed/      ← failed jobs land here
```

## Production Deployment

For deploying to Azure App Service with Managed Identity, see `bot-app/deployment/bot-infra.bicep`. Key steps:

1. Deploy `bot-infra.bicep` to provision App Service + User-Assigned Managed Identity + Azure Bot resource
2. Run `python setup_team.py` to create the Team and channel
3. Deploy the Docker container to the App Service
4. The Azure Bot resource is automatically configured to point to `https://<app-service>.azurewebsites.net/api/messages`

> **Note:** The Azure Bot resource uses a **User-Assigned Managed Identity** (no Entra app registration or client secrets required). This is the recommended identity type for new bots — multi-tenant app registration is deprecated for new bots after July 2025.

## Notes

- The heartbeat broadcasts automatically every 15 minutes to all known conversations
- Build/teardown commands execute `deployment/deploy-foundry-env.ps1` non-interactively via the `-SelectedAiModel` parameter
- For production, consider replacing the file-backed queue with Azure Storage Queue and the JSON conversation store with Table Storage or Cosmos DB

---

## Production Architecture

```text
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              MICROSOFT TEAMS                                        │
│                                                                                     │
│   ┌───────────────────────────────────────────────────┐                             │
│   │  Team: "Microsoft Foundry Deployments"            │                             │
│   │  ┌─────────────────────────────────────────────┐  │    Users type commands:     │
│   │  │  Channel: "bot-the-builder"                 │  │    • build it gpt-4.1-mini  │
│   │  │                                             │  │    • teardown               │
│   │  │  👤 User: build it gpt-4.1-mini             │  │    • list builds            │
│   │  │  🤖 Bot:  ✅ Build queued (job abc-123)     │  │    • heartbeat              │
│   │  │  🤖 Bot:  🚧 Bob's are still building! 🚧  │  │    • help                   │
│   │  │  🤖 Bot:  ✅ Build completed!               │  │                             │
│   │  │  🤖 Bot:  💓 Heartbeat (every 15 min)       │  │                             │
│   │  └─────────────────────────────────────────────┘  │                             │
│   └───────────────────────────────────────────────────┘                             │
└──────────────────────────────┬──────────────────────────────────────────────────────┘
                               │
                               │ Teams Protocol
                               ▼
┌──────────────────────────────────────────────────────┐
│           AZURE BOT SERVICE (Channel Registration)   │
│                                                      │
│  • Bot Handle: "bot-the-builder"                     │
│  • Messaging Endpoint:                               │
│    https://<app>.azurewebsites.net/api/messages      │
│  • Channels: Microsoft Teams                         │
│  • Identity: User-Assigned Managed Identity          │
└──────────────┬───────────────────────┬───────────────┘
               │                       ▲
               │ POST /api/messages    │ Bot Framework REST API
               │ (inbound activities)  │ (outbound replies &
               ▼                       │  proactive messages)
┌──────────────────────────────────────────────────────────────────────────────────────┐
│                     AZURE APP SERVICE (Linux, Container)                              │
│                                                                                      │
│  ┌────────────────────────────────────────────────────────────────────────────────┐  │
│  │                        DOCKER CONTAINER                                        │  │
│  │                                                                                │  │
│  │  ┌─ Python 3.11 Runtime ─────────────────────────────────────────────────────┐ │  │
│  │  │                                                                           │ │  │
│  │  │  ┌─ aiohttp Web Server (port 3978) ────────────────────────────────────┐  │ │  │
│  │  │  │  POST /api/messages ─► jwt_authorization_middleware ─► agent_app     │  │ │  │
│  │  │  │  GET  /api/messages ─► health_check                                 │  │ │  │
│  │  │  └─────────────────────────────────────────────────────────────────────┘  │ │  │
│  │  │                                                                           │ │  │
│  │  │  ┌─ M365 Agents SDK ──────────────────────────────────────────────────┐   │ │  │
│  │  │  │  CloudAdapter          ─ HTTP ↔ Activity translation               │   │ │  │
│  │  │  │  MsalConnectionManager ─ Token acquisition (via Managed Identity)  │   │ │  │
│  │  │  │  AgentApplication      ─ Decorator-based handler routing           │   │ │  │
│  │  │  │  Authorization         ─ JWT validation of inbound requests        │   │ │  │
│  │  │  └────────────────────────────────────────────────────────────────────┘   │ │  │
│  │  │                                                                           │ │  │
│  │  │  ┌─ Bot Logic (bot.py) ───────────────────────────────────────────────┐   │ │  │
│  │  │  │  Command Parser     ─ build it / teardown / list builds / etc.     │   │ │  │
│  │  │  │  Teardown Sessions  ─ Multi-turn interactive teardown flow         │   │ │  │
│  │  │  │  Conversation Store ─ Persists conversation refs (.state/)         │   │ │  │
│  │  │  │  Job Dispatcher     ─ Writes job JSON to .queue/pending/           │   │ │  │
│  │  │  └────────────────────────────────────────────────────────────────────┘   │ │  │
│  │  │                                                                           │ │  │
│  │  │  ┌─ Background Services (asyncio tasks) ──────────────────────────────┐   │ │  │
│  │  │  │                                                                    │   │ │  │
│  │  │  │  BackgroundWorker (worker.py)          HeartbeatService            │   │ │  │
│  │  │  │  • Polls .queue/pending/ (5s)          (heartbeat.py)              │   │ │  │
│  │  │  │  • Moves → running → completed/failed  • Broadcasts every 15 min  │   │ │  │
│  │  │  │  • Spawns pwsh subprocesses             • PID, uptime, memory,     │   │ │  │
│  │  │  │  • Sends progress updates (60s)           queue depth              │   │ │  │
│  │  │  │  • Posts final results                                             │   │ │  │
│  │  │  └───────────────┬────────────────────────────────────────────────────┘   │ │  │
│  │  │                  │                                                        │ │  │
│  │  │  ┌─ Proactive Messenger (proactive.py) ───────────────────────────────┐   │ │  │
│  │  │  │  • Reconstructs ConversationReference from store                   │   │ │  │
│  │  │  │  • Calls adapter.continue_conversation() ──► Bot Service ──► Teams │   │ │  │
│  │  │  └────────────────────────────────────────────────────────────────────┘   │ │  │
│  │  │                                                                           │ │  │
│  │  │  ┌─ Graph Setup (graph_setup.py) — one-time bootstrap ────────────────┐   │ │  │
│  │  │  │  • Creates Team + Channel via Python msgraph-sdk                   │   │ │  │
│  │  │  │  • Installs bot app in Team                                        │   │ │  │
│  │  │  └────────────────────────────────────────────────────────────────────┘   │ │  │
│  │  └───────────────────────────────────────────────────────────────────────────┘ │  │
│  │                                                                                │  │
│  │  ┌─ PowerShell Core (pwsh 7.x) ───────────────────────────────────────────┐   │  │
│  │  │                                                                         │   │  │
│  │  │  deploy-foundry-env.ps1                                                 │   │  │
│  │  │  ├── -SelectedAiModel "gpt-4.1-mini"    → Bicep deployment (build)     │   │  │
│  │  │  ├── -Cleanup -CleanupResourceGroup <rg> → Resource teardown            │   │  │
│  │  │  ├── -ListBuilds                         → Enumerate active builds      │   │  │
│  │  │  └── -BuildStatusResourceGroup <rg>      → Check build status           │   │  │
│  │  │                                                                         │   │  │
│  │  │  Installed Modules:                                                     │   │  │
│  │  │  ├── Az.Accounts, Az.Resources, Az.KeyVault, Az.OperationalInsights     │   │  │
│  │  │  ├── Microsoft.Graph.Authentication                                     │   │  │
│  │  │  ├── Microsoft.Graph.Groups                                             │   │  │
│  │  │  └── Microsoft.Graph.Users                                              │   │  │
│  │  └─────────────────────────────────────────────────────────────────────────┘   │  │
│  │                                                                                │  │
│  │  ┌─ Local State (persistent volume recommended) ───────────────────────────┐   │  │
│  │  │  .queue/pending/    ─ Jobs waiting to execute                           │   │  │
│  │  │  .queue/running/    ─ Job currently in progress                         │   │  │
│  │  │  .queue/completed/  ─ Finished jobs (with results)                      │   │  │
│  │  │  .queue/failed/     ─ Failed jobs (with error)                          │   │  │
│  │  │  .state/conversations.json ─ Conversation refs for proactive messaging  │   │  │
│  │  └─────────────────────────────────────────────────────────────────────────┘   │  │
│  └────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                      │
│  User-Assigned Managed Identity ◄────────────────────────────────────────────────┐   │
│  (shared by App Service + Azure Bot Service)                                     │   │
└───────┬──────────────────────┬──────────────────────────────────────┬─────────────┘   │
        │                      │                                      │                 │
        │ Az PowerShell        │ Graph PowerShell                     │ Python          │
        │ (Managed Identity)   │ (Managed Identity)                   │ msgraph-sdk     │
        ▼                      ▼                                      ▼                 │
┌───────────────────┐  ┌──────────────────────┐  ┌─────────────────────────────┐       │
│  AZURE RESOURCE   │  │   MICROSOFT GRAPH    │  │     MICROSOFT GRAPH         │       │
│  MANAGER          │  │   (Entra ID)         │  │     (Teams)                 │       │
│                   │  │                      │  │                             │       │
│  • Bicep deploy   │  │  • Add/remove user   │  │  • Create Team              │       │
│    (zolab-ai-*)   │  │    from zolab-ai-dev │  │  • Create Channel           │       │
│  • RBAC assign/   │  │    Entra group       │  │  • Install bot app          │       │
│    remove         │  │  • Resolve user IDs  │  │    (one-time setup)         │       │
│  • RG create/     │  │                      │  │                             │       │
│    delete         │  │                      │  │                             │       │
│  • Key Vault,     │  │                      │  │                             │       │
│    Storage, AI    │  │                      │  │                             │       │
└───────────────────┘  └──────────────────────┘  └─────────────────────────────┘       │
```

### Request Flow (e.g. `build it gpt-4.1-mini`)

```text
 User types          Teams delivers        App Service             Worker picks up
 in channel          to Bot Service        processes activity      queued job
     │                    │                      │                      │
     ▼                    ▼                      ▼                      ▼
 ┌────────┐  ──►  ┌────────────┐  ──►  ┌──────────────┐  ──►  ┌──────────────┐
 │ Teams  │       │ Azure Bot  │       │  bot.py:      │       │  worker.py:  │
 │ "build │       │ Service    │       │  parse cmd    │       │  poll .queue │
 │  it    │       │ forwards   │       │  validate     │       │  spawn pwsh  │
 │  gpt-  │       │ POST to    │       │  queue job    │       │  deploy.ps1  │
 │  4.1-  │       │ /api/      │       │  reply "✅    │       │  -Selected   │
 │  mini" │       │ messages   │       │   queued"     │       │  AiModel     │
 └────────┘       └────────────┘       └──────┬───────┘       └──────┬───────┘
                                              │                      │
                                              ▼                      │
                                     .queue/pending/                 │
                                     job-abc-123.json                │
                                                                     │
                          ┌──────────────────────────────────────────┘
                          │
                          ▼  (every 60s while running)
                  ┌──────────────┐        ┌────────────┐       ┌────────┐
                  │ proactive.py │  ──►   │ Azure Bot  │  ──►  │ Teams  │
                  │ send progress│        │ Service    │       │ "🚧    │
                  │ send result  │        │ delivers   │       │ still  │
                  └──────────────┘        └────────────┘       │ build- │
                                                               │ ing!"  │
                          ▼  (on completion)                   └────────┘
                  ┌──────────────┐        ┌────────────┐       ┌────────┐
                  │ proactive.py │  ──►   │ Azure Bot  │  ──►  │ Teams  │
                  │ final result │        │ Service    │       │ "✅    │
                  └──────────────┘        └────────────┘       │ done!" │
                                                               └────────┘
```

### Identity Model (Zero Secrets)

```text
┌─────────────────────────────────────────────────────────────────────┐
│                USER-ASSIGNED MANAGED IDENTITY                       │
│                (single identity, zero secrets)                      │
├─────────────────────────────────┬───────────────────────────────────┤
│                                 │                                   │
│  Bot Protocol                   │  Azure / Graph Operations         │
│  (Teams ↔ App Service)          │  (infra + Entra management)       │
│                                 │                                   │
│  USED BY:                       │  USED BY:                         │
│  • Azure Bot Service resource   │  • pwsh Az module (deployments,   │
│  • MsalConnectionManager       │    RBAC, resource management)     │
│  • CloudAdapter                 │  • pwsh Graph modules (Entra      │
│  • JWT validation of inbound    │    group membership, user ops)    │
│    requests from Bot Service    │  • Python msgraph-sdk (Team +     │
│  • Outbound replies & proactive │    channel creation, bot install) │
│    messages to Bot Service      │                                   │
│                                 │                                   │
│  AUTH: Client ID of the         │  AUTH: Automatic — Azure injects  │
│        Managed Identity         │        token via IMDS endpoint    │
│                                 │                                   │
│  SCOPE: Bot Framework / Teams   │  SCOPE: Azure RM + Microsoft     │
│         protocol only           │         Graph API                 │
└─────────────────────────────────┴───────────────────────────────────┘

No Entra App Registration required.
No client secrets to rotate.
Multi-tenant bot registration deprecated after July 2025.
```

### Docker Container Contents

```text
┌─────────────────────────────────────────────────┐
│  Docker Image                                   │
│                                                 │
│  Base: python:3.11-slim                         │
│                                                 │
│  + PowerShell Core 7.x (pwsh)                   │
│  + Az PowerShell Modules:                       │
│    ├── Az.Accounts                              │
│    ├── Az.Resources                             │
│    ├── Az.KeyVault                              │
│    └── Az.OperationalInsights                   │
│  + Microsoft.Graph PowerShell Modules:          │
│    ├── Microsoft.Graph.Authentication           │
│    ├── Microsoft.Graph.Groups                   │
│    └── Microsoft.Graph.Users                    │
│  + Python packages (requirements.txt):          │
│    ├── aiohttp, python-dotenv, psutil           │
│    ├── microsoft-agents-*  (M365 Agents SDK)    │
│    ├── msgraph-sdk, azure-identity              │
│    └── msal                                     │
│  + Bot application code                         │
│  + deploy-foundry-env.ps1                       │
│                                                 │
│  NOT included (not needed):                     │
│    ✗ Microsoft.Graph.Teams (pwsh)               │
│      (Teams ops handled by Python msgraph-sdk)  │
└─────────────────────────────────────────────────┘
```
