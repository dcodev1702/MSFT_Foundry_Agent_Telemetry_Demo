# 🤖 Microsoft Foundry — Agent Framework Observability PoC

Jupyter notebooks that configure and query Microsoft Foundry agents with **end-to-end observability** — tracing agent runs, tool invocations, and responses across Application Insights, Microsoft Foundry Traces, and Log Analytics. The Win11 notebook uses Azure AI Projects + the Responses API for execution and Agent Framework's resource helper for telemetry. It requires Python 3.14+; the separate macOS notebook supports Python 3.13+.

![Architecture overview of Foundry agent observability flow](https://github.com/user-attachments/assets/cbd172e9-b56e-4cf1-93a6-c48482eacd2a)

---

## 📋 Prerequisites

| Requirement | Details |
|---|---|
| **🏗️ AI Foundry Environment** | Deploy the infrastructure first — see [`deployment/README.md`](deployment/README.md) for full instructions |
| **Azure CLI** | Installed and authenticated (`az login`) — [Install Azure CLI](https://aka.ms/installazurecli) |
| **Entra ID Permissions** | `Contributor` (or equivalent) on the Foundry project and Application Insights resource |
| **Telemetry Read Access** | Log Analytics Reader (or equivalent query permissions) on the workspace linked to Application Insights; activate required PIM roles before the run |
| **Sentinel MCP** | Existing Foundry project connection, OAuth consent, and Sentinel/data-lake access for the signed-in identity; required to run every cell including Section 5.1 |
| **Microsoft Foundry Project** | Connected to an **Application Insights** instance backed by a **Log Analytics workspace** |
| **Model Deployment** | One allowed model (`gpt-4.1-mini`, `gpt-5.3`, `gpt-5.4`, or `grok-4-1-fast-reasoning`) is selected during deployment and auto-deployed — no manual setup needed |
| **Python** | Python 3.14+ for Win11 or Python 3.13+ for macOS, with `venv` support |
| **Jupyter Notebook** | VS Code with Jupyter extension or JupyterLab |

---

## 🚀 Quick Start

1. Run the deployment first — it generates `build_info-<suffix>.json` at the repo root (see [`deployment/README.md`](deployment/README.md))
2. Open `zolab-ai-agent-demo-macbook.ipynb` or `zolab-ai-agent-demo-win11.ipynb`
3. Run **Section 0** — creates `.venv` and registers the notebook kernel
4. On Win11, switch to the **AI Agent Demo (.venv, Python 3.14)** kernel
5. Run sections **1 → 5** in order
6. Run **Section 6** and inspect telemetry in the Azure Portal:
   - 📊 **Application Insights** — request/dependency traces
   - 🔍 **Microsoft Foundry** — agent execution traces
   - 📡 **Log Analytics** — `AppDependencies` table queries

On Windows, Section 1 installs [requirements-notebook.txt](requirements-notebook.txt) and runs `pip check`. If SDKs were already imported before updating, restart the kernel and rerun from the beginning. The matrix below applies to the Windows notebook only; the macOS notebook and standalone [Agent Framework SDK PoC](agent-framework-demo/README-agent-framework-sdk-poc.md) have separate setup instructions.

### Windows Dependency Matrix

Reviewed on **2026-09-14** using the configured package index. Versions can lag public PyPI; these are the resolved versions for this validation, not a promise of the newest release on every index.

| Package | Version | Role |
|---|---|---|
| `ipykernel` | `7.3.0` | Notebook kernel |
| `agent-framework-core` | `1.17.0` | OpenTelemetry resource helper |
| `agent-framework-openai` | `1.14.2` | Keeps the shared environment's provider compatible with OpenAI 3.x |
| `azure-ai-projects` | `2.6.0` | Foundry project agents, MCP definitions and Responses client |
| `openai` | `3.8.0` | Responses and conversations API; HTTPX2 transport |
| `httpx2` | `2.12.0` | Patched transport; replaces vulnerable 2.10.0 |
| `azure-identity` | `1.26.0b2` | Existing preview credential line retained |
| `azure-monitor-opentelemetry` | `1.8.10` | Azure Monitor exporter configuration |
| `azure-core-tracing-opentelemetry` | `1.0.0b13` | Azure SDK tracing bridge |
| `opentelemetry-instrumentation-httpx` | `0.65b0` | HTTPX 0.x instrumentation, not HTTPX2 |
| `opentelemetry-exporter-otlp-proto-grpc` | `1.44.0` | Aligns the shared environment's optional OTLP exporter with the SDK |

Azure Monitor resolves OpenTelemetry API/SDK **1.44.0** and Azure Monitor exporter **1.0.0b57**. The former `azure-ai-projects<2.5` restriction is removed because the updated Agent Framework provider accepts OpenAI 3.x. Do not independently upgrade the OpenTelemetry runtime or instrumentation train.

---

## 📓 Notebook Sections

After selecting the `AI Agent Demo (.venv)` kernel, run sections in order:

| # | Section | What It Does |
|---|---|---|
| **0** | Create or Reuse Virtual Environment | Validates Python 3.14 on Win11, creates `.venv`, installs `ipykernel`, and registers the Jupyter kernel |
| **1** | Install Dependencies | Installs the current validated Agent Framework, Foundry, Azure identity, and Azure Monitor/OpenTelemetry package matrix used by the notebook |
| **2** | Import Libraries | Verifies imports for `DefaultAzureCredential`, `AIProjectClient`, `MCPTool`, `PromptAgentDefinition`, and Agent Framework observability helpers |
| **3** | Configure Credentials and Clients | Reuses deployment values from `build_info-<suffix>.json`, resolves Azure auth, and configures the Foundry project client plus Responses API settings |
| **3.1** | Enable Telemetry | Configures Azure Monitor + OpenTelemetry, Foundry client-side tracing, HTTP dependency telemetry, and trace propagation controls |
| **3.2** | Configure MSFT Learn MCP Tool | Attaches the [Microsoft Learn MCP endpoint](https://learn.microsoft.com/api/mcp) directly to the Foundry project agent |
| **3.3** | Configure Microsoft Sentinel MCP Tool | Preserves the existing Foundry project-connection dependency for the Sentinel MCP tool |
| **4** | Create the Agent | Creates the main Foundry project agent and prepares the Sentinel-specific project agent when available |
| **5** | Query the Agent | Runs storytelling and Microsoft Learn grounded queries through the Responses API; saves results to `stories.json` and generates a Marp deck |
| **5.1** | Query Microsoft Sentinel | Keeps the Sentinel MCP dependency path and runs the Sentinel-specific interaction separately |
| **6** | Validate Telemetry | Resolves the linked workspace, flushes traces, waits for ingestion, and checks current-run interactions, dependencies, failures, service identity and version |

For bot and worker post-deploy validation, run [deployment/run-smoke-checks.sh](deployment/run-smoke-checks.sh) and then exercise the manual Teams smoke sequence from [deployment/OPERATIONS-RUNBOOK.md](deployment/OPERATIONS-RUNBOOK.md).

---

## 🔑 Key Configuration

### Build-Time Notebook Configuration

The deployment script writes a repo-local `build_info-<suffix>.json` file at build time. The notebooks read the latest matching file in the **Confirm Existing Deployment** section and reuse it in **Section 3** to populate:

- `foundry_proj_ep` → the Microsoft Foundry project endpoint
- `genai_model` → the model name used when creating the agent

This removes the need to hardcode the Foundry project endpoint in the notebook or store it in source control.

### Observability

Section **3.1** configures the notebook's observability path end to end:

- **Azure Monitor + Application Insights** receive exported OpenTelemetry traces.
- **Microsoft Foundry client-side tracing** is enabled for project-backed agent and Responses API activity.
- **Foundry instrumentation and explicit notebook-side client spans** make OpenAI 3.x / HTTPX2 calls visible in Application Insights and Log Analytics. The HTTPX instrumentor covers HTTPX 0.x clients only.
- **Trace context and baggage propagation** are enabled so notebook correlation identifiers flow with downstream requests.
- **GenAI semantic conventions** use the experimental profile supported by the installed SDKs. **Message content recording is off by default**, including custom prompt/completion attributes. Enable only for approved debugging; generated notebook outputs and Marp decks can still contain personal data.

See [observability.md](observability.md) for the full environment variable reference, version posture, and design notes.

### MCP Tool Setup

Section 3.2 creates an `azure.ai.projects.models.MCPTool` with server label `msft-learn` and the Microsoft Learn endpoint. The query cells explicitly handle MCP approval requests, with a bounded approval loop. Running these cells authorizes those tool calls; review the prompts and connected tools first.

The notebook keeps the Microsoft Sentinel MCP dependency on the Foundry project-connection path by design. That Sentinel-specific setup remains separate from the public MCP tool used for Microsoft Learn. Sentinel discovers the workspace/schema before querying, and its instructions require plain KQL without Markdown fences or backtick-quoted identifiers.

---

## 📊 Observability Flow

The notebook produces traces across three observability surfaces:

**Microsoft Foundry Traces (Preview)**

![Microsoft Foundry traces view for agent execution](https://github.com/user-attachments/assets/1b655116-fb69-429e-a1e5-13a12c6d070f)

**Microsoft Foundry Traces (Preview)**

![Application Insights telemetry view for traced operations](https://github.com/user-attachments/assets/37387f46-cc48-462a-9bb6-b42abc5f259d)

**Application Insights**

![Application Insights results for AppDependencies telemetry](https://github.com/user-attachments/assets/51c1a6ce-3216-49ed-9454-d6825e9076bc)

**Log Analytics - End-to-End Trace Correlation**

![End-to-end trace correlation view across observability tools](https://github.com/user-attachments/assets/15cc0aea-7af5-4b9c-9b4c-a8ec86f9df9c)

---

## 🏗️ Infrastructure

The `deployment/` directory contains Bicep IaC to provision the full AI Foundry environment — see [`deployment/README.md`](deployment/README.md) for details.

### Bot-The-Builder (Teams Bot)

The `bot-app/` directory contains **Bot-The-Builder**, a Teams bot that manages Foundry deployments via chat commands (`build it`, `list builds`, `build status <rg>`, `teardown`, `heartbeat`).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="./images/bot-overview-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="./images/bot-overview-light.svg">
  <img alt="Bot the Builder overview — Teams → Bot Service → Public Container App Ingress → VNet (Queue PE / Blob PE) → ACI Worker" src="./images/bot-overview-light.svg">
</picture>

- **Azure Container App** — public Teams ingress is preserved, but the app now runs in a custom VNet-backed Container Apps environment
- **ACI Worker** — runs inside the shared worker subnet and polls Queue Storage over the private network path
- **Azure Queue + Blob Storage** — RBAC-only (no shared keys), `publicNetworkAccess: Disabled`, reached through private endpoints and private DNS
- **Shared worker VNet** — hosts the Container Apps infrastructure subnet, the ACI subnet, and the storage private endpoint subnet
- **Cross-sub logging** — Container Apps logging still targets `DIBSecCom` LAW when that shared key is available to the deployer; otherwise deployment proceeds without explicit LAW wiring

See [`bot-app/runtime/README.md`](bot-app/runtime/README.md) for full bot documentation and [`deployment/README.md`](deployment/README.md) for the Teams command listener.

---

## 🔧 Troubleshooting

| Issue | Fix |
|---|---|
| Agent Framework or Azure Monitor import errors | Install the Windows matrix in **Section 1**, check `pip check`, then restart the kernel; the Agent Framework packages in this matrix are stable releases |
| Signed-in account shows as unavailable | Rerun **Section 3 — Configure the Project Client** (uses `az.cmd` on Windows) |
| Telemetry cell fails after dependency changes | Restart kernel, rerun from **Section 1** through **Section 3.1** |
| Sentinel cell cannot resolve a project connection | Verify the Foundry project still contains the Sentinel MCP connection and rerun **Section 3.3** |
| Sentinel `query_lake` returns KQL validation errors | Rerun Section 4 to update the specialist's schema-discovery and plain-KQL instructions, then Section 5.1; do not treat a tool error as a successful answer |
| Section 6 reports missing telemetry | Confirm workspace query permissions, exporter connectivity and the current run ID; the cell waits up to 3 minutes between ingestion checks and fails rather than accepting historical/empty results |

---

## ✅ Validation Checklist

- [ ] **Section 3** prints `🔐 Credential used: ...` and `👤 Signed-in account: ...`
- [ ] **Section 3.2** prints the [MSFT Learn MCP URL](https://learn.microsoft.com/api/mcp)
- [ ] **Section 3.3** resolves or prints the Sentinel MCP project connection details
- [ ] **Section 4** configures both Foundry project agents for a full run
- [ ] **Section 5** returns a response and appends to `stories.json`
- [ ] **Section 5.1** returns a Sentinel response when the Foundry project connection is available
- [ ] **Section 6** reports current-run story, facts and Sentinel coverage, response dependencies, zero failed spans, and the expected service/version

---

## 📚 References

- [Microsoft Agent Framework (Python)](https://github.com/microsoft/agent-framework)
- [Microsoft Agent Framework Observability Samples](https://github.com/microsoft/agent-framework/tree/main/python/samples/02-agents/observability)
- [Microsoft Foundry SDK Overview (Python)](https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/sdk-overview?pivots=programming-language-python#foundry-tools-sdks)
- [OpenTelemetry for Python: Instrumentation Guide](https://opentelemetry.io/docs/languages/python/instrumentation/)
- [Azure MCP Server Documentation](https://learn.microsoft.com/azure/developer/azure-mcp-server/)
- [Foundry client-side tracing (preview)](https://learn.microsoft.com/azure/foundry/observability/how-to/trace-agent-client-side)
- [Windows dependency matrix](requirements-notebook.txt)
- [Observability notes and validation evidence](observability.md)
- [Change history](CHANGELOG.md)
