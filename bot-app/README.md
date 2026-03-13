# 🤖 Bot-The-Builder — Teams Bot Workspace

The `bot-app/` workspace contains the Teams-facing automation layer for this repo: a Microsoft Teams bot built on the M365 Agents SDK, the bot-specific Azure deployment assets, the Teams app manifest, and the Python worker/runtime that brokers Foundry build and teardown operations.

This is the chat-first control plane for the Foundry environment described in [../deployment/README.md](../deployment/README.md) and the notebook flow described in [../README.md](../README.md).

---

## 📋 What Lives Here

| Path | Purpose |
|---|---|
| `Dockerfile` | Bot container image definition for Azure Container Apps |
| `deployment/` | Bot infrastructure Bicep, deploy script, and Container Apps resources |
| `docs/` | Implementation notes and bot automation design guidance |
| `runtime/` | Bot runtime, worker runtime, queue dispatch, storage, and proactive messaging code |
| `teams-app/` | Teams app manifest and icons for sideloading |

---

## 🏗️ Runtime Architecture

```text
Teams
  |
  v
Azure Bot Service (UserAssignedMSI, F0)
  |
  v
Azure Container App (public ingress)
  |
  v
Container Apps Environment on delegated subnet (`zolab-bot-env-botprd-vnet`)
  |\
  | \__ Private Queue endpoint (`botjobs`)
  |     |
  |     v
  |   Azure Container Instance worker on delegated subnet
  |
  \___ Private Blob endpoint (`botstate`)
```

### Key Responsibilities

| Component | Responsibility |
|---|---|
| Teams bot | Accepts chat commands, validates intent, prompts for confirmation, sends proactive updates |
| Bot Container App | Hosts the aiohttp/M365 Agents SDK web app and handles Teams traffic through public ingress on a VNet-backed environment |
| Queue worker | Executes long-running PowerShell and Bicep operations outside the request path from a subnet-integrated ACI |
| Blob state store | Persists conversation references and identities for proactive replies over private endpoints |
| Shared UAMI | Handles Azure auth for ACR pulls, Storage access, and automation operations |
| Shared worker VNet | Carries the Container Apps infrastructure subnet, worker subnet, and storage private endpoint subnet |

### A365 Agents SDK vs Bot Framework Imports

This bot is built on the Microsoft 365 Agents SDK, not the older Bot Framework SDK. The practical difference in Python is that imports move from the `botbuilder.*` namespace to the `microsoft_agents.*` namespace, and the app model becomes more host-oriented and decorator-based.

Typical Bot Framework style imports looked like:

```python
from botbuilder.core import BotFrameworkAdapter, TurnContext, MemoryStorage, MessageFactory
from botbuilder.schema import Activity
```

In this repo, the A365 Agents SDK imports look like:

```python
from microsoft_agents.activity import load_configuration_from_env, Activity, ConversationReference
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter, start_agent_process
from microsoft_agents.hosting.core import AgentApplication, Authorization, MemoryStorage, TurnContext, MessageFactory
```

What changed conceptually:

- Bot Framework centered on `BotFrameworkAdapter` plus `ActivityHandler`-style classes and the `botbuilder.core` / `botbuilder.schema` packages.
- A365 Agents SDK centers on `CloudAdapter`, `AgentApplication`, and the `microsoft_agents.hosting.*` packages.
- Authentication wiring is more explicit with `MsalConnectionManager` plus `Authorization`, instead of just adapter settings and app credentials.
- The aiohttp host integration is first-class: `start_agent_process(...)` handles inbound activity processing for the local and deployed web app.
- Proactive messaging also moved namespaces; in this repo it uses `ConversationReference`, `TurnContext`, and `ClaimsIdentity` from the `microsoft_agents` packages.

Concrete examples in this repo:

- Host setup and core imports: [runtime/src/app.py](runtime/src/app.py)
- Decorator-based message registration on `AgentApplication`: [runtime/src/bot.py](runtime/src/bot.py)
- Proactive continuation pattern with A365 imports: [runtime/src/proactive.py](runtime/src/proactive.py)
- Full SDK import reference: [docs/m365-agents-sdk-imports.md](docs/m365-agents-sdk-imports.md)
- One-page quick reference: [docs/m365-agents-sdk-cheat-sheet.md](docs/m365-agents-sdk-cheat-sheet.md)

---

## 💬 Supported Bot Commands

The user-facing command surface includes:

- `build it`
- `build it <model>`
- `list builds`
- `build status <resource-group>`
- `teardown`
- `teardown <resource-group>`
- `heartbeat`
- `listener status`
- `help`

The bot and worker emit periodic progress updates during active operations. The current build progress text is:

`🚧 👷 The Bobs Are Still Building 👷🚧 `

The current teardown progress text is:

`🚧 Pls hold while we teardown: <resource-group> 🚧`

---

## 📂 Folder Layout

```text
bot-app/
├── Dockerfile
├── README.md
├── deployment/
│   ├── bot-infra.bicep
│   ├── bot-infra.bicepparam
│   ├── deploy-bot-app.sh
│   └── modules/
│       └── bot-resources.bicep
├── docs/
│   ├── m365-agents-sdk-cheat-sheet.md
│   └── m365-agents-sdk-imports.md
├── runtime/
│   ├── src/
│   │   ├── app.py
│   │   ├── bot.py
│   │   ├── worker.py
│   │   ├── worker_standalone.py
│   │   ├── proactive.py
│   │   ├── heartbeat.py
│   │   ├── job_dispatcher.py
│   │   ├── command_parser.py
│   │   ├── conversation_store.py
│   │   ├── storage_config.py
│   │   └── models.py
│   ├── tests/
│   ├── requirements.txt
│   └── README.md
└── teams-app/
    └── manifest.json
```

---

## ▶️ Quick Start

### Run the bot locally

```bash
cd bot-app/runtime
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd src
python3 app.py
```

See [runtime/README.md](runtime/README.md) for the full local environment variable set and runtime details.

### Test local bot flow with Agents Playground

Install the Microsoft 365 Agents Playground once:

```bash
npm install -g @microsoft/m365agentsplayground
```

Then run the local bot server and connect the Playground to it:

```bash
cd bot-app/runtime
source .venv/bin/activate
cd src
python3 app.py
```

In a second terminal:

```bash
agentsplayground
```

How it works:

- `python3 app.py` starts the local aiohttp-based agent server from [runtime/src/app.py](runtime/src/app.py).
- The server loads the same bot handlers used in Azure and waits for inbound activities on the local `/api/messages` endpoint.
- `agentsplayground` acts as the local test client, sending activities to your running bot so you can exercise commands without redeploying the Container App.
- This is for local bot-loop testing only; the ACI worker, Azure Storage, and Teams channel integration still depend on the configured Azure resources and environment variables.

### Deploy the bot infrastructure

```bash
cd /path/to/repo
bash bot-app/deployment/deploy-bot-app.sh
```

See [deployment/README.md](deployment/README.md) for the bot infrastructure deployment flow, prerequisites, and rollout details.

### Sideload the Teams app

Zip the contents of `bot-app/teams-app/` and upload that package in Teams as a custom app.

If Teams keeps showing a stale custom app package after uninstalling it in the client, run `pwsh ./deployment/remove-teams-app.ps1` from the repo root to remove the current-user installation and the tenant app-catalog entry for this bot's manifest ID.

---

## 🔗 Related Docs

| Document | Use it for |
|---|---|
| [runtime/README.md](runtime/README.md) | Bot runtime, worker runtime, local dev, and command behavior |
| [deployment/README.md](deployment/README.md) | Bot Azure infrastructure, ACR build, Container App deployment, and RBAC |
| [docs/m365-agents-sdk-imports.md](docs/m365-agents-sdk-imports.md) | Detailed explanation of the `microsoft_agents.*` imports used in this repo |
| [docs/m365-agents-sdk-cheat-sheet.md](docs/m365-agents-sdk-cheat-sheet.md) | One-page quick reference for the bot's M365 Agents SDK surface |
| [../deployment/README.md](../deployment/README.md) | Foundry environment deployment and cleanup automation |

---

## 📝 Notes

- The bot infrastructure and the Foundry environment deployment are separate concerns. The bot lives under `bot-app/`; the Foundry environment lives under `deployment/` at the repo root.
- The bot Container App and the worker container are intentionally split so long-running PowerShell work does not block Teams request handling.
- The bot now defaults to the VNet-backed app and environment names `zolab-bot-ca-botprd-vnet` and `zolab-bot-env-botprd-vnet` so public Teams ingress stays up while storage access stays private.
- The deploy script in `bot-app/deployment/` performs a local Docker `--no-cache --pull` rebuild, pushes the bot image to the bot ACR, and redeploys the bot infrastructure into that VNet-backed environment by default.
- The worker image is defined in [../deployment/Dockerfile.worker](../deployment/Dockerfile.worker) because it shares the same PowerShell/Bicep automation code used by the root deployment flow. Use [../deployment/deploy-worker-app.sh](../deployment/deploy-worker-app.sh) for normal worker rollouts, and [../deployment/deploy-private-storage-rollout.sh](../deployment/deploy-private-storage-rollout.sh) for the staged bot+worker private-storage migration flow.