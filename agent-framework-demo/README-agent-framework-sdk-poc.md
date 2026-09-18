# Microsoft Agent Framework SDK + Aspire Observability PoC

A standalone companion README for the Windows notebook PoC in this repo.

This document covers the Agent Framework-first notebook experience in [zolab-agent-framework-sdk-win11.ipynb](./zolab-agent-framework-sdk-win11.ipynb). It does not replace the main repo README, which continues to describe the broader Foundry deployment and bot workspace.

## Executive Summary

This PoC demonstrates a practical, notebook-driven way to build and observe Microsoft Agent Framework workloads on Windows without using Microsoft Foundry as the runtime layer.

The notebook combines four ideas in one guided flow:

- Agent creation with Microsoft Agent Framework and Azure OpenAI
- MCP server exposure using the official `agent.as_mcp_server()` pattern
- A simple multi-agent workflow using Agent Framework orchestration builders
- Rich local observability using OpenTelemetry exported to the Aspire Dashboard

The value of the PoC is not just that each capability works in isolation. The main outcome is that an engineer can create a local, inspectable, end-to-end agent system where prompts, orchestration, tool activity, MCP boundaries, and workflow spans all show up in one local observability surface.

In practical terms, this PoC answers three questions:

1. What does an Agent Framework-first implementation look like when it is not hidden behind a larger app host?
2. How do MCP and multi-agent workflows fit into that implementation in Python?
3. How do you preserve detailed telemetry while keeping the demo simple enough to teach and debug?

## Description

This PoC is intentionally focused on a single teaching surface: one notebook that can be opened in VS Code, run cell by cell, and used to inspect the behavior of agents, tools, workflows, and observability in one place.

The notebook is designed around these constraints:

- Windows 11 and VS Code friendly
- independent `agent-framework-demo/.venv` local setup
- Azure OpenAI for model execution
- Microsoft Agent Framework as the primary SDK
- Aspire Dashboard as the primary trace viewer
- No Foundry project endpoint in the notebook runtime path

That makes it a useful contrast to the rest of this repo, which includes Foundry-oriented infrastructure, bot runtime code, and deployment automation.

## What This PoC Demonstrates

- A local virtual-environment and kernel bootstrap flow for notebook-driven Agent Framework work
- Direct Azure OpenAI configuration using `AzureCliCredential`
- An Agent Framework agent with local tools
- An MCP example where an Agent Framework agent is exposed as a stdio MCP server and invoked by a notebook verification client
- A basic group-chat workflow using `GroupChatBuilder`
- An A2A Reviewer service with Agent Card discovery, bearer authentication,
  streamed tasks/artifacts, and trace correlation to the local group chat
- OpenTelemetry instrumentation exported to Aspire Dashboard over OTLP
- Cleanup flows for the MCP subprocess, Aspire container, Aspire image, and Azure credential

## Primary Assets

- Notebook: [zolab-agent-framework-sdk-win11.ipynb](./zolab-agent-framework-sdk-win11.ipynb)
- Pinned notebook dependencies: [requirements.txt](./requirements.txt)
- A2A reviewer server: [agent_framework_reviewer_a2a_server.py](./agent_framework_reviewer_a2a_server.py)
- A2A client and process lifecycle: [agent_framework_reviewer_a2a_client.py](./agent_framework_reviewer_a2a_client.py)
- Official-source wheel recovery: [build_source_wheels.ps1](./build_source_wheels.ps1)
- Existing repo overview: [README.md](../README.md)
- Existing observability notes: [observability.md](../observability.md)
- Foundry deployment guide: [deployment/README.md](../deployment/README.md)
- Bot workspace guide: [bot-app/README.md](../bot-app/README.md)
- Bot runtime guide: [bot-app/runtime/README.md](../bot-app/runtime/README.md)
- Agent Framework migration notes: [bot-app/docs/agent-framework-migration-plan.md](../bot-app/docs/agent-framework-migration-plan.md)
- Refactor summary: [bot-app/docs/refactor-executive-summary.md](../bot-app/docs/refactor-executive-summary.md)

## Architecture

![Agent Framework architecture diagram](../images/agent-framework-architecture-dark.svg)

