# Observability for Section 3.1

This document explains what Section 3.1 in the Windows notebook turns on today, how that telemetry reaches Azure Monitor and Microsoft Foundry, and which observability enhancements were applied to strengthen the trace story.

The current Section 3.1 design is already solid. It enables OpenTelemetry-based client-side tracing, exports traces to Azure Monitor via Application Insights, and adds explicit notebook-side dependency spans so agent traffic is visible both in Foundry Traces and in Log Analytics. The recent updates improve semantic richness, propagation, and notebook-kernel reliability without changing the overall architecture.

## Short Answer

Yes. Section 3.1 was worth improving, and the core path remains correct.

The highest-value enhancements that are now applied are:

1. Add the newer GenAI semantic-convention opt-in used by the macOS notebook.
2. Enable baggage propagation for the notebook's safe correlation keys.
3. Add an explicit content-recording policy instead of relying on defaults.
4. Harden initialization order for notebook reruns in a reused kernel.
5. Keep custom span metadata in place for agent, run, and interaction correlation.

## What 3.1 Turns On Today

Section 3.1 in [zolab-ai-agent-demo-win11.ipynb](zolab-ai-agent-demo-win11.ipynb#L681) currently enables the following:

| Capability | Current behavior in 3.1 | Why it matters |
| --- | --- | --- |
| OpenTelemetry as the Azure SDK tracing backend | `settings.tracing_implementation = "opentelemetry"` | Makes Azure SDK operations emit spans through OTEL instead of using no-op tracing. |
| Azure Monitor export | `configure_azure_monitor(...)` with the project's Application Insights connection string | Sends notebook traces to Azure Monitor so they land in Application Insights and Log Analytics. |
| Foundry client-side tracing | `AIProjectInstrumentor().instrument()` after `AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=true` | Emits client-side GenAI spans for Foundry project and Responses API activity. |
| HTTP dependency tracing | `HTTPXClientInstrumentor().instrument()` | Captures HTTPX dependency telemetry used by the OpenAI and Foundry client stack. |
| Explicit Responses API dependency spans | Manual `POST /openai/v1/responses` client spans later in the notebook | Ensures Azure Monitor has concrete dependency rows that correlate cleanly in Service Map and KQL. |
| Custom notebook orchestration spans | Manual spans such as `create_agent`, `invoke_agent`, `persist_story`, and Sentinel-specific spans | Makes the notebook's orchestration layer observable rather than only the SDK internals. |
| Resource identity | `OTEL_SERVICE_NAME`, `OTEL_SERVICE_VERSION`, `OTEL_RESOURCE_ATTRIBUTES` | Gives every span service identity, environment, instance, and Foundry project metadata. |
| GenAI semantic conventions | `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` | Uses the latest experimental GenAI span attributes and events supported by the current OTEL guidance. |
| Baggage propagation | `AZURE_TRACING_GEN_AI_TRACE_CONTEXT_PROPAGATION_INCLUDE_BAGGAGE=true` | Lets the notebook's run, agent, and interaction baggage keys flow with downstream trace context. |
| Explicit content-recording policy | `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false` and `AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED=false` by default | Keeps payload capture off unless an operator intentionally opts in for debugging. |
| Safer kernel reruns | Azure Monitor is configured before Azure SDK tracing is switched back to OpenTelemetry | Reduces the chance of a reused notebook kernel inheriting a stale tracer provider. |
| Trace-only local export posture | `disable_logging=True`, `disable_metrics=True`, `enable_performance_counters=False` | Keeps the notebook focused on traces and avoids local startup noise and platform-specific performance-counter issues. |

## How the Telemetry Flows

The telemetry path for this repo is:

1. The notebook creates spans through OpenTelemetry, Azure SDK instrumentation, HTTPX instrumentation, and explicit custom spans.
2. `configure_azure_monitor(...)` registers Azure Monitor exporters for the signals that remain enabled.
3. The notebook retrieves the Application Insights connection string from the Foundry project at runtime by calling `project_client.telemetry.get_application_insights_connection_string()`.
4. Azure Monitor sends the exported trace data to Application Insights.
5. Because the Application Insights instance is workspace-based, the same telemetry is queryable in Log Analytics.
6. Because agent calls use the Foundry Responses API with `agent_reference`, the traces also show up in Microsoft Foundry Traces.

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
| Dependency layer | HTTPX plus explicit client spans create dependency rows for the actual outbound calls. |
| Run correlation layer | Resource attributes and baggage context let a single notebook run be grouped and traced across surfaces. |

That is the correct model for agent observability: agent actions, orchestration decisions, outbound dependencies, and correlation identifiers all need to exist in the same trace story.

## Current Package and Version Posture

The active `.venv` in this repo is already on the latest available versions for the primary packages behind Section 3.1 as of April 6, 2026.

| Package | Installed | Latest available | Notes |
| --- | --- | --- | --- |
| `azure-monitor-opentelemetry` | `1.8.7` | `1.8.7` | Latest stable Azure Monitor OTEL distro for Python. |
| `opentelemetry-sdk` | `1.40.0` | `1.40.0` | Latest stable OTEL Python SDK in the current environment. |
| `azure-ai-projects` | `2.0.1` | `2.0.1` | Latest stable package, but Foundry client-side GenAI tracing remains preview. |
| `azure-monitor-opentelemetry-exporter` | `1.0.0b50` | `1.0.0b50` | Latest prerelease exporter used underneath the distro. |
| `azure-core-tracing-opentelemetry` | `1.0.0b12` | `1.0.0b12` | Latest prerelease Azure Core OTEL bridge. |
| `opentelemetry-instrumentation-httpx` | `0.61b0` | `0.61b0` | Latest prerelease HTTPX instrumentation. |

So the version recommendation is not "upgrade everything again." The better recommendation is:

1. Keep using the current latest package set.
2. Pin the versions more intentionally if notebook reproducibility matters.
3. Spend effort on trace semantics and propagation policy rather than chasing package churn.

## Environment Variables in 3.1

These are the important environment variables used in the Windows notebook's current Section 3.1.

| Variable | Set today in 3.1 | What it does | How this repo uses it |
| --- | --- | --- | --- |
| `OTEL_SERVICE_NAME` | Yes | Sets the OTEL service identity. | Used to stamp notebook-generated spans as coming from the Foundry agent demo service. |
| `OTEL_SERVICE_VERSION` | Yes | Sets service version metadata. | Used to distinguish notebook build/version posture in traces. |
| `OTEL_RESOURCE_ATTRIBUTES` | Yes | Adds resource-level attributes to every span. | Used for `service.namespace`, `service.instance.id`, `deployment.environment`, and `foundry.project.name`. |
| `AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING` | Yes | Explicitly opts the Azure AI Projects SDK into preview GenAI tracing. | Must be set before `AIProjectInstrumentor().instrument()` or Foundry client-side GenAI spans will not be emitted. |
| `AZURE_TRACING_GEN_AI_ENABLE_TRACE_CONTEXT_PROPAGATION` | Yes | Enables W3C trace-context propagation for OpenAI clients returned by `get_openai_client()`. | Helps correlate client-side notebook spans with downstream Azure-side work. |
| `AZURE_TRACING_GEN_AI_TRACE_CONTEXT_PROPAGATION_INCLUDE_BAGGAGE` | Yes | Includes the `baggage` header with propagated trace context. | Used because the notebook's baggage keys are limited to safe correlation metadata such as run ID, agent ID, interaction name, and session ID. |
| `AZURE_TRACING_GEN_AI_INSTRUMENT_RESPONSES_API` | Yes | Enables Responses API instrumentation in the Foundry tracing path. | Used so `responses.create(...)` activity is observable in preview client-side traces. |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | Yes | Selects the semantic-convention profile used for GenAI spans. | Set to `gen_ai_latest_experimental` so the notebook emits the latest experimental GenAI attributes and events. |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Yes | Controls whether prompt, response, tool-argument, and tool-result contents are captured in traces. | Set to `false` by default so payload capture is opt-in instead of implicit. |
| `AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED` | Yes | Companion flag for content recording in the Azure tracing path. | Also set to `false` by default to keep debugging content capture an explicit choice. |
| `OTEL_EXPERIMENTAL_RESOURCE_DETECTORS` | Yes, only for local/non-Azure runs | Controls which OTEL resource detectors are active. | Set to `otel` locally to avoid Azure-host detector behavior when running outside Azure. |
| `OTEL_RESOURCE_DETECTORS` | Yes, only for local/non-Azure runs | Similar detector control for newer OTEL behavior. | Also set to `otel` locally so notebook metadata is predictable on developer machines. |
| `APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL` | Yes, only for local/non-Azure runs | Disables Statsbeat telemetry from the Application Insights exporter path. | Reduces local-noise telemetry and keeps the notebook trace-only. |

## Environment Variables to Keep in Mind

These are the main variables to understand when operating Section 3.1.

| Variable | Recommendation | Why it helps |
| --- | --- | --- |
| `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` | Keep enabled | Ensures the notebook uses the latest experimental GenAI semantic conventions. |
| `AZURE_TRACING_GEN_AI_TRACE_CONTEXT_PROPAGATION_INCLUDE_BAGGAGE=true` | Keep enabled while baggage remains limited to safe correlation keys | Makes the notebook's run and agent identifiers available across downstream trace context. |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` | Turn on only for short-lived debugging sessions | Allows prompt, response, tool-argument, and tool-result content to appear in traces, which is valuable for debugging but carries data-exposure risk. |
| `AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED=true` | Turn on only when content recording is intentionally approved | Companion content-recording control for the Azure tracing path. |
| `OTEL_TRACES_SAMPLER` and `OTEL_TRACES_SAMPLER_ARG` | Optional, but useful if you want env-driven sampling policy | Current code already forces `sampling_ratio=1.0`, which is fine for demos. Adding explicit sampler env vars makes the policy portable and easier to externalize later. |

## Enhancements Applied to 3.1

These are the main changes applied to the Windows 3.1 cell.

### 1. Add the latest GenAI semantic-convention opt-in

This is the most obvious gap. The macOS notebook already sets:

```python
os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "gen_ai_latest_experimental"
```

This is now enabled in the Windows 3.1 cell so the notebook emits the latest experimental GenAI attributes and events supported by the current OTEL guidance.

### 2. Make baggage propagation an explicit decision

The notebook already creates baggage context, and the Windows 3.1 flow now explicitly turns on baggage propagation so run identifiers, scenario IDs, and notebook correlation markers can flow across more downstream operations:

```python
os.environ["AZURE_TRACING_GEN_AI_TRACE_CONTEXT_PROPAGATION_INCLUDE_BAGGAGE"] = "true"
```

That choice is safe here because the current baggage keys are limited to correlation metadata rather than prompts or other sensitive payload content.

### 3. Add explicit content-recording policy

The Windows 3.1 path now makes message-content capture an explicit policy decision. That matters for agent debugging because content recording is the difference between seeing only operation shape versus seeing the actual prompt/tool payload that drove the result.

Recommended pattern:

```python
os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false")
os.environ.setdefault("AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED", "false")
```

The notebook now documents and displays this default so operators know to flip it only for short-lived debugging sessions.

### 4. Harden the initialization order for notebook reruns

The Windows notebook now uses the safer order already proven in the macOS variant: it delays `settings.tracing_implementation = "opentelemetry"` until after Azure Monitor is configured. That is a good notebook-specific hardening tactic because reused kernels can inherit stale global tracer-provider state.

For a notebook, I recommend this order:

1. Set environment variables.
2. Resolve the Application Insights connection string from the Foundry project.
3. Call `configure_azure_monitor(...)`.
4. Then switch `settings.tracing_implementation` to `"opentelemetry"`.
5. Then run `AIProjectInstrumentor().instrument()` and `HTTPXClientInstrumentor().instrument()`.

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

This is not a bug; it is a good default for a demo notebook. The current `configure_azure_monitor(...)` call disables logs and metrics, which keeps the signal focused.

I would keep that default, and only add logs later if you want:

1. structured prompt-routing logs,
2. tool-selection audit logs, or
3. alerting on agent failures without relying only on traces.

## Practical Recommendation

If you want the best improvement-to-effort ratio beyond the current notebook state, do these next:

1. Add a lightweight custom span processor for cross-cutting agent metadata.
2. Externalize sampling policy if you need lower-cost long-running telemetry.
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