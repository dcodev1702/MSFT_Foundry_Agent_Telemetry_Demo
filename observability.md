# Windows Notebook Observability and Validation

This document describes Sections 3.1, 3.3, 5, 5.1 and 6 of the [Windows notebook](zolab-ai-agent-demo-win11.ipynb). Dependency review date: **2026-09-14**. Agent execution uses Azure AI Projects and the Foundry Responses API, with native OpenTelemetry resource metadata. Agent Framework is not a runtime dependency of this notebook.

The notebook exports client-side traces to Azure Monitor through the Foundry project's Application Insights connection. Foundry instrumentation supplies GenAI spans, while explicit notebook-side HTTP dependency spans preserve Service Map edges across transport-library changes.

## Observability Continued

The highest-value enhancements that are now applied are:

1. Enforce trace-only export using controls honored by the installed Azure Monitor distro.
2. Enable baggage propagation for the notebook's safe correlation keys.
3. Add an explicit content-recording policy instead of relying on defaults.
4. Harden initialization order for notebook reruns in a reused kernel.
5. Keep custom span metadata in place for agent, run, and interaction correlation.

## What 3.1 & 3.3 Turns On Today

Sections 3.1 and 3.3 in [zolab-ai-agent-demo-win11.ipynb](zolab-ai-agent-demo-win11.ipynb) enable the following:

| Capability | Current behavior in 3.1 | Why it matters |
| --- | --- | --- |
| OpenTelemetry as the Azure SDK tracing backend | `settings.tracing_implementation = "opentelemetry"` | Makes Azure SDK operations emit spans through OTEL instead of using no-op tracing. |
| Azure Monitor export | `configure_azure_monitor(...)` with the project's Application Insights connection string | Sends notebook traces to Azure Monitor so they land in Application Insights and Log Analytics. |
| Foundry client-side tracing | `AIProjectInstrumentor().instrument(...)` with explicit content, trace-context and baggage booleans | Emits client-side GenAI spans with the same content policy as custom notebook spans. |
| HTTP dependency tracing | Azure Monitor auto-instruments HTTPX and HTTPX2 when installed | The HTTPX instrumentation 0.65b0 package contains both instrumentors. OpenAI 3.x uses HTTPX2; no second package or manual re-wrapping is needed. |
| Explicit Responses API dependency spans | Manual `POST /openai/v1/responses` client spans later in the notebook | Ensures Azure Monitor has concrete dependency rows that correlate cleanly in Service Map and KQL. |
| Custom notebook orchestration spans | Manual spans such as `create_agent`, `invoke_agent`, `persist_story`, and Sentinel-specific spans | Makes the notebook's orchestration layer observable rather than only the SDK internals. |
| Resource identity | Native `Resource.create(attributes)` with explicit service, session, environment and project values | Preserves existing identity and additional `OTEL_RESOURCE_ATTRIBUTES`; adds `deployment.environment.name` alongside the legacy environment attribute. |
| GenAI semantic conventions | Owned by Azure AI Projects' installed preview instrumentor | The Agent Framework `gen_ai_latest_experimental` opt-in does not select the Projects SDK's schema and is no longer set here. |
| Baggage propagation | `AZURE_TRACING_GEN_AI_TRACE_CONTEXT_PROPAGATION_INCLUDE_BAGGAGE=true` | Lets the notebook's run, agent, and interaction baggage keys flow with downstream trace context. |
| Explicit content-recording policy | One strict `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` boolean, default `true` for this demo | SDK and custom content agree. Prompts, responses and tool payloads may contain sensitive data; set the flag to `false` before initialization to opt out. The obsolete Azure flag has no effect. UPN is not propagated in baggage. |
| Safer kernel reruns | Identical configurations reuse the provider; changed identity/backend/content or partial failure requires a restart | Prevents duplicate exporters and misleading status after configuration changes. |
| Trace-only local export posture | `OTEL_LOGS_EXPORTER=none`, `OTEL_METRICS_EXPORTER=none`, `enable_live_metrics=False`, `enable_performance_counters=False` | Unlike the former `disable_logging`/`disable_metrics` arguments, these controls take effect in Azure Monitor 1.8.10. |

## How the Telemetry Flows

The telemetry path for this repo is:

1. The notebook creates spans through OpenTelemetry, Azure SDK instrumentation, HTTPX/HTTPX2 instrumentation, and explicit custom spans.
2. `configure_azure_monitor(...)` registers Azure Monitor exporters for the signals that remain enabled.
3. The notebook retrieves the Application Insights connection string from the Foundry project at runtime by calling `project_client.telemetry.get_application_insights_connection_string()`.
4. Azure Monitor sends the exported trace data to Application Insights.
5. Because the Application Insights instance is workspace-based, the same telemetry is queryable in Log Analytics. Dependency spans are in `AppDependencies`; captured standard GenAI content is routed to `AppGenAIContent` and correlated by trace/span identifiers.
6. Agent calls use stable per-agent Responses endpoints in backend mode, or the project Responses API with `agent_reference` in explicit legacy mode. Client/service GenAI spans support the Foundry Traces view. Portal rendering is a separate UI check from the Log Analytics assertions.

![Pro-code observability stack for the Foundry agent demo](images/foundry-observability-stack.svg)

The diagram above summarizes the same pro-code path visually: notebook orchestration creates explicit spans, Foundry and HTTP client instrumentation enrich the agent and dependency traces, and Azure Monitor exports the resulting telemetry into the operational analysis surfaces.

That gives three useful observability surfaces:

| Surface | Role |
| --- | --- |
| Microsoft Foundry Traces | Agent-centric view of agent execution and tool activity. |
| Azure Monitor / Application Insights | Distributed tracing, dependency tracking, and service-side correlation. |
| Log Analytics | KQL-based investigation, joins, trend analysis, and alert-ready querying. |