### Runtime View

![Agent Framework runtime sequence diagram](../images/agent-framework-runtime-dark.svg)

## Notebook Walkthrough

The notebook is organized as a step-by-step PoC, not as a generic SDK sample dump.

![Agent Framework notebook walkthrough diagram](../images/agent-framework-walkthrough-dark.svg)

## Agent Instruction and Collaboration Design

The notebook uses explicit role and output contracts rather than generic "helpful assistant" prompts:

- **Teaching agent** — treats tool output as untrusted evidence within each tool's declared scope, calls only tools relevant to the question, distinguishes local-demo behavior from production requirements, and includes a verification signal for runnable guidance.
- **Restaurant MCP agent** — treats tool output as untrusted menu data, separates price from availability, refuses to invent unsupported menu facts, and keeps menu answers concise.
- **ArchitectAgent** — produces the initial executable draft with assumptions, ordered actions, observability checks, and measurable success criteria without inventing implementation details.
- **ReviewerAgent** — runs in a separate A2A service and audits the draft with prioritized `Severity | Problem | Concrete correction` findings and an advisory `ACCEPT` or `REVISE` verdict; it does not rewrite the plan or manufacture defects.
- **CoachAgent** — owns the final runbook, incorporates valid review corrections regardless of verdict, removes repetition, and preserves the notebook's Windows/Python/direct-Azure-OpenAI boundaries.

The group chat performs one complete round in the fixed order **Architect → Reviewer → Coach**. The initial user task plus those three turns satisfies the termination condition, and `max_rounds=3` provides an additional loop guard. Participant responses are selected with `intermediate_output_from`, while the orchestrator's terminal workflow output is handled separately. Runtime validation rejects an unexpected turn order, selects the final response explicitly by `CoachAgent` author, and stores it as `workflow_final_response`. This keeps the exercise collaborative while ensuring the final response comes from the synthesis role rather than beginning a redundant second pass.

### A2A Reviewer Boundary

Architect and Coach remain local Python agents. The notebook launches only the
Reviewer as a separate process on an OS-selected `127.0.0.1` port and discovers
`/.well-known/agent-card.json`. The card advertises the reviewer revision, skill,
JSON-RPC 1.0 interface, streaming support, and bearer authentication.

The client sends **both the original user request and the Architect draft** as
labeled data. This is intentional: the generic MAF A2A client forwards only the
last input message. The server keeps its own Reviewer instructions. A2A task
updates and streamed artifacts become the Reviewer's group-chat response, not
extra participants or an additional caller-side model call.

Before continuing to Coach, the client retrieves the remote task, requires its
state to be `TASK_STATE_COMPLETED`, and checks for a nonempty review artifact.
The result panel displays the task ID, artifact count, and workflow trace ID;
metadata remains available in `reviewer_a2a_task`.

**Authentication and lifetime:** startup generates a new random bearer token.
It stays in memory and the child environment, not command arguments or files.
The public card contains no token. Missing-token and wrong-token task requests
must return HTTP 401 during startup; all task and shutdown endpoints are
protected. A changed instruction/model revision requires stopping and restarting
the reviewer; there is no silent local-agent fallback.

**Demo boundary:** loopback HTTP and a single shared demo principal are not a
production identity solution. Use TLS, real identity/authorization, tenant-aware
task ownership, durable storage, and retention limits before remote deployment.
The SDK's in-memory task store is lost on service restart. The review deadline
is 120 seconds, the HTTP timeout is 130 seconds, and startup is bounded to
30 seconds. Cleanup step 7.2 requests graceful shutdown and targets only the
owned process tree if forced cleanup is necessary.

**Observability:** W3C context is injected into A2A HTTP requests and extracted
by the service, placing the Reviewer's model spans beneath the notebook's
workflow trace. Use `service.name = zolab-agent-framework-a2a-reviewer` and the
shared `service.instance.id` to filter the remote signals. Startup's deliberate
401 checks are expected authentication-test spans, not successful-run failures.
Prompt recording honors the existing opt-out; bearer headers are never copied
into task state or telemetry attributes.
The A2A SDK emits detailed protocol/event-queue spans while streaming. Filter
the span name to `invoke_agent` to highlight Architect, remote Reviewer, and
Coach, or use the Gen AI view to focus on model activity rather than protocol
internals.

