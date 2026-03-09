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

1. Create an Entra app registration for the bot
2. Run `python setup_team.py` to create the Team and channel
3. Deploy `bot-infra.bicep` to provision App Service infrastructure
4. Deploy the bot code to the App Service
5. Configure the Bot Channel Registration in Azure to point to `https://<app-service>.azurewebsites.net/api/messages`

## Notes

- The heartbeat broadcasts automatically every 15 minutes to all known conversations
- Build/teardown commands execute `deployment/deploy-foundry-env.ps1` non-interactively via the `-SelectedAiModel` parameter
- For production, consider replacing the file-backed queue with Azure Storage Queue and the JSON conversation store with Table Storage or Cosmos DB
