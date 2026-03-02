# Microsoft Foundry - AI Agent Telemetry Notebook Guide
> (`zolab-ai-agent-demo-win11.ipynb`)

This document reflects the current Windows 11 notebook flow for creating and querying an Azure AI Foundry agent with end-to-end telemetry.

![image](https://github.com/user-attachments/assets/52606c05-9b90-49e2-bd39-d874d133f1e9)

---

## What this notebook does

The notebook walks through a complete run:

1. Create or reuse a local `.venv` and register a Jupyter kernel.
2. Install Azure AI and telemetry dependencies with compatibility safeguards.
3. Build `AIProjectClient` with `DefaultAzureCredential`.
4. Enable OpenTelemetry + Azure Monitor tracing.
5. Create an agent version and query it.
6. Validate traces in Log Analytics.

![image](https://github.com/user-attachments/assets/aaf309b6-5e28-421f-9784-6118b7b5535c)

![image](https://github.com/user-attachments/assets/5334d116-c5cd-4d2a-b3e1-dbe839a9874f)

---

## Recommended run order

After selecting the `AI Agent Demo (.venv)` kernel, run cells in order:

1. Cell 3 - venv and kernel setup
2. Cell 5 - dependency install
3. Cell 7 - imports
4. Cell 9 - project client + identity hint
5. Cell 11 - telemetry enablement
6. Cell 13 - create agent
7. Cell 15 - query agent and save `stories.json`
8. Cell 17 - query Log Analytics

---

## Key setup snippets

### 1) Windows-safe venv setup (Cell 3)

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

### 2) Dependency install with pip + exporter safeguards (Cell 5)

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

### 3) Azure CLI / PowerShell identity hint (Cell 9)

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

### 4) Telemetry enablement (Cell 11)

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

### 5) Query and persist results (Cell 15)

The notebook queries the agent through the OpenAI-compatible client and appends each result to `stories.json` with:

- `timestamp`
- `agent`
- `model`
- `prompt`
- `story`
- incremented `id`

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