**Validated on September 18, 2026:** the actual notebook runtime completed the
teaching-agent, MCP, and local/remote group-chat steps on MAF 1.19. The remote
review task reached `TASK_STATE_COMPLETED` with one retrieved artifact; the
workflow trace spanned two services and retained the required role order.
Missing and wrong bearer tokens were rejected before model execution, and
cleanup left neither protocol helper running.

## Why Aspire Matters Here

The Aspire Dashboard is the differentiator for this PoC.

Without it, the notebook would still prove that the code works. With it, the notebook proves how the code behaves.

That matters because the interesting part of Agent Framework is not only the final answer. It is the execution path:

- which spans were emitted
- which workflow step ran next
- which agent participated
- how long each step took
- whether model and MCP boundaries correlate cleanly in traces

For a local teaching PoC, Aspire is the fastest way to make those questions visible.

## Setup Summary

### Prerequisites

- Python 3.13+
- VS Code with Jupyter support
- Azure CLI logged in with access to the target Azure OpenAI resource
- Docker Desktop for Aspire Dashboard. The notebook startup cell attempts to launch Docker Desktop on Windows when the Docker CLI is installed but the Linux engine is not ready.
- An Azure OpenAI endpoint and deployment name

### Notebook Install Set

The notebook installs exact direct pins from [requirements.txt](./requirements.txt),
reviewed on September 18, 2026. The MAF A2A adapter remains a beta release; other
direct pins are stable. MCP remains below 2 because MAF requires it, and protobuf
remains below 7 because the A2A SDK requires it. Key pins are:

