# Microsoft Foundry - AI Agent Telemetry Notebook Guide
> (`zolab-ai-agent-demo-win11.ipynb`)

This document reflects the current Windows 11 notebook flow for creating and querying an Azure AI Foundry agent with end-to-end telemetry.

## Quick Start

1. Open `zolab-ai-agent-demo-win11.ipynb`.
2. Run Cell 3, then switch kernel to **AI Agent Demo (.venv)**.
3. Run Cells 5, 7, 9, and 11 in order to install dependencies, authenticate, and enable tracing.
4. Run Cell 13 (create/version agent), Cell 15 (query + save `stories.json`), and Cell 17 (validate Log Analytics traces).

![image](https://github.com/user-attachments/assets/52606c05-9b90-49e2-bd39-d874d133f1e9)

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
Your Entra ID identity must have **Contributor** (or equivalent) access to the Azure AI Foundry project and the associated Application Insights resource.

### 3. Azure AI Foundry Project with Application Insights
You need a project provisioned in **Microsoft Foundry** that is connected to an **Application Insights** instance backed by a **Log Analytics workspace**. The notebook retrieves the Application Insights connection string from the project at runtime to enable telemetry export.

---

## What this notebook does

The notebook walks through a complete run:

1. Create or reuse a local `.venv` and register a Jupyter kernel.
2. Install Azure AI and telemetry dependencies with compatibility safeguards.
3. Build `AIProjectClient` with `DefaultAzureCredential`.
4. Enable OpenTelemetry + Azure Monitor tracing.
5. Create an agent version and query it.
6. Validate traces in the following:
    - **Microsoft Foundry (Traces - Preview)**
    - **Application Insights**
    - **Log Analytics** (`AppDependencies` table)

![image](https://github.com/user-attachments/assets/aaf309b6-5e28-421f-9784-6118b7b5535c)

![images](https://github.com/user-attachments/assets/b0e6c9a4-d54f-4c0a-a52d-226d72c12ff4)

![image](https://github.com/user-attachments/assets/5334d116-c5cd-4d2a-b3e1-dbe839a9874f)

---

## Notebook section map (1:1)

After selecting the `AI Agent Demo (.venv)` kernel, run cells in order:

1. Cell 3 - **0. Create/Re-use Virtual Environment & Register Kernel**
2. Cell 5 - **1. Install Dependencies**
3. Cell 7 - **2. Import Libraries**
4. Cell 9 - **3. Configure the Project Client**
5. Cell 11 - **3.5 Enable Telemetry**
6. Cell 13 - **4. Create the Agent**
7. Cell 15 - **5. Query the Agent**
8. Cell 17 - **6. Validate Traces in Log Analytics**

---

## Key setup snippets (aligned to notebook headings)

### 0) Create/Re-use Virtual Environment & Register Kernel (Cell 3)

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

### 1) Install Dependencies (Cell 5)

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

### 2) Import Libraries (Cell 7)

This section verifies imports for:

- `DefaultAzureCredential`
- `AIProjectClient`
- `PromptAgentDefinition`
- `AIProjectInstrumentor`

and prints platform and Python version diagnostics.

### 3) Configure the Project Client (Cell 9)

Cell 9 now includes a full authentication hardening flow:

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

### 3.5) Enable Telemetry (Cell 11)

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

### 4) Create the Agent (Cell 13)

Define and create a versioned agent. Replace `<your-agent-name>` and `<your-model-deployment-name>` with your actual values.

- `create_version` will create a new agent or bump the version if the parameters have changed.
- The agent is given a **storytelling** persona via the `instructions` field.

The code wraps creation with `project_client.agents.create_version(...)` and sets tracing attributes such as:

- `agent.name`
- `gen_ai.request.model`
- `agent.version`
- `agent.id`

### 5) Query the Agent (Cell 15)

Use the OpenAI-compatible client from the project to send a prompt to the agent and retrieve a response. The agent is referenced by name using `agent_reference`. Each response is appended to `stories.json` as a new object in the array.

Saved fields include:

- `timestamp`
- `agent`
- `model`
- `prompt`
- `story`
- incremented `id`

### 6) Validate Traces in Log Analytics (Cell 17)

Use this section to validate that telemetry from the notebook is landing in the Log Analytics workspace hosted in the Security subscription.

- Workspace resource ID pattern: `/subscriptions/<subscription-id>/resourceGroups/Sentinel/providers/Microsoft.OperationalInsights/workspaces/<Log-Analytics-Workspace-Name>`
- The Azure CLI extension command `az monitor log-analytics query` may fail in some environments due to extension/runtime mismatch.
- The code cell uses `az rest` against `api.loganalytics.io` as a reliable fallback.

The notebook runs two KQL queries against `api.loganalytics.io` using `DefaultAzureCredential` token auth:

- End-to-end view (dependencies + trace context fields)
- Runs-only trend (calls, failures, avg/p95 duration)

This path is used as a reliable fallback when `az monitor log-analytics query` is impacted by extension/runtime mismatch.

---

## Troubleshooting notes

### ImportError: `cannot import name 'LogData'`

If you hit this while importing Azure Monitor telemetry modules, rerun Cell 5. The exporter upgrade line is intended to resolve this compatibility issue.

### Signed-in account shows as unavailable

If `DefaultAzureCredential` reports `AzureCliCredential` but no user is shown, rerun Cell 9. On Windows the notebook now uses `az.cmd` specifically for this lookup.

### Telemetry cell fails after dependency changes

Restart the notebook kernel and rerun from Cell 5 through Cell 11.

---

## Validation checklist

1. Cell 9 prints `🔐 Credential used: ...` and `👤 Signed-in account: ...`.
2. Cell 11 prints `Tracing enabled -> Application Insights (connection string retrieved from project)`.
3. Cell 13 creates or versions the agent successfully.
4. Cell 15 returns a response and appends a new record in `stories.json`.
5. Cell 17 returns data for end-to-end and trend KQL queries.

---

## References

- https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/sdk-overview?pivots=programming-language-python#foundry-tools-sdks
- https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-setup?view=foundry
- https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects#tracing
- https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects/samples/agents/telemetry
