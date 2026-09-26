# 🤖 Microsoft Foundry — Agent Framework Observability PoC

Jupyter notebooks that configure and query Microsoft Foundry agents with **end-to-end observability** — tracing agent runs, tool invocations, and responses across Application Insights, Microsoft Foundry Traces, and Log Analytics. The Win11 and Linux notebooks use **Microsoft Agent Framework (MAF) workflows** around their existing Azure AI Projects + Responses API calls. MAF and Foundry share the same OpenTelemetry/Azure Monitor pipeline. Windows requires Python 3.14+; the Ubuntu 26.04 notebook uses **Python 3.14.7** in its own environment, and the separate macOS notebook supports Python 3.13+.

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
| **Model Deployment** | The deployment script offers `gpt-4.1-mini`, `gpt-5.3`, `gpt-5.4`, or `grok-4-1-fast-reasoning`. The notebook can also use an existing compatible deployment, such as `gpt-5.6-terra`, through its local build metadata (see below). |
| **Python** | Python 3.14.7 for Ubuntu 26.04, Python 3.14+ for Win11, or Python 3.13+ for macOS; Linux bootstrap uses `uv`, including on hosts without system `pip`/`ensurepip` |
| **Jupyter Notebook** | VS Code with Jupyter extension or JupyterLab |

---

## 🚀 Quick Start