In other words, Foundry gives the agent/operator view, while Azure Monitor gives the platform/operations view. OpenTelemetry is the glue that makes the same run observable across both.

## Why This Brings Observability to Agents

Agent observability is useful only if it answers more than "did the call succeed?" The current design gets close to that goal because it makes these layers visible:

| Layer | What becomes observable |
| --- | --- |
| Agent execution layer | Foundry client-side spans and Foundry Traces show agent creation and Responses API activity. |
| Notebook orchestration layer | Custom spans show where the notebook invoked, persisted, or branched into Sentinel-specific paths. |
| Dependency layer | Automatically instrumented HTTPX2 plus explicit client spans create dependency rows for the actual outbound calls. |
| Run correlation layer | Resource attributes and baggage context let a single notebook run be grouped and traced across surfaces. |

That is the correct model for agent observability: agent actions, orchestration decisions, outbound dependencies, and correlation identifiers all need to exist in the same trace story.

## Current Package and Version Posture

The Python **3.14.7** environment was resolved on **2026-09-18** using the
approved package feed plus wheels built from verified official GitHub release
commits. [requirements-notebook.txt](requirements-notebook.txt) is the Windows
notebook's direct-dependency matrix; the table records the tested versions.
Use [build_notebook_wheels.ps1](build_notebook_wheels.ps1) when the feed has not
admitted a recent release. The root notebook's wheel cache and virtual
environment are independent of the standalone Agent Framework demo.

| Package | Installed | Notes |
| --- | --- | --- |
| `azure-ai-projects` | `2.6.1` | Project agents, MCP and client-side preview instrumentation. |
| `openai` | `3.16.1` | Responses/conversations API, using HTTPX2. |
| `httpx2` | `2.13.0` | Paired with HTTPCore2 2.13.0; independently verified with the updated SDK. |
| `azure-identity` | `1.26.0b2` | Existing preview line retained; no global `--pre` switch. |
| `azure-monitor-opentelemetry` | `1.8.10` | Resolves the matching exporter and instrumentation train. |
| `azure-monitor-opentelemetry-exporter` | `1.0.0b57` | Transitive Azure Monitor exporter. |
| `azure-core-tracing-opentelemetry` | `1.0.0b13` | Azure Core tracing bridge. |
| `opentelemetry-api` / `opentelemetry-sdk` | `1.44.0` | Aligned with Azure Monitor, not upgraded independently. |
| `opentelemetry-instrumentation-httpx` | `0.65b0` | Matches SDK 1.44 and includes separate HTTPX/HTTPX2 instrumentors. |
| `ipykernel` | `7.3.0` | Python 3.14 notebook kernel. |

Install the matrix together, run `pip check`, then restart the kernel if SDKs were already imported. The former `azure-ai-projects<2.5` / OpenAI 2.x restriction is no longer needed. This matrix does not upgrade the bot runtime, macOS notebook or standalone Agent Framework PoC.

### Runtime, Shared and Validation Profiles

- [requirements-notebook.txt](requirements-notebook.txt) contains only runtime requirements (82 resolved dependencies, excluding pip).
- [requirements-notebook-shared.txt](requirements-notebook-shared.txt) adds optional Agent Framework core 1.19.0, OpenAI provider 1.14.4, orchestrations 1.2.0 and OTLP gRPC exporter 1.44.0. These remain compatible with the root environment, but this notebook neither imports Agent Framework nor configures an OTLP exporter.
- [requirements-notebook-validation.txt](requirements-notebook-validation.txt) adds `nbclient==0.11.0` and `nbformat==5.11.1` for automated execution.
- [constraints-notebook-win11.txt](constraints-notebook-win11.txt) captures 102 direct/transitive versions across those profiles for Windows / CPython 3.14. It constrains resolution without installing optional packages; it is not a hash-verified lock or a cross-platform snapshot.

The constrained runtime plus validation profile installs cleanly without Agent Framework or OTLP. Existing shared packages were not uninstalled. The tested Azure Identity preview line was retained; moving to stable credentials remains a separate compatibility exercise.

The September 18 dependency check uses the real Azure AI Projects/OpenAI clients
with an in-memory HTTPX2 transport to verify both project and stable-agent
Responses routes, conversation creation, W3C trace propagation, and GenAI spans.
This is SDK compatibility validation, not a new live Azure model run. It does
not create or modify cloud agents, model deployments, or telemetry policy; earlier
live-run evidence below retains its original dates and versions.

All 119 tests pass against the publication snapshot; the clean runtime/validation
environment skips only the optional shared-MAF version check. The dependency
self-check found no active advisories in the PyPI per-version metadata for the
102 constrained packages. That check is best-effort published-advisory coverage,
not an independent code security audit or a guarantee about unpublished issues.

## Current-Run Telemetry Gate (Section 6)

Section 6 is a validation gate, not just a query display:

1. Resolve `WorkspaceResourceId` from the deployment's Application Insights component. Do not assume the Sentinel data workspace is also the telemetry workspace.
2. Flush the OpenTelemetry provider before querying.
3. Scope to the current `demo.run_id`, then follow `OperationId` to include SDK and HTTP child spans that do not carry that custom attribute themselves.
4. Poll for ingestion at 15-second intervals, up to 12 waits. Empty or old results cannot produce a pass.
5. Require story, facts and (when configured) Sentinel interaction coverage, a correlated Responses API dependency for each model interaction, GenAI chat spans, exactly one `persist_story` span labelled `persistence`, zero failed spans, and the configured service version (currently `2026.09.16`). Persistence does not require a Responses dependency because it is not an LLM call. Azure Monitor combines namespace and service name into `AppRoleName=foundry-agent-demo.foundry-agent-fw-demo`. Responses wrappers are identified by `gen_ai.operation.name=responses.create` and a `/responses` name suffix, supporting both project and stable agent endpoints.
6. Reject API errors and partial results. Display an HTML report with stage totals, content availability, conversation snapshots, a joined span inventory, root-call trends and exception drill-downs. Copyable KQL remains available in expandable sections.

