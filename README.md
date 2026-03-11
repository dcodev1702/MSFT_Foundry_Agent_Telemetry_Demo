# 🤖 Microsoft Foundry — AI Agent Observability PoC

A Jupyter Notebook (Python 3.13) that creates and queries a Microsoft Foundry AI agent with **end-to-end observability** — tracing agent creation, tool invocations, and responses across Application Insights, Microsoft Foundry Traces, and Log Analytics.

![Architecture overview of Foundry agent observability flow](https://github.com/user-attachments/assets/cbd172e9-b56e-4cf1-93a6-c48482eacd2a)

---

## 📋 Prerequisites

| Requirement | Details |
|---|---|
| **🏗️ AI Foundry Environment** | Deploy the infrastructure first — see [`deployment/README.md`](deployment/README.md) for full instructions |
| **Azure CLI** | Installed and authenticated (`az login`) — [Install Azure CLI](https://aka.ms/installazurecli) |
| **Entra ID Permissions** | `Contributor` (or equivalent) on the Foundry project and Application Insights resource |
| **Microsoft Foundry Project** | Connected to an **Application Insights** instance backed by a **Log Analytics workspace** |
| **Model Deployment** | One allowed model (`gpt-4.1-mini`, `gpt-5.3`, `gpt-5.4`, or `grok-4-1-fast-reasoning`) is selected during deployment and auto-deployed — no manual setup needed |
| **Python 3.13+** | With `venv` support |
| **Jupyter Notebook** | VS Code with Jupyter extension or JupyterLab |

---

## 🚀 Quick Start

1. Run the deployment first — it generates `build_info-<suffix>.json` at the repo root (see [`deployment/README.md`](deployment/README.md))
2. Open `zolab-ai-agent-demo-win11.ipynb`
4. Run **Section 0** — creates `.venv` and registers the `AI Agent Demo (.venv)` kernel
5. Switch to the **AI Agent Demo (.venv)** kernel
6. Run sections **1 → 6** in order
7. Observe telemetry in the Azure Portal:
   - 📊 **Application Insights** — request/dependency traces
   - 🔍 **Microsoft Foundry** — agent execution traces
   - 📡 **Log Analytics** — `AppDependencies` table queries

---

## 📓 Notebook Sections

After selecting the `AI Agent Demo (.venv)` kernel, run sections in order:

| # | Section | What It Does |
|---|---|---|
| **0** | Create or Reuse Virtual Environment | Creates `.venv`, installs `ipykernel`, registers Jupyter kernel |
| **1** | Install Dependencies | Installs Azure AI, OpenTelemetry, and Azure Monitor packages with compatibility safeguards |
| **1.1** | Confirm Existing Deployment | Loads the latest `build_info-<suffix>.json` and prints the current infrastructure summary before SDK imports |
| **2** | Import Libraries | Verifies imports for `DefaultAzureCredential`, `AIProjectClient`, `PromptAgentDefinition`, `AIProjectInstrumentor` |
| **3** | Configure the Project Client | Reuses the deployment values loaded from `build_info-<suffix>.json`, then tries `DefaultAzureCredential` with CLI fallback if needed |
| **3.1** | Enable Telemetry | Configures OpenTelemetry + Azure Monitor tracing pipeline and instruments the SDK |
| **3.2** | Configure MSFT Learn MCP Tool | Sets up the [Microsoft Learn MCP endpoint](https://learn.microsoft.com/api/mcp) as a tool for the agent |
| **4** | Create the Agent | Defines a versioned agent with storytelling persona and MCP tool access |
| **5** | Query the Agent | Two passes — fiction story generation + MCP-powered Foundry guidance; results saved to `stories.json` |
| **6** | Validate Traces in Log Analytics | Runs KQL queries against `api.loganalytics.io` to verify end-to-end telemetry |

---

## 🔑 Key Configuration

### Build-Time Notebook Configuration

The deployment script writes a repo-local `build_info-<suffix>.json` file at build time. The Win11 notebook reads the latest matching file in the **Confirm Existing Deployment** section and reuses it in **Section 3** to populate:

- `foundry_proj_ep` → the Microsoft Foundry project endpoint
- `genai_model` → the model name used when creating the agent

This removes the need to hardcode the Foundry project endpoint in the notebook or store it in source control.

### Telemetry Environment Variables

These **must** be set before calling `instrument()`:

```python
from uuid import uuid4

from azure.core.settings import settings
from opentelemetry import baggage, context as otel_context, trace
from opentelemetry.sdk.resources import Resource
from azure.monitor.opentelemetry import configure_azure_monitor

settings.tracing_implementation = "opentelemetry"

# ---------------------------------------------------------------------------
# Phase 1: Trace settings (must be set before instrumentation)
# ---------------------------------------------------------------------------
os.environ["AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING"] = "true"
os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "gen_ai_latest_experimental"
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"
os.environ["AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED"] = "true"
os.environ["AZURE_TRACING_GEN_AI_ENABLE_TRACE_CONTEXT_PROPAGATION"] = "true"
os.environ["AZURE_TRACING_GEN_AI_TRACE_CONTEXT_PROPAGATION_INCLUDE_BAGGAGE"] = "true"

project_name = foundry_proj_ep.rstrip("/").split("/")[-1] if "foundry_proj_ep" in globals() else "unknown-project"
os.environ["OTEL_SERVICE_NAME"] = f"foundry-ai-agent-1702"
os.environ["OTEL_TRACES_SAMPLER"] = "microsoft.fixed_percentage"
os.environ["OTEL_TRACES_SAMPLER_ARG"] = "1.0"

if "telemetry_session_id" not in globals():
    telemetry_session_id = str(uuid4())

# -----------------------------------------------------------------------------
# Phase 2: Backend setup (Microsoft Foundry -> Application Insights connection)
# -----------------------------------------------------------------------------
application_insights_connection_string = project_client.telemetry.get_application_insights_connection_string()

resource = Resource.create(
    {
        "service.name": os.environ["OTEL_SERVICE_NAME"],
        "service.namespace": "foundry-agent-1702",
        "service.instance.id": telemetry_session_id,
        "deployment.environment": "demo",
        "foundry.project.name": project_name,
    }
)

# Configure Azure Monitor as the tracing backend.
configure_azure_monitor(
    connection_string=application_insights_connection_string,
    resource=resource,
    sampling_ratio=1.0,
)

# -----------------------------------------------------------------------------
# Phase 3: SDK instrumentation
# -----------------------------------------------------------------------------
AIProjectInstrumentor().instrument(enable_content_recording=True)

# -----------------------------------------------------------------------------
# Phase 4: Tracer handle for custom spans in later sections
# -----------------------------------------------------------------------------
tracer = trace.get_tracer(__name__)
```

### MCP Tool Setup

```python
from azure.ai.projects.models import MCPTool

mcp_tool_spec = MCPTool(
    server_label="msft-learn",
    server_url="https://learn.microsoft.com/api/mcp",
)
```

---

## 📊 Observability Flow

The notebook produces traces across three observability surfaces:

**Microsoft Foundry Traces (Preview)**

![Microsoft Foundry traces view for agent execution](https://github.com/user-attachments/assets/1b655116-fb69-429e-a1e5-13a12c6d070f)

**Application Insights**

![Application Insights telemetry view for traced operations](https://github.com/user-attachments/assets/37387f46-cc48-462a-9bb6-b42abc5f259d)

**Application Insights**

![Application Insights results for AppDependencies telemetry](https://github.com/user-attachments/assets/51c1a6ce-3216-49ed-9454-d6825e9076bc)

**Log Analytics - End-to-End Trace Correlation**

![End-to-end trace correlation view across observability tools](https://github.com/user-attachments/assets/15cc0aea-7af5-4b9c-9b4c-a8ec86f9df9c)

---

## 🏗️ Infrastructure

The `deployment/` directory contains Bicep IaC to provision the full AI Foundry environment — see [`deployment/README.md`](deployment/README.md) for details.

### Bot the Builder (Teams Bot)

The `bot-app/` directory contains **Bot the Builder**, a Teams bot that manages Foundry deployments via chat commands (`build it`, `list builds`, `build status`, `teardown`, `heartbeat`).

```
Teams ──► Bot Service ──► Container App (M365 Agents SDK)
                               │
                          Queue Storage ──► ACI Worker (PowerShell/Bicep)
```

- **Azure Container App** — bot web server with auto-TLS and UAMI auth
- **ACI Worker** — polls Azure Queue, executes PowerShell deployments, sends results back via proactive messaging
- **Azure Queue + Blob Storage** — RBAC-only (no shared keys), conversation state in Blob
- **Cross-sub logging** — all Container Apps logs go to `DIBSecCom` LAW in the Security subscription

See [`bot-app/python-teams-bot-sample/README.md`](bot-app/python-teams-bot-sample/README.md) for full bot documentation and [`deployment/README.md`](deployment/README.md) for the Teams command listener.

---

## 🔧 Troubleshooting

| Issue | Fix |
|---|---|
| `ImportError: cannot import name 'LogData'` | Rerun **Section 1 — Install Dependencies** to resolve exporter compatibility |
| Signed-in account shows as unavailable | Rerun **Section 3 — Configure the Project Client** (uses `az.cmd` on Windows) |
| Telemetry cell fails after dependency changes | Restart kernel, rerun from **Section 1** through **Section 3.1** |

---

## ✅ Validation Checklist

- [ ] **Section 3** prints `🔐 Credential used: ...` and `👤 Signed-in account: ...`
- [ ] **Section 3.2** prints the [MSFT Learn MCP URL](https://learn.microsoft.com/api/mcp)
- [ ] **Section 4** creates or versions the agent successfully
- [ ] **Section 5** returns a response and appends to `stories.json`
- [ ] **Section 6** returns data for end-to-end and trend KQL queries

---

## 📚 References

- [Microsoft Foundry SDK Overview (Python)](https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/sdk-overview?pivots=programming-language-python#foundry-tools-sdks)
- [Microsoft Foundry Observability: Trace Agent Setup](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-setup?view=foundry)
- [OpenTelemetry for Python: Instrumentation Guide](https://opentelemetry.io/docs/languages/python/instrumentation/)
- [Azure AI Projects SDK Tracing (GitHub)](https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects#tracing)
- [Azure AI Projects Agent Telemetry Samples (GitHub)](https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects/samples/agents/telemetry)
- [Azure MCP Server Documentation](https://learn.microsoft.com/azure/developer/azure-mcp-server/)