1. Run the deployment first — it generates `build_info-<suffix>.json` at the repo root (see [`deployment/README.md`](deployment/README.md))
2. Open [the Linux notebook](zolab-ai-agent-demo-linux.ipynb), [the macOS notebook](zolab-ai-agent-demo-macbook.ipynb), or [the Windows notebook](zolab-ai-agent-demo-win11.ipynb)
3. Run **Section 0** — creates `.venv` (Windows/macOS) or `.venv-linux` (Linux) and registers the notebook kernel; first-time Linux setup is below
4. Select the registered kernel: **AI Agent Demo (.venv, Python 3.14)** on Win11 or **AI Agent Demo (Linux, Python 3.14.7)** on Linux
5. Run sections **1 → 5** in order
6. Run **Section 6** and inspect telemetry in the Azure Portal:
   - 📊 **Application Insights** — request/dependency traces
   - 🔍 **Microsoft Foundry** — agent execution traces
   - 📡 **Log Analytics** — span health in `AppDependencies`, conversation/tool content in `AppGenAIContent`, and correlated exception drill-downs
   - **Response accounting and MCP outcomes** — input/output, cached input and reasoning tokens; optional estimates using your supplied prices; approvals, tool errors, request failures and final stage outcomes. See [configuration and interpretation](docs/observability.md#response-usage-cost-estimates-and-mcp-outcomes).

On Windows, Section 1 installs [requirements-notebook.txt](requirements/requirements-notebook.txt) and runs `pip check`. If SDKs were already imported before updating, restart the kernel and rerun from the beginning. The Windows dependency matrix applies to the Windows notebook only; Linux has its own profile below, while the macOS notebook and standalone [Agent Framework SDK PoC](agent-framework-demo/README-agent-framework-sdk-poc.md) have separate setup instructions.

### Ubuntu 26.04 Linux Setup

[zolab-ai-agent-demo-linux.ipynb](zolab-ai-agent-demo-linux.ipynb) uses an isolated
`.venv-linux` and the `ai-agent-demo-linux` kernel. Its requirements and constraints
do not include the Windows profiles, Windows-only packages, or Windows wheel-build
scripts. The original Windows/macOS notebooks and the standalone demo are unchanged.

This host has Ubuntu 26.04.1 x86_64, system Python 3.14.4, and `uv`, but no system
`pip` or `ensurepip`. Keep system Python intact: use `uv` to provision the requested
**Python 3.14.7** without adding it to the user's executable directory.
The installed `uv` catalog ends at Python 3.14.6, so setup uses
[official uv download metadata at a fixed revision](https://github.com/astral-sh/uv/blob/716f320609fec9a36b27718be4c403d73fab7c9a/crates/uv-python/download-metadata.json),
including upstream download checksums, rather than silently selecting an older Python.
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first if it is absent.

For **first-time setup**, run these commands from the repository root:

```bash
uv python install --no-bin --python-downloads-json-url \
  https://raw.githubusercontent.com/astral-sh/uv/716f320609fec9a36b27718be4c403d73fab7c9a/crates/uv-python/download-metadata.json \
  3.14.7
uv venv --python 3.14.7 --no-python-downloads --seed .venv-linux
.venv-linux/bin/python -m pip install --upgrade -r requirements/requirements-notebook-linux.txt
.venv-linux/bin/python -m pip check
.venv-linux/bin/python -m ipykernel install --user \
  --name ai-agent-demo-linux --display-name "AI Agent Demo (Linux, Python 3.14.7)"
```

If `.venv-linux` already exists, use Section 0 to validate and reuse it instead
of recreating it. An environment with a different Python version must be moved
aside explicitly before setup; the notebook will not overwrite it. Select
**AI Agent Demo (Linux, Python 3.14.7)** in VS Code, then run environment
verification and Section 1. After package changes, restart that kernel and rerun
verification and the runtime cells. Activating a terminal environment does not
switch an already-running notebook kernel.

Azure CLI is a separate system tool, not a notebook Python dependency. If missing,
follow Microsoft's [Ubuntu installation instructions](https://learn.microsoft.com/cli/azure/install-azure-cli-linux?pivots=apt).
This Linux host is verified with **Azure CLI 2.90.0** and the following extensions
(versions checked **2026-09-26**):

| Extension | Version | Purpose |
|---|---|---|
| `log-analytics` | `1.0.0b2` (preview) | KQL queries through `az monitor log-analytics query` |
| `azure-devops` | `1.0.8` | Azure DevOps Services: projects, repos, pipelines, boards, and artifacts |

Install them in the Linux terminal, outside the notebook virtual environment:

```bash
az extension add --name log-analytics --version 1.0.0b2 --allow-preview true --yes
az extension add --name azure-devops --version 1.0.8 --allow-preview false --yes
az extension list --output table
az monitor log-analytics query --help
az devops project list --help
```

There is no extension named `kql` in the official index:
[`log-analytics`](https://learn.microsoft.com/cli/azure/monitor/log-analytics#az-monitor-log-analytics-query)
provides the Azure Monitor KQL query command. Section 6 still uses the Logs API
directly; installing this extension does not change the notebook's telemetry path.
[`azure-devops`](https://learn.microsoft.com/azure/devops/cli/) is the Azure DevOps
extension, not the separate Azure Developer CLI (`azd`). CLI extensions are not
added to the Python requirements or constraints.

For VS Code Remote/SSH or a headless host, sign in in a terminal **on that Linux host**:

```bash
az login --use-device-code
az account show
```

Use your organization's approved interactive method if device-code authentication
is restricted. The notebook reports missing or expired credentials rather than
starting a hidden interactive login or installing system packages. Existing
deployment metadata, project access, and Sentinel connection/consent requirements
still apply.

### Linux Dependency Matrix

Reviewed on **2026-09-26** for **Ubuntu 26.04.1 / CPython 3.14.7 / x86_64**.
The runtime resolves independently of Windows, uses the latest stable direct
releases where compatible, and retains the Azure Monitor/OpenTelemetry release train.
OpenTelemetry **1.45.0 / instrumentation 0.66b0** are newer but incompatible with
[Azure Monitor 1.8.10's declared bounds](https://pypi.org/pypi/azure-monitor-opentelemetry/1.8.10/json),
so this profile deliberately uses **1.44.0 / 0.65b0**. The tracing bridge,
instrumentation, and Monitor exporter require preview-version packages; the
installer does not enable prereleases globally.

| Package | Version | Notes |
|---|---|---|
| `pip` / `ipykernel` | `26.2.1` / `7.3.0` | Isolated installer and notebook kernel |
| `agent-framework-core` | `1.19.0` | Existing workflows; no extra MAF providers |
| `azure-ai-projects` | `2.7.0` | Foundry agents, project and agent-endpoint Responses clients |
| `openai` | `3.19.2` | Responses and conversations APIs |
| `httpx2` / `httpcore2` | `2.13.1` | Matching transport pair |
| `azure-identity` | `1.25.3` | Stable release instead of the Windows profile's `1.26.0b2` preview |
| `azure-monitor-opentelemetry` | `1.8.10` | Resolves exporter `1.0.0b57` |
| `azure-core-tracing-opentelemetry` | `1.0.0b13` | Azure SDK tracing bridge |
| `opentelemetry-api` / `opentelemetry-sdk` | `1.44.0` | Compatible with Azure Monitor and MAF |
| `opentelemetry-instrumentation-httpx` | `0.65b0` | Includes the HTTPX2 instrumentor |
| `nbclient` / `nbformat` | `0.11.0` / `5.11.1` | Optional validation profile |

- [requirements-notebook-linux.txt](requirements/requirements-notebook-linux.txt):
  exact direct runtime pins.
- [requirements-notebook-linux-validation.txt](requirements/requirements-notebook-linux-validation.txt):
  runtime plus notebook validation tools.
- [constraints-notebook-linux.txt](requirements/constraints-notebook-linux.txt):
  96 resolved package versions for these two Linux profiles, including Linux
  terminal dependencies `pexpect`/`ptyprocess`. This is a version snapshot,
  not a hash-verified lock or a promise of compatibility with other platforms.
  Constraints do not install optional validation packages.

Package installation uses the configured index with TLS verification enabled.
If an approved feed lacks a release, have it admitted or supply approved
Linux-compatible wheels; do not use Windows constraints or disable certificate
verification. Upgrade the runtime and telemetry train together and rerun:

```bash
.venv-linux/bin/python -m pip install -r requirements/requirements-notebook-linux-validation.txt
.venv-linux/bin/python -m pip check
.venv-linux/bin/python -m unittest discover -s tests -p "test_notebook*.py" -v
```

These tests exercise the real SDKs with in-memory HTTP transports and MAF
callbacks, and cover Linux bootstrap, kernel isolation, authentication errors,
dependency versions, and unchanged workflow/telemetry behavior. They do not
execute paid model calls or verify live Azure permissions and telemetry ingestion.

### Notebook Support Layout

The notebooks stay at the repository root; run their kernels and the commands
below from that directory. Supporting files are grouped by purpose:

```text
notebook_support/
  __init__.py
  agent_endpoints.py
  observability.py
  response_observability.py
  workflow.py
requirements/
  requirements-notebook.txt
  requirements-notebook-shared.txt
  requirements-notebook-validation.txt
  constraints-notebook-win11.txt
  requirements-notebook-linux.txt
  requirements-notebook-linux-validation.txt
  constraints-notebook-linux.txt
docs/
  observability.md
  OTEL-Agent-Spans.md
```

- [notebook_support](notebook_support) is a Python package. Notebook and test
  imports use `notebook_support.agent_endpoints`, `notebook_support.observability`,
  `notebook_support.response_observability` and `notebook_support.workflow`;
  no `sys.path` workaround is needed.
- [requirements](requirements) contains the root notebooks' platform-specific
  dependency profiles and constraints. Linux is independent of the Windows
  snapshot; `-r`/`-c` includes remain relative to their requirement files.
- [docs](docs) contains the [observability guide](docs/observability.md) and
  [span reference](docs/OTEL-Agent-Spans.md).

The standalone Agent Framework demo and bot keep their own requirements in their
existing project directories. After pulling this layout change, restart the
notebook kernel and rerun the runtime cells from **Confirm Existing Deployment**.
Clear outputs before saving or sharing executed notebooks.

The shared [VS Code settings](.vscode/settings.json) suppress Pylint and Pylance
diagnostics for notebooks, while keeping autocomplete/navigation and normal
checks for `.py` files. This does not disable notebook execution errors or the
regression suite. Machine-specific Python search paths remain local.

### Windows Dependency Matrix

Reviewed on **2026-09-18** using the approved package feed and verified official
GitHub release sources. Recent SDK wheels are built locally when the company
feed has not yet admitted a release; no direct PyPI artifact fallback or
certificate-verification bypass is used.

| Package | Version | Role |
|---|---|---|
| `ipykernel` | `7.3.0` | Notebook kernel |
| `agent-framework-core` | `1.19.0` | Sequential workflow/executor orchestration and native workflow tracing |
| `azure-ai-projects` | `2.6.1` | Foundry project agents, MCP definitions and Responses client |
| `openai` | `3.16.1` | Responses and conversations API; HTTPX2 transport |
| `httpx2` | `2.13.0` | Current compatible transport; matches HTTPCore2 2.13.0 |
| `azure-identity` | `1.26.0b2` | Existing preview credential line retained |
| `azure-monitor-opentelemetry` | `1.8.10` | Azure Monitor exporter configuration |
| `azure-core-tracing-opentelemetry` | `1.0.0b13` | Azure SDK tracing bridge |
| `opentelemetry-api` / `opentelemetry-sdk` | `1.44.0` | Tracing APIs, native resources and the telemetry runtime |
| `opentelemetry-instrumentation-httpx` | `0.65b0` | Includes both HTTPX and HTTPX2 instrumentors, managed by Azure Monitor |

Azure Monitor resolves exporter **1.0.0b57** on the matching OpenTelemetry train. The former `azure-ai-projects<2.5` restriction is removed. Keep the tested SDK versions together rather than independently upgrading the runtime or instrumentation.

### Dependency Profiles

- [requirements-notebook.txt](requirements/requirements-notebook.txt): Windows runtime, including MAF core **1.19.0**. No MAF model-provider package is needed because existing Foundry calls remain unchanged.
- [requirements-notebook-shared.txt](requirements/requirements-notebook-shared.txt): runtime plus optional OpenAI provider **1.14.4**, orchestrations **1.2.0**, and OTLP gRPC exporter **1.44.0**. These are not used by the Windows workflow integration.
- [requirements-notebook-validation.txt](requirements/requirements-notebook-validation.txt): runtime plus `nbclient==0.11.0` and `nbformat==5.11.1` for automated execution and validation.
- [constraints-notebook-win11.txt](requirements/constraints-notebook-win11.txt): 102 version constraints covering the three profiles on Windows / CPython 3.14. Constraints do not install optional packages. This is a version snapshot, not a hash-verified lock, and does not cover other platforms.
- [agent-framework-demo/requirements.txt](agent-framework-demo/requirements.txt): exact direct pins for the standalone Agent Framework notebook, installed into its independent `agent-framework-demo\.venv` and verified by an in-notebook package inventory.

Section 0 also constrains the bootstrap kernel version. Installing the smaller profile does **not** uninstall existing shared packages. Do not prune the shared environment blindly; use a separate environment for a minimal installation. Azure Identity's previously validated preview version is retained, not silently downgraded.

### GitHub Release Wheels for Restricted Package Feeds

After creating the root `.venv`, run:

```powershell
.\build_notebook_wheels.ps1
.\.venv\Scripts\python.exe -m pip install --find-links .\.wheels -r .\requirements\requirements-notebook.txt
.\.venv\Scripts\python.exe -m pip check
```

[build_notebook_wheels.ps1](build_notebook_wheels.ps1) reuses the verified build
engine from the standalone demo, with this notebook's own Python interpreter,
source checkout directory, wheel cache, and
[notebook-source-releases.json](notebook-source-releases.json). It checks official
repository URLs, immutable release commits, clean source, and expected wheel
filenames. SHA-256 hashes and source provenance are written to the ignored root
`.wheels` directory. The script only builds; it does not install packages into
either runtime environment.

Section 1 automatically uses the root `.wheels` cache when present and refuses
to install into the wrong kernel. Restart the **AI Agent Demo (.venv, Python
3.14)** kernel after upgrades. The two notebooks do not share virtual
environments; the Foundry runtime does not inherit A2A/MCP server dependencies
from the standalone demo.

For local validation tooling and the regression suite:

```powershell
.\.venv\Scripts\python.exe -m pip install --find-links .\.wheels -r .\requirements\requirements-notebook-validation.txt
.\.venv\Scripts\python.exe -m unittest discover -s .\tests -p "test_notebook*.py" -v
```

Install the optional shared profile with the same `--find-links .\.wheels`
argument and `-r .\requirements\requirements-notebook-shared.txt` only when additional MAF
providers or an OTLP exporter are needed by other work. The notebook's
`SHOW_GENAI_CONTENT` setting controls report previews independently of capture.
The current notebook opts in; set it to `False` and clear outputs before sharing.

The notebook regression suite includes actual MAF workflow execution with local
callbacks and an in-memory trace exporter: ordering, exactly-once persistence
within one execution, failures, optional Sentinel, and shared trace ancestry.
A runtime/validation-only environment skips the optional provider-profile check. SDK tests use
an in-memory HTTP transport; they do not create cloud resources or execute
paid model calls. Both environments pass `pip check`.

---

## 📓 Notebook Sections

After selecting the platform's registered demo kernel, run sections in order:

| # | Section | What It Does |
|---|---|---|
| **0** | Create or Reuse Virtual Environment | Validates Python 3.14 on Win11 or 3.14.7 on Linux, creates `.venv` or `.venv-linux`, installs `ipykernel`, and registers the platform's Jupyter kernel |
| **1** | Install Dependencies | Installs the constrained MAF core, Foundry, Azure Identity and Azure Monitor/OpenTelemetry runtime profile |
| **2** | Import Libraries | Verifies the Foundry clients, workflow helper and native OpenTelemetry `Resource` imports |
| **3** | Configure Credentials and Clients | Reuses deployment values from `build_info-<suffix>.json`, resolves Azure auth, and configures the Foundry project client plus Responses API settings |
| **3.1** | Enable Telemetry | Configures one Azure Monitor/OpenTelemetry provider for MAF, Foundry and HTTP tracing, with shared capture and propagation controls |
| **3.2** | Configure MSFT Learn MCP Tool | Attaches the [Microsoft Learn MCP endpoint](https://learn.microsoft.com/api/mcp) directly to the Foundry project agent |
| **3.3** | Configure Microsoft Sentinel MCP Tool | Preserves the existing Foundry project-connection dependency for the Sentinel MCP tool |
| **4** | Prepare the Agents | In backend sync mode, creates/reuses and activates the matching definition version; strict pinned mode validates only; project mode creates agent versions as before |
| **5** | Query the Agent | Runs the story/facts/persistence MAF workflow around the existing Responses calls; saves results and generates a Marp deck |
| **5.1** | Query Microsoft Sentinel | Runs an optional independent MAF workflow while preserving Sentinel's MCP dependency path |
| **6** | Validate Telemetry | Flushes traces, waits for ingestion, then validates workflow/executor coverage, span ancestry, dependencies, failures, service identity and version |

For bot and worker post-deploy validation, run [deployment/run-smoke-checks.sh](deployment/run-smoke-checks.sh) and then exercise the manual Teams smoke sequence from [deployment/OPERATIONS-RUNBOOK.md](deployment/OPERATIONS-RUNBOOK.md).

### Model Metadata in Marp Outputs

Both the main story/Learn deck and the Sentinel deck display a footer on every
slide with **LLM type/provider**, **model name**, and **model version**. For the
current deployment, these are `OpenAI`, `gpt-5.6-terra`, and `2026-07-09`.
The run-metadata slide lists the deployment alias and response model identifiers
separately; neither is confused with the agent version.

Section 4 resolves the underlying model metadata once through
`project_client.deployments.get`. Sections 5 and 5.1 validate the response model
against that snapshot and persist it with each record's `model_metadata`.
Missing deployment metadata or an unexpected response model raises an explicit
error rather than displaying guessed values. Rerun **Section 4**, then **5 and
5.1**, to regenerate both decks with the footer after updating the notebook.

### Backend Agent Endpoints

The Windows and Linux notebooks support two explicit invocation modes through
[notebook_support/agent_endpoints.py](notebook_support/agent_endpoints.py):

- **`project`**: the backward-compatible project Responses endpoint with an
  `agent_reference`. This remains the default for build files without migration
  settings.
- **`agent_endpoint`**: separate stable Responses endpoints for the main and
  Sentinel backend agents. Requests do not send an `agent_reference` override;
  each endpoint's pinned version selects the agent.

The backend-only migration uses the existing project, model and MCP connection.
It does not publish to Teams/M365, provision a hosted-agent container, or change
the caller's authentication method.

The local build metadata selects the mode and fixed versions:

```json
{
  "agent_invocation_mode": "agent_endpoint",
  "backend_version_policy": "sync",
  "backend_agents": {
    "main": {"name": "ZoDEfendersAgent-1702-backend", "version": "2"},
    "sentinel": {"name": "ZoDEfendersAgent-1702-sentinel-backend", "version": "2"}
  }
}
```

These fields supplement the existing build file; do not replace its other values.
The metadata file remains Git-ignored. Backend endpoints require a unique
identity, an enabled Responses endpoint with Entra authorization and a single
100% fixed-version selector. Version handling is explicit:

- **`sync` (selected for this demo):** Section 4 compares the notebook definition
  with the actual active version. It reuses the active version when unchanged,
  reuses a matching latest candidate if available, or calls `create_version`.
  It verifies the returned definition, pins the endpoint to that concrete version,
  reads the endpoint back, and saves the selected version to the local build file
  and in-memory runtime. The example version values above are checkpoints, not
  permanent restrictions. Rerunning unchanged definitions does not create or
  reactivate versions.
- **`pinned` (default when no policy is specified):** read-only validation retains
  the stricter release workflow. A definition or pin mismatch raises rather than
  creating/promoting. Use this for consumers that require separate release approval.

Set `FOUNDRY_AGENT_VERSION_POLICY` to override the local policy explicitly.
**Sync activation changes behavior for every consumer of that agent endpoint.**
It does not enable `@latest`, delete older versions, change identities/endpoints,
or remove authorization. Do not run competing releases concurrently: the helper
checks for endpoint changes before activation but cannot make service updates
and local file persistence one atomic transaction.
Sync verifies the definition and endpoint state, not a full evaluation suite
before activation. Use `pinned` when a release must pass separate approval.

For strict releases, create and test a candidate separately, then update both
the endpoint selector and the local selected version. For rollback to the legacy path, set
`FOUNDRY_AGENT_INVOCATION_MODE=project` before restarting the kernel and rerunning
from Section 3, or set `agent_invocation_mode` to `project` in the local build
file. The legacy agent names and invocation path are retained. Start new
conversations after switching modes; conversation portability is not assumed.
An agent-version pin does not freeze changes made directly to the underlying
model deployment or external MCP service; those remain separate change controls.

If activation succeeds but saving local metadata fails, the error reports the
active version and does not pretend the run succeeded. Resolve the file error and
rerun sync: it reconciles the checkpoint without creating another version.
Concurrent local file changes are detected before replacement; unrelated build
metadata is preserved. Start new conversations when you activate changed behavior.
Synchronization is per agent: if a later agent fails, an earlier successful
activation remains recorded and can be reused on the next run.

**Version-sync fix validation:** run `447909c6-f671-4146-bade-cf841fd3644a`
successfully created and activated version 2 of both backend agents from the
edited notebook definitions, persisted both selections and completed all 10
runtime cells with no failures. Two additional unchanged sync rounds per agent
were tested with create/update operations blocked: version 2 was reused and the
build file was not rewritten. Version 1 and the original identities were retained.
All 80 regression tests passed.

The project metadata is regenerated by environment deployment scripts; preserve
or reapply these local migration settings when regenerating it. Other notebooks
have not automatically been migrated by adding these Windows runtime settings.

**Validated migration:** run `b4591fc5-2289-4527-8e81-a9e97e153f34` used the saved
backend configuration without injected settings and passed all 10 runtime cells.
It exercised story generation, Learn MCP, Sentinel `SigninLogs`, persistence and
model metadata. Its inventory contained 54 correlated spans, seven Responses
dependencies, one persistence span and zero failures. All 65 regression tests
passed. The original agents remained unchanged, and anonymous requests to both
new endpoints returned HTTP 401.
Both exported Marp decks were also checked in the browser: all seven slides
retained the model footer without clipping/overlap and showed the backend runtime
label on the metadata slide.

---

## 🔑 Key Configuration

### Build-Time Notebook Configuration

The deployment script writes a repo-local `build_info-<suffix>.json` file at build time. The notebooks read the latest matching file in the **Confirm Existing Deployment** section and reuse it in **Section 3** to populate:

- `foundry_proj_ep` → the Microsoft Foundry project endpoint
- `genai_model` → the model deployment name used when creating both notebook agents

This removes the need to hardcode the Foundry project endpoint in the notebook or store it in source control.

### Switching the Notebook Model

The current local demo configuration selects **`gpt-5.6-terra`**, backed by the
OpenAI model **`gpt-5.6-terra` version `2026-07-09`** on **GlobalStandard** in the
existing East US 2 Foundry account. It is a separate deployment; the previous
`gpt-5.4` deployment is retained. That older deployment name is an alias for
`gpt-5.4-mini`, not proof that the underlying model is GPT-5.4.

To select an already-provisioned deployment, update only `genai_model` in the
local `build_info-<suffix>.json` read by the notebook:

```json
{
  "genai_model": "gpt-5.6-terra"
}
```

This illustrates one field; preserve all other fields in your existing file.

Use the **deployment name**, and ensure its model supports the Responses API and
the notebook's MCP tools. Editing this field does not provision a model. The
build metadata is intentionally Git-ignored; this example does not automatically
switch another user's environment or change the deployment script's model menu.
Other notebooks that read the same build metadata will also see the selected
deployment on their next run.

Restart the kernel and rerun the runtime cells from **Confirm Existing Deployment**
through **Section 6**. Sections 3 and 4 use the selected deployment for both agents,
model environment variables and request telemetry. No SDK update, chat-model
change or new authentication flow is required when the existing credentials and
Sentinel connection remain authorized.

The [Terra validation evidence](docs/observability.md#gpt-56-terra-notebook-validation--2026-09-15)
covers all 10 runtime cells, real Learn/Sentinel tool calls and model metadata in
current-run telemetry. The three environment/dependency setup cells were not
rerun because the dependency set did not change.

### Observability

Section **3.1** configures the notebook's observability path end to end:

- **Azure Monitor + Application Insights** receive exported OpenTelemetry traces.
- **MAF core workflows** add native `workflow.run`, `executor.process`, graph/message and error spans. `enable_instrumentation` reuses the already configured provider; it does not call `configure_otel_providers` or attach a second exporter.
- **Microsoft Foundry client-side tracing** is enabled for project-backed agent and Responses API activity.
- **Azure Monitor owns HTTPX/HTTPX2 auto-instrumentation**. Foundry instrumentation adds GenAI spans; explicit notebook spans retain the demo's orchestration and dependency context. The notebook no longer uninstalls/re-wraps HTTP instrumentors.
- **Trace context and baggage propagation** are enabled so notebook correlation identifiers flow with downstream requests.
- **GenAI model/tool span semantics** remain controlled by the installed Projects instrumentor; MAF contributes workflow/executor semantics rather than wrapping model calls a second time.
- **One content policy** controls Projects, MAF and custom spans: `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` defaults to `true` for this demo, accepts only `true`/`false` (case-insensitive), and is passed explicitly to both SDKs. Set it to `false` to opt out. MAF message events are disabled, and workflow edges carry only an opaque run UUID, not prompts/results. Changing policy requires a kernel restart.
- **Trace-only export is enforced** with `OTEL_LOGS_EXPORTER=none`, `OTEL_METRICS_EXPORTER=none`, `enable_live_metrics=False`, and `enable_performance_counters=False`. Sampling is explicitly fixed at 100%, overriding inherited sampling settings for this demo. `OTEL_TRACES_EXPORTER=none` is rejected.
- **Native resources preserve service/session/project identity**, add `deployment.environment.name`, and retain additional `OTEL_RESOURCE_ATTRIBUTES`. Supply `cloud.region` only from verified deployment metadata; the notebook does not guess it.
- **Agent setup spans carry diagnostic metadata**: resolved model publisher/name/version/deployment, a deterministic SHA-256 configuration fingerprint, and allowlisted service/API Management request IDs when supplied by the response. Backend mode emits `sync_agent` or `resolve_agent` according to its version policy and records the verified identity and active version; project mode retains `create_agent`. None simulates Foundry service spans.
- **Persistence is run-correlated**: `persist_story` explicitly records the run, session and agent identity with `app.interaction=persistence`. Section 6 requires exactly one persistence span in addition to the story/facts/Sentinel response coverage.
- **Section 6 presents an observability report**, not truncated JSON: MAF workflow wall-clock duration, explicit executor roots, stage-by-stage coverage, agent/model/version metadata, content indexes, a joined span inventory, root-call latency trends, exception diagnostics and expandable current-run KQL. Parent/child ancestry assigns spans to stages even when story/facts/persistence share one trace; nested spans are not counted as additional interactions.
- **Content enriches spans rather than replacing them**: `AppGenAIContent` is joined by resource + trace + span after aggregation to prevent duplicate counts. The health gate remains span-based; missing content has its own status. Standard GenAI content moves out of legacy telemetry tables on September 30, 2026, so the report does not read those legacy content attributes.
- **Message previews are separately opt-in**: `SHOW_GENAI_CONTENT=True` in Section 6 requests/displays up to 1,200 characters per message/tool field, only when local content recording is enabled. Set it to `False` to show metadata and character counts without retrieving payload previews. Detail views cap at 200 rows; coverage counts are uncapped. Treat exception messages as potentially sensitive too.

Content capture is enabled by default for this controlled demo: prompts, responses and tool payloads may be exported to Application Insights, including sensitive Sentinel data. Set the environment variable to `false` before initialization when this is not appropriate. Restarting a previously initialized kernel is necessary to pick up the new default; an inherited explicit `false` still takes precedence. The policy is not a universal redaction filter: exception diagnostics and locally generated stories/decks can still contain personal data. Identical setup reruns reuse providers; changes to identity, backend or content policy require restarting the kernel.

See [observability.md](docs/observability.md) for the full environment variable reference,
version posture and design notes, and [OTEL-Agent-Spans.md](docs/OTEL-Agent-Spans.md)
for per-cell span inventories, code examples and validation evidence.

### MAF Workflow Boundaries

[notebook_support/workflow.py](notebook_support/workflow.py) uses the real MAF `WorkflowBuilder`
and function executors. Section 5 runs `story -> facts -> persistence`; Section
5.1 runs a separate `sentinel` workflow with the same `demo.run_id` but its own
trace. This preserves independent notebook execution and Sentinel's existing
OAuth/project connection. Set `RUN_SENTINEL_WORKFLOW=False` to skip it; an absent
Sentinel agent also skips explicitly. A configured Sentinel failure is never
treated as a skip.

Foundry agent definitions, endpoint routing/version policy, model selection,
MCP approvals and generated story/Marp formats are unchanged. A failed step
stops the workflow without automatic retries or checkpoint replay. Persistence
runs once per workflow execution, **not** once across manual cell reruns; use a
fresh kernel/run ID for a clean validation run. Native MAF executor spans are
the explicit interaction roots, while Foundry/HTTP spans remain descendants.

### MCP Tool Setup

Section 3.2 creates an `azure.ai.projects.models.MCPTool` with server label `msft-learn` and the Microsoft Learn endpoint. The query cells explicitly handle MCP approval requests, with a bounded approval loop. Running these cells authorizes those tool calls; review the prompts and connected tools first.

The notebook keeps the Microsoft Sentinel MCP dependency on the Foundry project-connection path by design. That Sentinel-specific setup remains separate from the public MCP tool used for Microsoft Learn. Sentinel resolves the workspace, then queries **`SigninLogs`** directly with **`IsInteractive == true`** and the signed-in `UserPrincipalName`, selecting the latest event by `TimeGenerated`. The shared system/user instructions supply the schema and an exact KQL template, and prohibit `search_tables` or fallback table discovery. Plain KQL and raw output columns avoid formatting and alias errors.

The [SigninLogs schema](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/signinlogs) uses a boolean `IsInteractive`, `UserPrincipalName` for identity, `AppDisplayName` for the application, and the dynamic `LocationDetails` object for city/state/country. `Location` supplies a country-code fallback. This is an explicit SDL table selection, not an Advanced Hunting query; no Microsoft Graph permission or custom MCP collection is required. The query returns the latest interactive event, not only successful sign-ins.

To reproduce the lookup in SDL, use the same workspace and identity as the MCP connection, replacing the example UPN:

```kusto
SigninLogs
| where IsInteractive == true
| where UserPrincipalName =~ 'user@your-domain.example'
| top 1 by TimeGenerated desc
| project TimeGenerated, UserPrincipalName, IsInteractive, IPAddress,
          Location, LocationDetails, AppDisplayName, ResourceDisplayName
```

The selected SDL workspace must expose `SigninLogs` to the connected identity. If the table or required columns are unavailable, the error is surfaced rather than switching tables or treating it as an empty result. Rerun Section 4 to apply changed instructions in project or backend **sync** mode, then Section 5.1 in a new conversation. Backend **pinned** mode still requires a separate tested release and local version update. The existing Sentinel user-passthrough connection is retained; see [validation evidence](docs/observability.md#direct-sentinel-table-routing--2026-09-15).

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
| Foundry or Azure Monitor import errors | Install the Windows runtime in **Section 1**, check `pip check`, then restart the kernel; install optional shared requirements only if another demo needs them |
| Signed-in account shows as unavailable | Rerun **Section 3 — Configure the Project Client** (uses `az.cmd` on Windows) |
| Telemetry cell fails after dependency changes | Restart kernel, rerun from **Section 1** through **Section 3.1** |
| Telemetry configuration changed or partial setup failed | Restart the kernel and rerun in order; do not reset the initialization flags to bypass the guard |
| Sentinel cell cannot resolve a project connection | Verify the Foundry project still contains the Sentinel MCP connection and rerun **Section 3.3** |
| Sentinel `query_lake` returns KQL validation errors | Check the specialist's supplied-table and plain-KQL instructions. Rerun Section 4 in project/sync mode, or explicitly release a candidate in strict pinned mode. Do not treat a tool error as a successful answer. |
| Backend definition differs | Use the demo's `sync` policy to create/reuse and activate the matching version. Strict `pinned` policy deliberately requires a separate release. |
| Version activated but local save failed | Fix the reported build-file problem and rerun sync. It reads the actual endpoint and repairs the checkpoint without duplicating the version. |
| `SigninLogs` cannot be resolved | Verify the table in Data lake exploration with the same workspace and identity used by MCP. The sign-in demo explicitly targets `SigninLogs` and does not call `search_tables` or substitute another table. |
| Section 6 reports missing telemetry | Confirm workspace query permissions, exporter connectivity and the current run ID; the cell waits up to 3 minutes in total for span ingestion and fails rather than accepting historical/empty results |
| Section 6 reports failed spans after a successful retry | Expand failed spans and correlated exceptions; one failed operation can create several failed spans. Earlier attempts sharing the same run ID remain in the strict gate. Restart the kernel and rerun the runtime cells for a new run ID; do not disable the failure check |
| Span health passes but content is waiting/not recorded | Content availability is independent of span health. Check the local content policy, service-side capture, table permissions and ingestion; rerun Section 6 to refresh. No content does not mean an empty answer |
| `AppGenAIContent` query is denied or the table is unavailable | Obtain appropriate read access, including protected-table access when configured, or verify content routing. Errors remain explicit; the notebook does not silently fall back to legacy content attributes |

---

## ✅ Validation Checklist

- [ ] **Section 3** prints `🔐 Credential used: ...` and `👤 Signed-in account: ...`
- [ ] **Section 3.1** reports MAF workflow tracing and HTTPX2 enabled, 100% sampling, the intended content policy, and disabled log/metric/Live Metrics/performance-counter export
- [ ] **Section 3.2** prints the [MSFT Learn MCP URL](https://learn.microsoft.com/api/mcp)
- [ ] **Section 3.3** resolves or prints the Sentinel MCP project connection details
- [ ] **Section 4** configures both Foundry project agents for a full run
- [ ] **Section 5** runs the story/facts/persistence MAF workflow, returns responses and appends once to `stories.json`
- [ ] **Section 5.1** runs the Sentinel MAF workflow when enabled/configured, or prints an explicit skip
- [ ] **Section 6** reports required MAF workflows/executors, current-run interaction coverage, response dependencies, zero failed spans, and the expected service/version
- [ ] **Section 6 content report** links the available snapshots without increasing the span count, reports input/output availability separately, and keeps sensitive previews hidden unless explicitly requested

---

## 📚 References

- [Microsoft Agent Framework (Python)](https://github.com/microsoft/agent-framework)
- [Microsoft Agent Framework Observability Samples](https://github.com/microsoft/agent-framework/tree/main/python/samples/02-agents/observability)
- [Microsoft Foundry SDK Overview (Python)](https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/sdk-overview?pivots=programming-language-python#foundry-tools-sdks)
- [OpenTelemetry for Python: Instrumentation Guide](https://opentelemetry.io/docs/languages/python/instrumentation/)
- [Azure MCP Server Documentation](https://learn.microsoft.com/azure/developer/azure-mcp-server/)
- [Foundry client-side tracing (preview)](https://learn.microsoft.com/azure/foundry/observability/how-to/trace-agent-client-side)
- [Windows dependency matrix](requirements/requirements-notebook.txt)
- [Observability notes and validation evidence](docs/observability.md)
- [Change history](CHANGELOG.md)