The Sentinel orchestration span now carries both `demo.run_id` and `app.interaction=sentinel`, fixing its omission from run-filtered queries. Its response helper no longer reattaches a context captured before the parent span: doing that detached HTTP dependencies into unrelated operations. The query cells also reject failed/empty responses and exhausted approval loops instead of persisting them as successful results. The Sentinel specialist uses the supplied `SigninLogs` schema and plain KQL; it no longer requires table discovery.

Generated stories and Marp decks are local demo artifacts, not evidence that the service succeeded by themselves. Review MCP call results and the Section 6 gate as well.

The current notebook uses `OTEL_SERVICE_NAME=foundry-agent-fw-demo` and
`OTEL_SERVICE_VERSION=2026.09.16`; Section 6 expects the matching combined role.
Restart the kernel and rerun the runtime cells after this identity change, since
an initialized telemetry provider cannot adopt a different resource identity.
Historical run evidence below retains its original role and version. Queries or
dashboards hardcoded to the old `foundry-agent-framework-demo` service name must
include the new name to display subsequent runs.

### GenAI Content and the Section 6 Report

[notebook_observability.py](notebook_observability.py) owns the query builders and
HTML rendering; the notebook retains workspace resolution, credential selection,
trace flushing, bounded ingestion polling and service-identity validation.
Rerunning Section 6 only queries existing telemetry; it does not invoke agents,
change versions, install packages or create additional demo spans.

| Question | Source and interpretation |
|---|---|
| Did the required operations reach telemetry? | `AppDependencies`: unique spans, failed spans/operations, Responses wrappers, GenAI chat spans and persistence. |
| What happened in each notebook section? | Root interaction labels propagate to the trace's SDK/HTTP/service children. Stage totals show orchestration count/duration, tools, agent versions and models. |
| What messages, instructions and tool payloads were captured? | `AppGenAIContent`: content snapshots, metadata and optional bounded previews. These are not additional spans or distinct agent calls. |
| What failed? | Failed dependency spans plus `AppExceptions`, grouped by trace and parent span; exception messages are bounded but may contain sensitive data. |
| How many logical calls and how long did they take? | The 15-minute trend counts only notebook story/facts/Sentinel roots. It excludes nested Foundry `invoke_agent` spans; P95 for a single call is that call's duration. |

#### Correlation and cardinality

1. Validate the run ID as a UUID and select its operations within six hours.
2. Deduplicate dependency rows by `(_ResourceId, OperationId, Id)` and content
   records by `(_ResourceId, Id)`, retaining the latest timestamp.
3. Reduce root interaction context to one row per trace. Conflicting labels
   produce an explicit ambiguous-correlation validation issue rather than
   multiplying the span count.
4. Aggregate content by `(_ResourceId, TraceId, SpanId)` **before** the left join.
   Match `OperationId = TraceId`, dependency `Id = SpanId`, and the same resource.
   Spans without content remain visible, while multiple content records can
   annotate one span without multiplying it. The content index retains distinct
   record IDs instead of arbitrarily selecting one message snapshot.
5. Compute health and latency from spans alone. Content rows without matching
   spans are reported as a warning, not discarded from the content index.

`ConversationId` comes from `Attributes["gen_ai.conversation.id"]`; it can span
multiple traces/turns. `ContentId` is the content record ID, not a span ID.
The current pipeline's `Properties["_MS.GenAIContentId"]` also matches content
`Id`, but the report uses the resource/trace/span relationship for enrichment.
Client and service records may repeat input history. Do not sum their message
counts or token attributes as unique conversation turns or total model cost.

Instructions prefer the dedicated `SystemInstructions` field. When absent, the
query parses `InputMessages` JSON and retains messages with role `system` or
`developer`, including their structured parts. It does not regex-match legacy
span properties. Missing agent/model values on SDK records can use the tagged
root's metadata; unavailable fields remain labelled as not recorded.

#### Reading, privacy and failure behavior

- **Span-health PASS** requires the original strict coverage, failure,
  persistence and identity checks. It does not independently prove answer
  correctness, groundedness or successful tool semantics.
- **Content AVAILABLE** means input/output content exists for every expected
  interaction in this snapshot, not that every span should contain messages.
  **WAITING / NOT RECORDED** identifies missing interactions separately. With
  local recording disabled, content is not required for span health; independently
  captured service content can still exist. Missing content is not an empty answer.
- Empty message arrays do not count toward input/output coverage. Invalid JSON
  message arrays generate an explicit warning and a state label in the index,
  rather than being accepted as valid content or silently rendered as empty.
- `SHOW_GENAI_CONTENT=False` is the Section 6 default. The query returns metadata,
  sizes and instruction-source labels, but does not return message/tool preview
  fields. Setting it to `True` requests bounded previews and requires the local
  content-recording policy to be enabled. This display toggle does not change
  capture settings or require a kernel restart.
- Previews show at most **1,200 characters per field**, with original lengths
  and explicit truncation notices. Index, inventory and diagnostic detail views
  show up to **200 rows/groups**; coverage totals are uncapped. HTML-escape all
  telemetry and KQL before display. No telemetry is treated as executable markup.
- Queries are sequential snapshots and ingestion is asynchronous. Rerun the last
  cell to refresh content. Table/permission/API/partial-result errors still raise;
  there is no fallback to historical content, legacy pointers or invented values.
- Exception messages are separate from the message-preview toggle and can contain
  PII. Treat notebook outputs as sensitive; clear them before sharing. Consider
  protected-table access for `AppGenAIContent`, with retention/access appropriate
  to prompts and Sentinel results. Generated Marp content remains Git-ignored.
