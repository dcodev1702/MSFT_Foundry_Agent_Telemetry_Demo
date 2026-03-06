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
| **Model Deployment** | `gpt-5.3-chat` is auto-deployed by the [Bicep deployment](deployment/README.md) — no manual setup needed |
| **Python 3.13+** | With `venv` support |
| **Jupyter Notebook** | VS Code with Jupyter extension or JupyterLab |

---

## 🚀 Quick Start

1. Open `zolab-ai-agent-demo-win11.ipynb`
2. Run **Section 0** — creates `.venv` and registers the `AI Agent Demo (.venv)` kernel
3. Switch to the **AI Agent Demo (.venv)** kernel
4. Run sections **1 → 6** in order
5. Observe telemetry in the Azure Portal:
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
| **2** | Import Libraries | Verifies imports for `DefaultAzureCredential`, `AIProjectClient`, `PromptAgentDefinition`, `AIProjectInstrumentor` |
| **3** | Configure the Project Client | Full auth hardening — tries `DefaultAzureCredential`, falls back to CLI install/login if needed |
| **3.1** | Enable Telemetry | Configures OpenTelemetry + Azure Monitor tracing pipeline and instruments the SDK |
| **3.2** | Configure MSFT Learn MCP Tool | Sets up the [Microsoft Learn MCP endpoint](https://learn.microsoft.com/api/mcp) as a tool for the agent |
| **4** | Create the Agent | Defines a versioned agent with storytelling persona and MCP tool access |
| **5** | Query the Agent | Two passes — fiction story generation + MCP-powered Foundry guidance; results saved to `stories.json` |
| **6** | Validate Traces in Log Analytics | Runs KQL queries against `api.loganalytics.io` to verify end-to-end telemetry |

---

## 🔑 Key Configuration

### Telemetry Environment Variables

These **must** be set before calling `instrument()`:

```python
# ---------------------------------------------------------------------------
# Phase 1: Trace settings (must be set before instrumentation)
# ---------------------------------------------------------------------------
os.environ["AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING"] = "true"
os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "gen_ai_latest_experimental"
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"
os.environ.setdefault("OTEL_SERVICE_NAME", "foundry-ai-agent-demo")

# Enable end-to-end correlation between client and service spans
os.environ["AZURE_TRACING_GEN_AI_ENABLE_TRACE_CONTEXT_PROPAGATION"] = "true"
os.environ["AZURE_TRACING_GEN_AI_TRACE_CONTEXT_PROPAGATION_INCLUDE_BAGGAGE"] = "true"
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

![Microsoft Foundry traces view for agent execution](https://github.com/user-attachments/assets/cb347a74-9f66-4ab5-ae3f-a505c5d0ef5c)

**Application Insights**

![Application Insights telemetry view for traced operations](https://github.com/user-attachments/assets/1392ab5a-b390-4e84-9113-3e67b441f0a5)

**Application Insights**

![Application Insights results for AppDependencies telemetry](https://github.com/user-attachments/assets/6eb98f3b-92f8-4508-b05c-0fc0811fcf00)

**Log Analytics - End-to-End Trace Correlation**

![End-to-end trace correlation view across observability tools](https://github.com/user-attachments/assets/a991fb07-9f6e-4609-a5a8-d6416d501086)

---

## 🏗️ Infrastructure

The `deployment/` directory contains Bicep IaC to provision the full AI Foundry environment — see [`deployment/README.md`](deployment/README.md) for details.

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