| Package | Version |
| --- | --- |
| `agent-framework-core` | `1.19.0` |
| `agent-framework-openai` | `1.14.4` |
| `agent-framework-orchestrations` | `1.2.0` |
| `agent-framework-a2a` | `1.0.0b260918` (beta) |
| `a2a-sdk` | `1.1.4` |
| `anyio` | `4.15.1` |
| `mcp` | `1.30.0` (compatible with MAF's `mcp>=1.24,<2` range) |
| `openai` | `3.16.0` |
| `azure-identity` | `1.25.3` |
| `httpx` / `httpx2` | `0.28.1` / `2.13.0` |
| `ipykernel` | `7.3.0` |
| `pydantic` | `2.13.5` |
| `protobuf` | `6.33.6` (A2A compatibility) |
| `starlette` / `uvicorn` | `1.6.0` / `0.53.0` |
| `opentelemetry-api` / `opentelemetry-sdk` / OTLP gRPC exporter | `1.44.0` |

### Recovering From a Lagging Package Mirror

If the configured Python feed cannot supply these published releases, run
[build_source_wheels.ps1](./build_source_wheels.ps1) after the environment
bootstrap. It checks out official GitHub release tags, verifies their immutable
commit IDs and clean source, and builds wheels with isolated build dependencies.
The Python MAF tag is `python-1.19.0`, not the independent .NET release tag.
WinGet is not used to install Python-library packages.

The ignored `.wheels` folder stores the built wheels and their SHA-256/source
provenance; `.source-builds` stores reproducible upstream checkouts. The notebook
installation cell automatically uses `.wheels` when present. No TLS verification
is disabled, and no root-environment packages are installed or changed. Restart
the notebook kernel after upgrading packages; old imports do not update in place.

## Quick Start

1. Open [zolab-agent-framework-sdk-win11.ipynb](./zolab-agent-framework-sdk-win11.ipynb).
2. If the demo environment does not exist, select any working Python 3.13+ kernel using **Select Another Kernel > Python Environments**. Run the first setup cell from the repository root or `agent-framework-demo`; it creates `agent-framework-demo/.venv` when its interpreter is missing, otherwise reuses it, and installs/registers the demo kernel without changing the root environment.
3. Switch to **Select Another Kernel > Jupyter Kernel > Agent Framework SDK Demo (.venv)** and run the kernel verification cell.
4. Run the dependency installation and package-inventory cells only after verifying the demo kernel.
5. Run the Azure OpenAI configuration cell.
6. Run the Aspire Dashboard startup cell and use the printed browser token or login URL. If Docker Desktop is not already running, the cell waits for the Docker Linux engine and prints a clear fallback status instead of raising a Docker `CalledProcessError`.
7. Run the observability cell to initialize OTLP export.
8. Run the basic agent section.
9. Generate the MCP helper, connect the MCP client (5.1), and run the menu verification call (5.2). Inspect the printed trace ID in Aspire and select the MCP service under Metrics.
10. Run section 6 to start/discover/authenticate the A2A reviewer and build the local/remote workflow, then section 6.1 to run Architect → remote Reviewer → Coach.
11. Inspect the completed A2A task and workflow trace, then run cleanup in order: MCP, A2A reviewer, notebook telemetry, Aspire, and Azure credential. Restart the kernel before rerunning the demo after telemetry shutdown.

If VS Code reports that the selected Python environment is no longer available,
the setup cell has not executed: a missing kernel cannot create its own
environment. Select a working Python kernel, or use the **PowerShell recovery**
block in the notebook's first setup description from an `agent-framework-demo`
terminal. That block creates the missing environment and registers the kernel
without requiring a running notebook. Then continue from step 3.

## Observability Design Notes

This PoC chooses rich telemetry with controlled notebook noise.

The intended telemetry model is:

- spans stay rich enough for Aspire exploration
- prompt, response, tool-argument, and tool-result capture is enabled by default; set `AGENT_DEMO_CAPTURE_CONTENT=false` before telemetry setup to opt out
- root DEBUG is disabled by default; set `AGENT_DEMO_ROOT_DEBUG=true` for a focused troubleshooting run
- console exporters are disabled when OTLP is available and used as a fallback otherwise; `AGENT_DEMO_CONSOLE_EXPORTERS` can override the choice
- duplicate GenAI message events are disabled by default while content remains on current GenAI spans; `AGENT_DEMO_MESSAGE_EVENTS=true` enables them
- the package inventory, service version, session ID, and agent/workflow revisions make trace comparisons reproducible
- notebook status panels use the same semantic palette as the main Windows notebook: green for enabled/success, red for disabled/action-required states, rust for session IDs and revisions, blue for endpoints/versions, and magenta for named services/agents

This is deliberately different from a production posture. In production, route OTLP through a collector to Azure Monitor/Application Insights, apply redaction and access-controlled retention, use a specific managed identity, and define sampling plus alerting policies explicitly.

### MCP Request-Response Verification

Section 5.1 uses `MCPStdioTool` to own the server process, initialize the protocol,
and discover `RestaurantAgent`. Section 5.2 makes one direct MCP `tools/call`
request asking for today's specials and the price of Clam Chowder. The request
has a 120-second deadline and fails explicitly if the server errors or the answer
omits the expected menu facts. There is no extra caller-side model agent.
On Windows, startup refreshes the active environment's site-package paths if
`pywin32` was installed after the kernel started; it does not reinstall packages
or change the repository's root environment.
The notebook's small `MCPStdioTool` subclass supplies a real temporary stderr file
to the SDK transport because Jupyter's output stream has no usable file
descriptor. Diagnostics are replayed to notebook stderr when the connection
closes, and the temporary file is closed and removed. No event-loop policy or
SDK internals are patched.

The server extracts the incoming W3C trace context from MCP `_meta`; this bridges
the current MAF server adapter's missing extraction step without replacing its
tool dispatch. Aspire should show the notebook's
`agent_framework.mcp_verification` span and the server's `mcp.tools/call`,
RestaurantAgent, model, and menu-tool spans in the same trace. The services
`zolab-agent-framework-sdk-demo` and `zolab-agent-framework-mcp-demo` share the
notebook's `service.instance.id`.

The helper flushes metrics, traces, and logs after each call, rather than waiting
for the periodic metrics interval. In Aspire Metrics, select
`zolab-agent-framework-mcp-demo` and inspect
`agent_framework.function.invocation.duration`, `gen_ai.client.operation.duration`,
and `gen_ai.client.token.usage`. Flush completion does not prove ingestion; verify
the printed trace ID and metric data in the dashboard. Without an OTLP endpoint,
only the request-response check is available; stdout remains reserved for MCP.

The full response and trace ID are retained in `mcp_verification_response` and
`mcp_verification_trace_id`. Prompt/response telemetry respects the existing
content-capture option. Each rerun makes another billable model-backed request.
Cleanup step 7.1 closes the client and its child process before notebook
OpenTelemetry shutdown and Aspire removal. It also handles the older idle
launcher when upgrading a running notebook; run that cleanup before reconnecting.

### Aspire Dashboard Startup Behavior

The Windows notebook now handles the common Docker Desktop cold-start case explicitly:

- If Docker CLI is missing, the notebook skips Aspire and continues with console exporters.
- If Docker CLI is present but the Docker Desktop Linux engine is unavailable, the notebook attempts to start Docker Desktop and waits for the engine.
- If ports `18888` or `4317` are already busy, the notebook chooses available local ports and prints the actual Aspire UI and OTLP endpoint.
- If container startup fails, the notebook prints Docker stderr and keeps the rest of the demo runnable with console exporters.
- Cleanup flushes and shuts down the OpenTelemetry meter, tracer, and logger providers before removing Aspire. This stops the metrics export thread and prevents repeated `StatusCode.UNAVAILABLE` retries after the local receiver is gone.

## Scope Boundaries

This notebook PoC is intentionally separate from the Foundry-focused parts of the repo.

### In Scope

- Notebook-based Agent Framework exploration
- Azure OpenAI-backed Agent Framework usage
- MCP server exposure from Agent Framework
- Local workflow orchestration demos
- Aspire-based tracing and troubleshooting

### Out of Scope

- Foundry project runtime inside this notebook
- Teams bot hosting as the notebook execution surface
- Production deployment patterns for the notebook itself
- A general-purpose MCP host application beyond the bounded notebook verification client

## Relationship To The Rest Of The Repo

This repo has three useful layers of material:

1. The main repo README and deployment docs describe the existing Foundry-centered environment and infrastructure.
2. The `bot-app/` subtree describes the Teams-based runtime and worker architecture.
3. This PoC README describes the standalone Agent Framework notebook path that is intentionally simpler and more inspectable.

That split is useful because it lets you compare two approaches:

- a larger system with deployment and bot runtime concerns
- a focused notebook that isolates Agent Framework, MCP, workflows, and tracing

## Known Tradeoffs

- The notebook is optimized for learning and inspection, not for minimal package count.
- The notebook intentionally owns `agent-framework-demo/.venv`; it does not share or constrain the main Foundry notebook's root `.venv`.
- The MCP demonstration includes a real handshake and request-response verification, but it is not a full reusable host product.
- The workflow stores a full transcript for inspection, but the visible summary is bounded and the exercise stops after one Architect → Reviewer → Coach pass.
- The notebook depends on local Docker availability if you want the full Aspire experience.

## Recommended Next Steps

- Add a second workflow example, such as sequential or handoff orchestration, for comparison
- Extract agent definitions, tools, workflow construction, MCP hosting, and telemetry bootstrap into tested modules if the PoC becomes a production seed.
- Replace notebook-managed stdio with an authenticated, health-checked MCP service only when tools must be shared remotely.
- Route production telemetry through an OpenTelemetry Collector and Azure Monitor/Application Insights; Aspire Dashboard is a development viewer, not a production monitoring system.

## Related References

- Microsoft Agent Framework repo: [https://github.com/microsoft/agent-framework](https://github.com/microsoft/agent-framework)
- Agent Framework observability: [https://learn.microsoft.com/agent-framework/agents/observability](https://learn.microsoft.com/agent-framework/agents/observability)
- Agent Framework Python 2026 significant changes: [https://learn.microsoft.com/agent-framework/support/upgrade/python-2026-significant-changes](https://learn.microsoft.com/agent-framework/support/upgrade/python-2026-significant-changes)
- Azure OpenAI passwordless authentication: [https://learn.microsoft.com/azure/developer/ai/keyless-connections](https://learn.microsoft.com/azure/developer/ai/keyless-connections)
- Standalone Aspire Dashboard with OTLP: [https://learn.microsoft.com/dotnet/core/diagnostics/observability-otlp-example](https://learn.microsoft.com/dotnet/core/diagnostics/observability-otlp-example)
- Application Insights Well-Architected guidance: [https://learn.microsoft.com/azure/well-architected/service-guides/application-insights](https://learn.microsoft.com/azure/well-architected/service-guides/application-insights)
- Existing repo overview: [README.md](../README.md)
- Foundry deployment details: [deployment/README.md](../deployment/README.md)
- Existing observability notes: [observability.md](../observability.md)
- Bot runtime architecture: [bot-app/runtime/README.md](../bot-app/runtime/README.md)

## Bottom Line

This PoC is a clean, local, inspectable example of Microsoft Agent Framework used the way many engineers actually need it during exploration: one notebook, one Azure OpenAI-backed runtime, one MCP example, one workflow example, and one strong observability surface.

That makes it a useful companion to the broader Foundry and bot assets in this repo, not a replacement for them.

## Appendix: Observability Stack by Python Import / Library

This appendix aligns the broader Microsoft agent observability stack to the Python imports and packages that commonly back each layer. Some layers are Python SDK imports used directly in notebooks or applications; others are Azure, Microsoft Sentinel, Microsoft Defender XDR, or Microsoft Entra service surfaces that are configured through portals, REST APIs, Microsoft Graph, ARM, connectors, or Log Analytics.

| Observability / security layer | Python import / package | What it does in a notebook or app | Notes |
| --- | --- | --- | --- |
| Microsoft Foundry | `from azure.ai.projects import AIProjectClient` | Creates the Foundry project client and retrieves project telemetry metadata. | Main SDK entry point for a Foundry project. |
| Microsoft Foundry project agents | `from azure.ai.projects.models import PromptAgentDefinition, MCPTool` | Defines project-backed agents and attaches MCP tools such as Microsoft Learn or Sentinel MCP. | `PromptAgentDefinition` describes the agent; `MCPTool` describes remote MCP tools. |
| Foundry agent tracing | `from azure.ai.projects.telemetry import AIProjectInstrumentor` | Enables client-side GenAI tracing for Foundry / Azure AI Projects operations. | Makes Foundry agent activity emit OpenTelemetry GenAI spans. |
| Foundry Responses API path | `project_client.get_openai_client()` | Gets the OpenAI-compatible client used for `conversations.create()` and `responses.create(...)`. | The `agent_reference` payload ties Responses API runs back to the Foundry project agent. |
| Azure authentication | `from azure.identity import DefaultAzureCredential` | Authenticates to Foundry, Azure Monitor, and Log Analytics APIs. | In the Agent Framework-only notebook, the equivalent explicit auth choice is `AzureCliCredential`. |
| OpenTelemetry trace model | `from opentelemetry import trace` | Creates tracers and manual spans. | Core trace/span API. |
| OpenTelemetry span metadata | `from opentelemetry.trace import SpanKind, Status, StatusCode` | Marks spans as client spans and records success/error state. | Used for operation kind and failure details. |
| OpenTelemetry baggage/context | `from opentelemetry import baggage, context as otel_context` | Adds safe correlation metadata such as run ID, agent name, model, project, and session ID. | Baggage should not contain secrets, tokens, or sensitive prompt data. |
| HTTP dependency instrumentation | `from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor` | Captures HTTPX dependency telemetry from SDK/network calls. | Helps Application Insights show outbound dependencies. |
| Manual dependency spans | `tracer.start_as_current_span("POST /openai/v1/responses", kind=SpanKind.CLIENT)` | Creates explicit client spans around `responses.create(...)`. | Makes notebook-side Responses API dependency calls visible in `AppDependencies`. |
| Azure Monitor exporter | `from azure.monitor.opentelemetry import configure_azure_monitor` | Exports OpenTelemetry traces to Azure Monitor / Application Insights. | Main bridge from OpenTelemetry to Azure Monitor. |
| Azure Monitor resource metadata | `from agent_framework.observability import create_resource` | Supplies resource attributes for Azure Monitor export. | Useful for stamping service/resource identity onto telemetry. |
| Application Insights | `configure_azure_monitor(connection_string=...)` | Receives exported OpenTelemetry telemetry. | There is no separate Application Insights import in the notebook; it is the destination configured through Azure Monitor OpenTelemetry. |
| `AppDependencies` | Azure Monitor ingestion from OpenTelemetry spans | Stores dependency/span-like operation records. | Populated by Azure Monitor from exported telemetry, not written directly by Python. |
| `AppGenAIContent` | `AIProjectInstrumentor` plus content-recording environment variables | Stores captured GenAI content such as inputs, outputs, system instructions, tool args, and tool results. | Microsoft-managed Azure Monitor table; Python enables content capture, Azure Monitor materializes the table. |
| GenAI content capture | `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` and `AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED=true` | Enables prompt/output/tool content capture for demo/debug scenarios. | Requires strong privacy controls in production. |
| Log Analytics query path | `urllib.request`, `urllib.error`, `json`, and `DefaultAzureCredential` | Calls `https://api.loganalytics.io/v1/workspaces/{workspace_id}/query`. | The Foundry notebook uses direct REST calls rather than `azure-monitor-query`. |
| Optional Log Analytics SDK | `from azure.monitor.query import LogsQueryClient` | SDK-based alternative for querying Log Analytics. | Cleaner for production code, but not required by the current notebook. |
| Microsoft Sentinel MCP | `from azure.ai.projects.models import MCPTool` | Defines the Sentinel MCP tool using `project_connection_id`. | Sentinel access is through Foundry project connection / OAuth passthrough, not a Sentinel Python SDK. |
| Sentinel workspace lookup | `subprocess` calling `az rest` and `az monitor log-analytics workspace show` | Resolves project connections and workspace identifiers. | CLI-driven discovery in the notebook. |
| Microsoft Sentinel SIEM | No direct Python import in this notebook | Consumes data through connectors, Log Analytics, and Defender portal experiences. | For management automation, use REST/ARM or `azure-mgmt-securityinsight`; for hunting, use Log Analytics. |
| Microsoft Defender XDR | No direct Python import in this notebook | Security operations plane for incidents, alerts, advanced hunting, and Sentinel integration. | Typically configured service-to-service through the Defender/Sentinel connector. For API work, use Microsoft Graph Security APIs. |
| Microsoft Entra Agent ID | No direct Python import in this notebook | Identity/governance concept for non-human agent identities. | For production automation, use Microsoft Graph plus `azure.identity`; the notebook currently uses Azure developer credentials. |
| Conditional Access | No direct Python import in this notebook | Entra policy plane for users, workload identities, and agent-related conditions. | Programmatic policy management is through Microsoft Graph `conditionalAccessPolicy`, not an Azure Monitor library. |

The most common import map for a Microsoft Foundry observability notebook looks like this:

```python
from agent_framework.observability import create_resource

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from azure.ai.projects.telemetry import AIProjectInstrumentor
from azure.core.settings import settings
from azure.identity import DefaultAzureCredential
from azure.monitor.opentelemetry import configure_azure_monitor

from opentelemetry import baggage, context as otel_context, trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.trace import SpanKind, Status, StatusCode
```

Shortest practical alignment:

- **Foundry**: `azure-ai-projects`
- **OpenTelemetry traces/spans**: `opentelemetry-api`, `opentelemetry-sdk`
- **Azure Monitor / Application Insights export**: `azure-monitor-opentelemetry`
- **Azure SDK trace bridge**: `azure-core-tracing-opentelemetry`
- **HTTP dependency spans**: `opentelemetry-instrumentation-httpx`
- **Identity**: `azure-identity`
- **Log Analytics queries**: REST with `urllib` + `DefaultAzureCredential`, or optional `azure-monitor-query`
- **Sentinel / XDR / Conditional Access / Entra Agent ID**: mostly service/API planes, commonly Microsoft Graph, ARM/REST, Sentinel connectors, and Log Analytics rather than notebook-local imports