- Rerunning an interaction can reuse the existing `demo.run_id`. The strict gate
  includes earlier failed attempts even after a successful retry. The failure
  report distinguishes failed span count from failed operation count and provides
  correlated exception details. To validate a clean logical run, restart the
  kernel and execute from Confirm Existing Deployment through Section 6, skipping
  the environment/install cells. No failures are hidden or reclassified.

#### Dedicated-table migration

Microsoft documents that starting **September 30, 2026**, newly ingested values
for `gen_ai.input.messages`, `gen_ai.output.messages`,
`gen_ai.system_instructions`, `gen_ai.tool.definitions`,
`gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` and
`gen_ai.evaluation.explanation` move out of legacy telemetry tables into
`AppGenAIContent`. Legacy keys contain pointers; older data remains queryable.
The report reads content from the dedicated table now. No preview-feature flags,
RBAC, retention, logging/metrics exporters or SDK versions are changed here.

Sources: [table schema](https://learn.microsoft.com/azure/azure-monitor/reference/tables/appgenaicontent),
[migration guidance](https://learn.microsoft.com/azure/azure-monitor/app/data-model-complete#generative-ai-telemetry),
[protected tables](https://learn.microsoft.com/azure/azure-monitor/logs/protected-tables-configure).

#### Read-only validation evidence - 2026-09-15

The enhanced final cell was executed in an isolated validation kernel against
the previously completed run `8b2584d2-92d5-4e19-bdcf-37a12d56473f`, without
replaying inference, setup/version synchronization or package installation.
All generated queries executed successfully: **64 unique spans**, **34 matched
content records**, **30 spans without content**, **29 input / 29 output records**,
**18 instruction snapshots** (developer messages), **zero unmatched content**,
**three notebook root calls**, and **zero failures**. Preview queries respected
the 1,200-character bounds; the default report did not retrieve payload previews.

The earlier failed run `50a8a785-9e4d-42ba-9b7e-96cb5bcaa9fd` was also queried:
the new diagnostics correctly report **four failed spans in one operation** and
the underlying Sentinel MCP **403 Forbidden**, despite the later successful retry.
These are observations of specific saved runs, not fixed expected future counts.
The **101 notebook regression tests** pass. Synthetic queries executed in the
actual KQL engine also verified duplicate span/content deduplication, multiple
content records per span, cross-resource isolation, absent content, empty and
invalid message arrays, root-only trends and conflicting interaction labels.
The HTML report was browser-checked, including its expandable views and layout
at a 1,100-pixel viewport.

### Backend Endpoint Migration

The current Windows runtime can select the new backend endpoint mode through
its local build metadata. New agents `ZoDEfendersAgent-1702-backend` and
`ZoDEfendersAgent-1702-sentinel-backend` were created alongside the legacy agents,
each with a distinct instance identity/blueprint and version **1** initially pinned at 100%.
Only Responses and Entra authorization are enabled; there is no Teams publishing.

In backend mode:

- With **`pinned`** policy, Section 4 emits **`resolve_agent`** around read-only
  checks. With the demo's **`sync`** policy, it emits **`sync_agent`**, reuses a
  matching active/latest version or calls `create_version`, verifies activation
  on a concrete version and saves the new local checkpoint. Unchanged reruns
  make no create/update calls.
- The setup spans retain the model/request-ID/fingerprint attributes described
  below and add `app.agent.identity.principal_id`, `app.agent.identity.client_id`
  and `app.agent.active_version`.
- Each flow gets its own agent-bound client. Request URLs end in
  `/agents/{name}/endpoint/protocols/openai/responses`; no `agent_reference`
  override is sent to those endpoints.
- Explicit interaction roots add `app.agent.invocation_mode=agent_endpoint`.
  Persisted records also identify the invocation mode, and Marp runtime labels
  distinguish the endpoint mode from project dispatch.
- The client SDK can emit a generic `responses` span rather than an unversioned
  `invoke_agent` span. Service-side agent/model/tool spans and notebook roots
  remain correlated. Do not assert a fixed span count or require the old URL.

The endpoint's selected version, unique identity and enabled state are verified
before invocation. Strict `pinned` policy rejects drift; explicit `sync` policy
reconciles changed definitions and stale local version checkpoints. It preserves
protocols, authorization, identities and older versions, and does not enable
`@latest` or silently fall back. Sync changes are visible to all endpoint
consumers immediately after activation; it is a development convenience, not a
pre-activation evaluation/approval pipeline.
The model-definition fingerprint remains separate from endpoint routing.
Use [the README rollback setting](README.md#backend-agent-endpoints) to explicitly
return to the retained project endpoint path.

**Live validation:** a candidate rehearsal passed before cutover. After saving
the local backend settings, run `b4591fc5-2289-4527-8e81-a9e97e153f34` completed
all 10 runtime cells plus response/data/metadata/telemetry assertions with exit
code 0, without configuration injection, dependency installation or interactive
authentication. The detailed snapshot contained **54 unique spans**:
**10 setup**, **24 story/Learn**, **1 persistence**, **19 Sentinel**.
There were **7 endpoint Responses dependencies**, **11 chat spans**, no failed
spans and no `search_tables` call. All 65 local regression tests passed.
The agent principal/client IDs and active version on both setup spans were
compared directly with live agent metadata. Exported main/Sentinel Marp decks
passed browser checks for runtime labels and footer fit on all seven slides.

In that original pinned-mode validation, the four additional spans relative to
the comparable 50-span project-mode run
come from reading both agent details and the pinned version during setup, rather
than one creation request per agent. This comparison applies to these measured
runs; additional tool calls or asynchronous ingestion can change the totals.
Both candidates stayed pinned to version 1. A separate two-turn endpoint test
confirmed conversation continuity, and anonymous calls to both were rejected
with HTTP 401. No claim is made that old project conversation IDs are portable.

### Version Synchronization Follow-up

The demo now opts in to `backend_version_policy=sync`; strict `pinned` behavior
remains available and remains the default for build files without a policy.
Section 4 handles definition changes rather than treating the original version
checkpoint as permanent. It reuses the active or matching latest version,
otherwise calls `create_version`, then verifies the definition and activates a
concrete version. It writes only the corresponding local version checkpoint,
preserving other configuration and older remote versions.

The `sync_agent` span carries `app.agent.version_policy`,
`app.agent.version_action` (`reuse_active`, `reuse_latest`, or `create_version`),
`app.agent.previous_version` and the verified `app.agent.active_version`.
The create action records the API used; the service can itself deduplicate an
identical definition. `agent_version.endpoint_updated` and
`agent_version.selection_saved` events distinguish activation from checkpointing.

Run `447909c6-f671-4146-bade-cf841fd3644a` successfully synchronized both edited
definitions from version **1 to 2**, saved those selections, and completed all
10 runtime cells and live data/model/telemetry assertions with exit code 0.
The gate observed 64 spans, six Responses dependencies, one persistence span and
zero failures; counts are ingestion snapshots, not fixed requirements.
Both identities and stable endpoints remained the same, with version 1 retained.
Two further unchanged sync rounds per agent were tested with create/update calls
blocked: they reused version 2 without rewriting the build file.

All 80 regression tests pass, including creation/activation failures, wrong
returned definitions, unverified activation, concurrent endpoint/local edits,
atomic-save failure and reconciliation after a partial save failure.
Service activation and local persistence are not one transaction: a later file
error reports the already-active version, and a subsequent sync repairs the
checkpoint without creating another version. Synchronization is per agent;
successful earlier work is retained if a later agent fails.

### Creation Diagnostics and Persistence Correlation

The main/Sentinel **client setup spans** (`create_agent` in project mode,
`sync_agent` in backend sync mode, or `resolve_agent` in strict pinned mode) include:

- `app.model.publisher`, `app.model.name`, `app.model.version` and
  `app.model.deployment`, using the resolved deployment metadata.
- `app.agent.config.sha256`, computed from canonical UTF-8 JSON containing the
  agent definition and resolved model snapshot. Object keys are sorted; array
  order is retained. Changing instructions, tools or the resolved model version
  changes the fingerprint. Run/session/request IDs and returned agent versions
  are not inputs.
- `app.azure.request_id` and `app.azure.request_id_header`, taken only from
  allowlisted HTTP response headers. Preference order is `x-request-id`,
  `x-ms-request-id`, `apim-request-id`, then `request-id`.
  `app.azure.apim_request_id` also retains the gateway ID when present.
- `app.azure.request_id_available`: `false` until a response supplies an ID.
  A response without one emits `create_agent.request_id_unavailable`; no ID is
  fabricated. Error responses are inspected too, while exceptions still propagate
  and mark the creation span failed.

The fingerprint adds no raw definition payload and the response hook does not
export arbitrary headers. This does not redact the pre-existing content-capture
feature: prompts/instructions may still be recorded when that feature is enabled.
A SHA-256 fingerprint is a comparison aid, not encryption or proof of secrecy.

No extra creation wrapper or synthetic server span was added. The separate
`persist_story` span now explicitly carries `demo.run_id`, `app.session.id`,
`app.interaction=persistence`, agent identity/version and runtime. It remains an
internal span in its own operation, discoverable through the same run-ID filter.
Write failures propagate, retain the run attributes and record `error.type`.
The automatic span context manager records the exception and error status.

See [OTEL-Agent-Spans.md](OTEL-Agent-Spans.md) for the verified enhanced run and
the historical 49-span baseline. Counts can vary with tool use and approvals;
the validation gate does not hardcode a total span count.

Validation on **2026-09-15 UTC**: **53 regression tests passed**, and fresh run
`2855bde8-64be-45fd-b58f-5db1dbde25ce` completed all 10 runtime cells plus live
assertions with **exit code 0**. Both creation spans contained real service/gateway
request IDs and independently recomputed configuration fingerprints. The
`persist_story` span carried the run/session/agent/record identifiers and passed
the new gate. No library install or interactive authentication was required.

After asynchronous ingestion settled, the read-only inventory contained
**50 unique spans**: **6 creation**, **24 story/Learn**, **1 persistence** and
**19 Sentinel**; **7 Responses dependencies**, **11 chat spans**, zero failures
and no table-discovery call. The earlier gate snapshot had 43 rows; it checks
coverage rather than complete ingestion. The span guide preserves both facts.
The first enhanced-run test needed a query-only normalization after Kusto exposed
the date-shaped model version as a datetime; the full rerun then passed.

### Marp Model Identity

The main story/Learn deck and Sentinel deck now show the model's publisher/type,
underlying name and publisher-specific version in a visible footer on every
slide. Section 4 obtains these values from the Foundry deployment API, not from
the deployment alias, model-name string parsing, or agent version. Each final
response must match the verified deployment alias, model name or versioned name.
The run-metadata slide retains the deployment and response identifiers separately.

The same snapshot is saved under `model_metadata` in each generated record;
the main record keeps separate story/facts response identifiers. This describes
the deployed LLM, not the client-side `service.version` or `app.session.id`.
The new lookup uses the existing project credentials and does not add a package
or authentication flow. Lookup/metadata errors remain explicit.

All **42 local regression tests** pass. Private previews generated from the saved
Terra responses were exported to HTML with Marp and checked in the browser:
the four-slide main deck and three-slide Sentinel deck both show the footer on
every slide, without footer clipping or content overlap. No fresh model inference
was needed for this layout check. The active notebook run's decks and saved
records were left untouched; rerun Section 4, then Sections 5 and 5.1, to regenerate
those outputs with the updated generators.

### GPT-5.6 Terra Notebook Validation — 2026-09-15

The existing East US 2 account now has a separate **`gpt-5.6-terra`** deployment
using model version **`2026-07-09`**, **GlobalStandard**, capacity **250**.
The local Git-ignored build metadata selects that deployment. The old `gpt-5.4`
deployment (backed by `gpt-5.4-mini`) remains intact; the chat model was not changed.

Run **`3ef41db4-2f01-4194-ac78-2562798aac34`** completed all **10 runtime code
cells** with interactive authentication blocked by test-only guards. The three
environment/dependency setup cells were skipped; no SDK packages were changed.
Validation confirmed:

- Completed story, grounded Microsoft Learn MCP and Sentinel responses on Terra.
- One actual interactive `SigninLogs` record for the requested identity, with
  valid IP/time values and identity/IP reflected in the final answer.
- Both persisted record types and their Marp outputs.
- **55 correlated spans**, **8 Responses API dependencies** covering story,
  facts and Sentinel, **13 GenAI spans**, and **zero failed spans**.
- No `search_tables` call or span.

The SDK emits both `gpt-5.6-terra` and `gpt-5.6-terra-2026-07-09` in request and
response model attributes. An additional test initially expected only the
unversioned deployment name and failed after the runtime cells and data checks
had passed. The test was corrected to ignore empty attributes and accept only
those two verified names; a fresh query of the same run passed. This was a
validation-expectation correction, not a notebook or service failure.

The source notebook remains cleared, and its runtime code matches the executed
copy. Deployment selection is local configuration, not a hardcoded notebook
default or an update to the infrastructure deployment menu.

### Direct Sentinel Table Routing — 2026-09-15

To avoid the reported approximately six-second `execute_tool mcp_microsoft-sentinel-data.search_tables` step, system and user prompts share a known-table policy:

- Resolve the workspace with `list_sentinel_workspaces`, then call `query_lake` directly.
- Query only `SigninLogs`, filtering `IsInteractive == true` and the signed-in `UserPrincipalName` case-insensitively; select `top 1 by TimeGenerated desc`.
- Use the supplied exact KQL template and column types, including the boolean `IsInteractive` and dynamic `LocationDetails`. This table selection was explicitly requested, not a fallback.
- Map `LocationDetails.city`, `.state` and `.countryOrRegion` to display fields, with `Location` as the country-code fallback. Map `AppDisplayName` (or `ResourceDisplayName` when empty) to the application. Do not invent top-level city/state/country columns.
- Do not call `search_tables`, rediscover the schema or silently substitute another table.

All **34 local regression tests** pass, including the explicit table, boolean filter, schema/output mappings, shared prompts, canonical query and unchanged MCP connection.

The fresh targeted notebook run `981ede37-56db-437a-bb29-6b1062000647` succeeded on **2026-09-15 UTC** through the existing Foundry project connection with interactive authentication blocked by test-only guards. The actual MCP request used `SigninLogs`, the interactive/identity filters and the expected workspace. Its dataset completed without errors and returned **one interactive sign-in record**. The returned identity, valid IP/timestamp and location agreed with the final answer; the completed result and Marp output were persisted. Raw sign-in values are not included here.

Current-run Application Insights telemetry confirmed these successful tool spans under the same Sentinel operation:

| Tool | Duration | Success |
|---|---|---|
| `query_lake` | 4,028.57 ms | true |
| `list_sentinel_workspaces` | 2,933.00 ms | true |

There was **no `search_tables` call or span**. These are measured tool durations from one run, not a guarantee of end-to-end latency or six-second savings. This was a targeted Sentinel run, not a rerun of the independent story/Learn interactions. Notebook outputs remain cleared.

The earlier `EntraIdSignInEvents` attempt failed table resolution in SDL despite its visibility in Advanced Hunting. That blocked configuration is superseded by the explicitly selected and successfully validated `SigninLogs` path; no Graph API or custom MCP collection is used.

### Trace-Only Policy Follow-up — 2026-09-14

Run `419d2a78-eda0-4168-be3f-65e1afe5baf6` executed all **13 notebook code cells** successfully, plus private validation assertions. The current-run gate observed **55 correlated spans**, **eight response dependencies** covering story/facts/Sentinel, **13 GenAI spans**, and **zero failures**, with the expected role and version.

Unlike the initial trace-only claim, this run also checked actual runtime objects: **no SDK log or metric providers**, **no Live Metrics or performance-counter span processors**, **SDK and custom content capture both off**, and **HTTPX2 auto-instrumentation enabled**.

All **25 regression tests** passed in the existing environment and in a clean runtime-plus-validation environment without Agent Framework or OTLP. The clean install passed `pip check`; the optional shared profile also resolved against the same constraints. Earlier live attempts surfaced Sentinel-generated datetime-format and unquoted projection-alias errors; the specialist instructions now keep datetime values raw and aliases space-free. These remain model-generated queries, so service/tool failures still raise rather than being silently retried or presented as success.

The reduced runtime closure contains **82 packages**, all checked against PyPI version advisory metadata with **zero reported advisories** and no missing metadata entries. This is not a comprehensive security audit. Notebook code in the cleared working copy was compared byte-for-byte per cell with the successful execution.

### Initial Dependency-Upgrade Evidence — 2026-09-14

The final fresh-kernel run executed **all 13 code cells** successfully, followed by an additional local assertion cell. It verified completed story and Microsoft Learn responses, an actual Learn MCP call and Learn URL, a completed Sentinel response without terminal tool errors, both persisted record types, and generated Marp files.

This initial run verified traces only. A subsequent configuration sanity check established that the old log/metric-disable arguments were overwritten by the distro and Live Metrics was enabled by default. The implementation described above corrects that; the initial counts below are not proof that those other signals were disabled.

For run `73b57ebd-8d96-4a42-a5dc-d7475b127bf7`, the ingestion gate observed:

| Check | Result |
| --- | --- |
| Correlated dependency spans | 55 |
| Failed spans | 0 |
| Responses API HTTP dependencies | 8, covering story, facts and Sentinel |
| GenAI chat spans | 13 |
| MCP tool spans | Microsoft Learn search; Sentinel workspace listing, table search and `query_lake` |
| Notebook role / version | `foundry-agent-demo.foundry-agent-framework-demo` / `2026.09.14` |
| Targeted regression tests | 12 passed |
| Dependency consistency | `pip check` passed |
| Advisory metadata | 92 resolved packages checked against PyPI version metadata; zero reported advisories after the HTTPX2 patch |

The advisory check found HTTPX2 2.10.0 affected by [GHSA-h4x7-gw46-3wm6](https://github.com/advisories/GHSA-h4x7-gw46-3wm6), [GHSA-pf96-p4fj-6566](https://github.com/advisories/GHSA-pf96-p4fj-6566), and [GHSA-8xx6-hgc6-gc2m](https://github.com/advisories/GHSA-8xx6-hgc6-gc2m), plus their PYSEC aliases. The 2.12.0 pin clears these reported advisories. This is a metadata check, not a comprehensive security audit.

Run the local regression tests without Azure calls:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s .\tests -p test_notebook_validation.py -v
```

Tests cover approval boundaries, failed/empty responses, content-recording opt-in, strict Log Analytics result decoding, and the Sentinel parent/child trace relationship using an in-memory exporter. Full integration verification still requires running the notebook against Azure. Foundry portal rendering and the separate macOS/standalone Agent Framework notebooks were not validated by this run. Committed notebooks have cleared outputs; private execution evidence and generated validation decks are not committed.

## Environment Variables in 3.1

These are the important environment variables used in the Windows notebook's current Section 3.1.

| Variable | Set today in 3.1 | What it does | How this repo uses it |
| --- | --- | --- | --- |
| `OTEL_SERVICE_NAME` | Yes | Sets the OTEL service identity. | Used to stamp notebook-generated spans as coming from the Foundry agent demo service. |
| `OTEL_SERVICE_VERSION` | Yes | Sets service version metadata. | Used to distinguish notebook build/version posture in traces. |
| `OTEL_RESOURCE_ATTRIBUTES` | Optional, preserved | Adds resource-level attributes to every span. | Explicit notebook service/project/session/environment attributes take precedence. Supply `cloud.region` only from verified deployment metadata, not a guessed location. |
| `AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING` | Yes | Explicitly opts the Azure AI Projects SDK into preview GenAI tracing. | Must be set before `AIProjectInstrumentor().instrument()` or Foundry client-side GenAI spans will not be emitted. |
| `AZURE_TRACING_GEN_AI_ENABLE_TRACE_CONTEXT_PROPAGATION` | Yes | Enables W3C trace-context propagation for OpenAI clients returned by `get_openai_client()`. | Helps correlate client-side notebook spans with downstream Azure-side work. |
| `AZURE_TRACING_GEN_AI_TRACE_CONTEXT_PROPAGATION_INCLUDE_BAGGAGE` | Yes | Includes the `baggage` header with propagated trace context. | Used because the notebook's baggage keys are limited to safe correlation metadata such as run ID, agent ID, interaction name, and session ID. |
| `AZURE_TRACING_GEN_AI_INSTRUMENT_RESPONSES_API` | Yes | Enables Responses API instrumentation in the Foundry tracing path. | Used so `responses.create(...)` activity is observable in preview client-side traces. |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Yes | Controls SDK and custom prompt/completion capture. | Defaults to `true` for this demo; explicit `false` opts out. Accepts case-insensitive `true`/`false`, normalized once and passed explicitly to the instrumentor. Invalid values fail before provider setup. |
| `OTEL_LOGS_EXPORTER` / `OTEL_METRICS_EXPORTER` | Yes | Select signals to export. | Both set to `none`; these are the effective signal-disable controls in Azure Monitor 1.8.10. |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | Yes | Select the trace sampler. | Set to `microsoft.fixed_percentage` / `1.0` so inherited settings cannot reduce demo coverage. |
| `OTEL_TRACES_EXPORTER` | Checked | Can disable tracing when set to `none`. | `none` is rejected with an actionable error instead of silently producing no traces. |
| `OTEL_EXPERIMENTAL_RESOURCE_DETECTORS` | Yes, only for local/non-Azure runs | Controls which OTEL resource detectors are active. | Set to `otel` locally to avoid Azure-host detector behavior when running outside Azure. |
| `APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL` | Yes, only for local/non-Azure runs | Disables Statsbeat telemetry from the Application Insights exporter path. | Reduces local-noise telemetry and keeps the notebook trace-only. |

## Environment Variables to Keep in Mind

These are the main variables to understand when operating Section 3.1.

| Variable | Recommendation | Why it helps |
| --- | --- | --- |
| `AZURE_TRACING_GEN_AI_TRACE_CONTEXT_PROPAGATION_INCLUDE_BAGGAGE=true` | Keep enabled while baggage remains limited to safe correlation keys | Makes the notebook's run and agent identifiers available across downstream trace context. |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` | Demo default; explicitly set `false` outside approved content-capture scenarios | Allows prompt, response, tool-argument, and tool-result content to appear in traces, which carries data-exposure risk. |
| `AZURE_TRACING_GEN_AI_INCLUDE_BINARY_DATA` | Leave unset/off | Binary payload recording is unnecessary for this text-only demo. |
| `OTEL_TRACES_SAMPLER` and `OTEL_TRACES_SAMPLER_ARG` | Fixed at 100% for this demo | Changing sampling for production requires revisiting the complete-trace validation gate. |

## Enhancements Applied to 3.1 & 3.3

These are the main changes applied to the Windows 3.1 & 3.3 cells.

### 1. Use Settings Honored by the Actual SDK

The Windows notebook does not set the Agent Framework-specific GenAI opt-in or the obsolete `AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED` flag. Azure AI Projects 2.6.0 uses its installed preview span schema and reads `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`. HTTP instrumentation has its own semantic-convention behavior; there is no global switch that upgrades every emitted span schema.

### 2. Make baggage propagation an explicit decision

The notebook already creates baggage context, and the Windows 3.1 flow now explicitly turns on baggage propagation so run identifiers, scenario IDs, and notebook correlation markers can flow across more downstream operations:

```python
os.environ["AZURE_TRACING_GEN_AI_TRACE_CONTEXT_PROPAGATION_INCLUDE_BAGGAGE"] = "true"
```

That choice is safe here because the current baggage keys are limited to correlation metadata rather than prompts or other sensitive payload content.

### 3. Add explicit content-recording policy

The Windows 3.1 path now makes message-content capture an explicit policy decision. That matters for agent debugging because content recording is the difference between seeing only operation shape versus seeing the actual prompt/tool payload that drove the result.

`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` defaults to `true` for this controlled demo. Set it explicitly to `false` before initialization to opt out of recording potentially sensitive prompts, responses and tool payloads. The helper parses it once, uses the same boolean for SDK and custom spans, and rejects ambiguous values such as `1`. Explicit environment values take precedence over the default, including a `false` left by a previous run. Restart the kernel to pick up the changed default; if `false` is inherited from the launching environment, change or remove that override as well. The notebook refuses to display a new policy while retaining an old provider configuration.

This setting is not a universal redaction filter. Exception messages, MCP diagnostics, saved stories and generated decks may contain sensitive information independently of normal prompt/completion capture.

The live-run evidence above records the earlier content-off policy. The later default-on change is covered by local regression tests for the default, explicit opt-out, SDK agreement and restart guard; that historical content-off result is not a claim about the new default.

### 4. Harden the initialization order for notebook reruns

The Windows notebook now uses the safer order already proven in the macOS variant: it delays `settings.tracing_implementation = "opentelemetry"` until after Azure Monitor is configured. That is a good notebook-specific hardening tactic because reused kernels can inherit stale global tracer-provider state.

For a notebook, I recommend this order:

1. Set environment variables.
2. Resolve the Application Insights connection string from the Foundry project.
3. Call `configure_azure_monitor(...)`.
4. Then switch `settings.tracing_implementation` to `"opentelemetry"`.
5. Apply the content/propagation booleans to `AIProjectInstrumentor`, then verify its state and the distro-owned HTTPX2 instrumentor. Do not uninstrument or manually re-wrap HTTP clients.

The helper remembers its backend/resource/content configuration. Identical reruns do not call Azure Monitor again. Changed configuration or a prior partially failed setup requires a fresh kernel.

### 5. Add a span processor for agent metadata

Microsoft's Foundry tracing guidance explicitly supports adding a custom span processor. That remains a reasonable next step if you want every span to include the same correlation metadata without repeating `span.set_attribute(...)` everywhere.

Good candidates include:

| Attribute | Value idea |
| --- | --- |
| `session.id` | Notebook telemetry session UUID |
| `gen_ai.agent.name` | Main agent or Sentinel agent name |
| `gen_ai.agent.id` | Agent ID when available |
| `demo.scenario` | `storytelling`, `msft_learn`, or `sentinel` |
| `foundry.project.name` | Already present in resource attributes; keep consistent |

### 6. Keep traces on, keep logs and metrics off by default for the notebook

The trace-only policy uses exporter environment variables, not the old `disable_logging` and `disable_metrics` keyword arguments. Live Metrics is independently disabled because it defaults to on in Azure Monitor 1.8.10. Performance counters stay disabled. This is a notebook trace-demo policy, not a claim that logs or metrics are undesirable in production.

I would keep that default, and only add logs later if you want:

1. structured prompt-routing logs,
2. tool-selection audit logs, or
3. alerting on agent failures without relying only on traces.

## Practical Recommendation

If you want the best improvement-to-effort ratio beyond the current notebook state, do these next:

1. Add a lightweight custom span processor for cross-cutting agent metadata.
2. Deliberately redesign sampling and the validation gate if you need lower-cost long-running telemetry.
3. Keep reviewing which baggage keys are safe to propagate as the notebook evolves.
4. Turn on content recording only for controlled debugging windows.

Those changes would make Section 3.1 more complete without changing the overall design.

## Bottom Line

Section 3.1 already has the right architecture for agent observability:

1. OpenTelemetry provides the instrumentation model.
2. Azure Monitor provides the export path and operational analysis surface.
3. Application Insights and Log Analytics provide queryable distributed traces and dependencies.
4. Microsoft Foundry Traces provides the agent-specific execution view.

The meaningful enhancements are not about replacing Azure Monitor or OTEL. They are about making the existing trace story richer, more policy-driven, and more reliable for repeated notebook runs.

## References

1. Microsoft Foundry client-side tracing: https://learn.microsoft.com/azure/foundry/observability/how-to/trace-agent-client-side
2. Set up tracing in Microsoft Foundry: https://learn.microsoft.com/azure/foundry/observability/how-to/trace-agent-setup
3. Azure Monitor OpenTelemetry configuration: https://learn.microsoft.com/azure/azure-monitor/app/opentelemetry-configuration
4. Azure Monitor OpenTelemetry Python package: https://learn.microsoft.com/python/api/overview/azure/monitor-opentelemetry-readme?view=azure-python
5. OpenTelemetry Python: https://opentelemetry.io/docs/languages/python/