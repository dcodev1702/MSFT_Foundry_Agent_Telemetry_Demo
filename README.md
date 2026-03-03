# Microsoft Foundry - AI Agent Telemetry Notebook Guide
> (`zolab-ai-agent-demo-win11.ipynb`)

A Jupyter Notebook (Python 3.13) running on Windows 11 that creates and queries a Microsoft Foundry client-side AI Agent demonstrating end-to-end observability.

## Quick Start

1. Open `zolab-ai-agent-demo-win11.ipynb`.
2. Run **0. Create/Re-use Virtual Environment & Register Kernel**, then switch kernel to **AI Agent Demo (.venv)**.
3. Run **1. Install Dependencies**, **2. Import Libraries**, **3. Configure the Project Client**, and **3.1 Enable Telemetry** in order.
4. Run **3.2 Configure MSFT Learn MCP Tool**, **4. Create the Agent**, **5. Query the Agent**, and **6. Validate Traces in Log Analytics**.
5. Go to your Azure portal and observe the telemetry & traces in: Application Insights, Foundry (e.g. Traces), and Log Analytics

![image](https://github.com/user-attachments/assets/4cf6c5e7-036c-4020-aaf6-d67d8a286ebb)

---

## Prerequisites

Before running this notebook, ensure the following are in place:

### 1. Azure CLI Installed
The notebook uses `DefaultAzureCredential`, which relies on Azure CLI for local authentication.

- **Install via winget (Windows):**
    ```powershell
    winget install --id Microsoft.AzureCLI -e --accept-source-agreements --accept-package-agreements
    ```
- **Other platforms / manual install:** https://aka.ms/installazurecli

### 2. Logged in via Azure CLI with Appropriate Permissions
After installing, sign in and verify your session:
```bash
az login
az account show
```
Your Entra ID identity must have **Contributor** (or equivalent) access to the Microsoft Foundry project and the associated Application Insights resource.

### 3. Microsoft Foundry Project with Application Insights
You need a project provisioned in **Microsoft Foundry** that is connected to an **Application Insights** instance backed by a **Log Analytics workspace**. The notebook retrieves the Application Insights connection string from the project at runtime to enable telemetry export.

---

## What this notebook does

The notebook walks through a complete run:

1. Create or reuse a local `.venv` and register a Jupyter kernel.
2. Install Azure AI and telemetry dependencies with compatibility safeguards.
3. Build `AIProjectClient` with `DefaultAzureCredential`.
4. Enable OpenTelemetry + Azure Monitor tracing.
5. Configure the MSFT Learn MCP tool spec.
6. Create an agent version and query it.
7. Validate traces in the following:
    - **Microsoft Foundry (Traces - Preview)**
    - **Application Insights**
    - **Log Analytics** (`AppDependencies` table)

![image](https://github.com/user-attachments/assets/2f1886f2-8e5d-47e3-b014-0eb8bf1cbe4c)

![image](https://github.com/user-attachments/assets/1e7146ef-c44f-4f51-b841-29318fb47a38)

![images](https://github.com/user-attachments/assets/81c0ca29-e9f8-4ee8-80d0-f62f08ff0c50)

![image](https://github.com/user-attachments/assets/5334d116-c5cd-4d2a-b3e1-dbe839a9874f)

---

## Notebook section map

After selecting the `AI Agent Demo (.venv)` kernel, run sections in order:

- **0. Create/Re-use Virtual Environment & Register Kernel**
- **1. Install Dependencies**
- **2. Import Libraries**
- **3. Configure the Project Client**
    - **3.1 Enable Telemetry**
    - **3.2 Configure MSFT Learn MCP Tool**
        - **MSFT Learn MCP Tool Spec (code block)**
- **4. Create the Agent**
- **5. Query the Agent**
- **6. Validate Traces in Log Analytics**

---

## Key setup snippets (aligned to notebook headings)

### 0. Create/Re-use Virtual Environment & Register Kernel

```python
venv_dir = os.path.join(os.getcwd(), ".venv")
subprocess.check_call([sys.executable, "-m", "venv", venv_dir])

venv_python = (
    os.path.join(venv_dir, "Scripts", "python.exe")
    if os.name == "nt"
    else os.path.join(venv_dir, "bin", "python")
)

subprocess.check_call([venv_python, "-m", "pip", "install", "--upgrade", "ipykernel"])
subprocess.check_call([
    venv_python,
    "-m",
    "ipykernel",
    "install",
    "--user",
    "--name",
    "ai-agent-demo",
    "--display-name",
    "AI Agent Demo (.venv)",
])
```

### 1. Install Dependencies

```python
outdated = subprocess.check_output(
    [sys.executable, "-m", "pip", "list", "--outdated", "--format=json"],
    text=True,
)
if any(pkg["name"].lower() == "pip" for pkg in json.loads(outdated)):
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])

%pip --disable-pip-version-check install --upgrade --pre azure-monitor-opentelemetry-exporter
%pip --disable-pip-version-check install --pre "azure-ai-projects>=2.0.0b4"
%pip --disable-pip-version-check install azure-identity azure-monitor-opentelemetry azure-core-tracing-opentelemetry
```

Why this matters:
- `pip` upgrades only when it is actually outdated.
- Exporter is explicitly updated to avoid OpenTelemetry import mismatches.

### 2. Import Libraries

This section verifies imports for:

- `DefaultAzureCredential`
- `AIProjectClient`
- `PromptAgentDefinition`
- `AIProjectInstrumentor`

and prints platform and Python version diagnostics.

### 3. Configure the Project Client

This section includes a full authentication hardening flow:

- Tries `DefaultAzureCredential` token acquisition first.
- If authentication fails, checks for Azure CLI (`az.cmd` on Windows).
- Attempts Azure CLI install with `winget` if missing.
- Triggers interactive `az login` if no active CLI session exists.
- Rebuilds `DefaultAzureCredential` and retries token acquisition.
- Creates `AIProjectClient` only after successful authentication.

It also prints the selected successful credential and tries to resolve signed-in identity details for:

- `AzureCliCredential` via `az.cmd account show --query user.name -o tsv`
- `AzurePowerShellCredential` via `pwsh` and `Get-AzContext`

```python
def _resolve_identity_hint(credential_name: str) -> str | None:
    if credential_name == "AzureCliCredential":
        az_exe = "az.cmd" if os.name == "nt" else "az"
        return _run_command([az_exe, "account", "show", "--query", "user.name", "-o", "tsv"])
    if credential_name == "AzurePowerShellCredential":
        powershell_cmd = "$ctx = Get-AzContext; if ($ctx -and $ctx.Account) { $ctx.Account.Id }"
        return _run_command(["pwsh", "-NoProfile", "-Command", powershell_cmd])
    return None
```

This is the Windows fix for Azure CLI account resolution from Python subprocess (`az.cmd` instead of `az`).

Expected output:

```text
🔐 Credential used: AzureCliCredential
👤 Signed-in account: agent007@BondEnterprises.onmicrosoft.com
```

### 3.1. Enable Telemetry

Configure tracing per the [azure-ai-projects SDK tracing guide](https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects#tracing):

1. **`configure_azure_monitor`** — Sets up the full OpenTelemetry pipeline (TracerProvider, exporter, span processors) to send traces to Application Insights.
2. **`AIProjectInstrumentor`** — Instruments all `azure-ai-projects` SDK operations (agent create/version, list, etc.) and automatically instruments OpenAI responses/conversations operations.

> **Note:** `AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=true` must be set **before** calling `instrument()`. Content recording is controlled by `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`.

```python
os.environ["AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING"] = "true"
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"
os.environ["AZURE_TRACING_GEN_AI_ENABLE_TRACE_CONTEXT_PROPAGATION"] = "true"
os.environ["AZURE_TRACING_GEN_AI_TRACE_CONTEXT_PROPAGATION_INCLUDE_BAGGAGE"] = "true"

application_insights_connection_string = project_client.telemetry.get_application_insights_connection_string()
from azure.monitor.opentelemetry import configure_azure_monitor

configure_azure_monitor(connection_string=application_insights_connection_string)
AIProjectInstrumentor().instrument(enable_content_recording=True)
```

### 3.2. Configure MSFT Learn MCP Tool

This section introduces the MCP setup step and creates the MCP tool spec used by agent creation in Step 4.

- MSFT Learn MCP URL: `https://learn.microsoft.com/api/mcp`
- MSFT Learn MCP documentation: [Azure MCP Server docs](https://learn.microsoft.com/azure/developer/azure-mcp-server/)
- The code prints the URL for verification.
- The code creates `mcp_tool_spec` and Step 4 passes it via `tools=[mcp_tool_spec]`.

```python
from azure.ai.projects.models import MCPTool

msft_learn_mcp_url = "https://learn.microsoft.com/api/mcp"
print(f"MSFT Learn MCP URL: {msft_learn_mcp_url}")

mcp_tool_spec = MCPTool(
    server_label="msft-learn",
    server_url=msft_learn_mcp_url,
)
```

### 4. Create the Agent

Define and create a versioned agent. Replace `<your-agent-name>` and `<your-model-deployment-name>` with your actual values.

- `create_version` will create a new agent or bump the version if the parameters have changed.
- The agent is given a **storytelling** persona via the `instructions` field.
- The MSFT Learn MCP tool is available to the agent via `mcp_tool_spec` in `tools=[mcp_tool_spec]`.

The code wraps creation with `project_client.agents.create_version(...)` and sets tracing attributes such as:

- `agent.name`
- `gen_ai.request.model`
- `agent.version`
- `agent.id`

### 5. Query the Agent

Use the OpenAI-compatible client from the project to send prompts to the agent and retrieve responses. The section runs two independent passes:

- **Pass 1 (fiction):** Generates a six-sentence fictional story only.
- **Pass 2 (facts):** Uses the MSFT Learn MCP tool for concise Microsoft Foundry guidance.

The agent is referenced by name using `agent_reference`. MCP approvals are handled automatically when `mcp_approval_request` items are returned, by submitting `mcp_approval_response` payloads until text is produced (or max rounds are reached).

Each run is appended to `stories.json` with these fields:

- `id` (incremented)
- `timestamp`
- `agent`
- `model`
- `prompt`
- `story`
- `msft_learn_insights`
- `combined_output`

### 6. Validate Traces in Log Analytics

Use this section to validate that telemetry from the notebook is landing in the Log Analytics workspace hosted in the Security subscription.

- Workspace resource ID pattern: `/subscriptions/<subscription-id>/resourceGroups/Sentinel/providers/Microsoft.OperationalInsights/workspaces/<Log-Analytics-Workspace-Name>`
- The Azure CLI extension command `az monitor log-analytics query` may fail in some environments due to extension/runtime mismatch.
- The code cell uses direct HTTPS calls to `api.loganalytics.io` with a `DefaultAzureCredential` bearer token (`urllib.request`) as a reliable fallback.

The notebook runs two KQL queries against `api.loganalytics.io` using `DefaultAzureCredential` token auth:

- End-to-end view (dependencies + trace context fields)
- Runs-only trend (calls, failures, avg/p95 duration)

This path is used as a reliable fallback when `az monitor log-analytics query` is impacted by extension/runtime mismatch.

---

## Troubleshooting notes

### ImportError: `cannot import name 'LogData'`

If you hit this while importing Azure Monitor telemetry modules, rerun **1. Install Dependencies**. The exporter upgrade line is intended to resolve this compatibility issue.

### Signed-in account shows as unavailable

If `DefaultAzureCredential` reports `AzureCliCredential` but no user is shown, rerun **3. Configure the Project Client**. On Windows the notebook now uses `az.cmd` specifically for this lookup.

### Telemetry cell fails after dependency changes

Restart the notebook kernel and rerun from **1. Install Dependencies** through **3.1 Enable Telemetry**.

---

## Validation checklist

1. **3. Configure the Project Client** prints `🔐 Credential used: ...` and `👤 Signed-in account: ...`.
2. **3.2 Configure MSFT Learn MCP Tool** prints `MSFT Learn MCP URL: https://learn.microsoft.com/api/mcp`.
3. **4. Create the Agent** creates or versions the agent successfully.
4. **5. Query the Agent** returns a response and appends a new record in `stories.json`.
5. **6. Validate Traces in Log Analytics** returns data for end-to-end and trend KQL queries.

---

## References

- https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/sdk-overview?pivots=programming-language-python#foundry-tools-sdks
- https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-setup?view=foundry
- https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects#tracing
- https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects/samples/agents/telemetry
