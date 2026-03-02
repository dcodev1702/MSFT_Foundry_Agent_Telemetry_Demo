# Azure AI Foundry Notebook Guide 
> (`zolab-ai-agent-demo.ipynb`)

This document covers setup and troubleshooting for the notebook workflow that uses Azure AI Projects + OpenTelemetry tracing.

---

## Notebook intent (observability-first)

This notebook demonstrates how to enable end-to-end observability for Azure AI Foundry Agent workflows, with emphasis on **visibility** rather than agent behavior. It configures Azure Monitor + OpenTelemetry and SDK instrumentation so you can:

- Correlate client-side and service-side operations for a single run.
- View trace spans and execution flow in **Foundry Trace (Preview)**.
- Send telemetry to **Application Insights** for deeper diagnostics and historical analysis.
- Capture context needed for troubleshooting latency, failures, and workflow bottlenecks.

---

## Known-good dependency set

Run this in notebook Cell 5:

```python
%pip install --no-input --pre "azure-ai-projects>=2.0.0b4" azure-identity "opentelemetry-sdk<1.39" "opentelemetry-api<1.39" azure-monitor-opentelemetry azure-core-tracing-opentelemetry
```

---

## Recommended run order (after kernel restart)

1. Cell 7 (imports)
2. Cell 9 (project client)
3. Cell 11 (telemetry setup)
4. Cell 13 (agent creation)
5. Cell 15 (agent query)

---

## Credential transparency (Cell 9)

Cell 9 now prints both:

- The concrete credential selected by `DefaultAzureCredential` (for example, `AzurePowerShellCredential` or `AzureCliCredential`)
- The signed-in account identifier when available (for example, UPN)

Example output:

```text
DefaultAzureCredential acquired a token from AzurePowerShellCredential
🔐 Credential used: AzurePowerShellCredential
👤 Signed-in account: agent007@m365x81069033.onmicrosoft.com
```

How account resolution works:

- If `AzurePowerShellCredential` is selected, the notebook queries `Get-AzContext` via `pwsh` and prints `Account.Id`.
- If `AzureCliCredential` is selected, the notebook queries `az account show --query user.name -o tsv`.
- For other credential types, the notebook still prints the credential class and may report that account resolution is unavailable.

---

## Telemetry imports explained

The notebook imports a small set of libraries specifically to enable tracing and export telemetry:

- `from opentelemetry import trace`  
	Creates/uses tracers so notebook steps can emit spans.

- `from azure.monitor.opentelemetry import configure_azure_monitor`  
	Configures Azure Monitor OpenTelemetry integration and exporter wiring.

- `from azure.ai.projects.telemetry import AIProjectInstrumentor`  
	Instruments Azure AI Projects + OpenAI client calls so SDK operations are automatically traced.

- `from azure.core.settings import settings` with `settings.tracing_implementation = "opentelemetry"`  
	Ensures Azure SDK tracing routes through OpenTelemetry.

- `from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter` and OTel span processor imports  
	Used as resilient fallback export path if full monitor configuration is slow/unavailable.

---

## How telemetry is illuminated on the agent

Telemetry visibility comes from combining automatic SDK instrumentation with explicit span context around agent lifecycle operations:

1. **Telemetry pipeline setup (Cell 11)**  
	 Initializes monitor/export, enables tracing feature flags, and instruments SDK clients.

```python
os.environ["AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING"] = "true"
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"

ai_conn_str = project_client.telemetry.get_application_insights_connection_string()
configure_azure_monitor(
	connection_string=ai_conn_str,
	enable_performance_counters=False,
)

AIProjectInstrumentor().instrument(
	enable_content_recording=True,
	enable_trace_context_propagation=True,
)
```

2. **Agent creation span (Cell 13)**  
	 Wraps create-version call in a custom span and stamps attributes like `agent.name`, `agent.id`, model, and version.

```python
tracer = trace.get_tracer(__name__)
with tracer.start_as_current_span("agent-creation") as span:
	span.set_attribute("agent.name", agent_name)
	span.set_attribute("gen_ai.request.model", model_name)
	agent = project_client.agents.create_version(
		agent_name=agent_name,
		definition=PromptAgentDefinition(model=model_name, instructions="..."),
	)
	span.set_attribute("agent.id", agent.id)
	span.set_attribute("agent.version", agent.version)
```

3. **Agent query span (Cell 15)**  
	 Wraps responses call, records prompt/model/completion metadata, and correlates request/response IDs.

```python
with tracer.start_as_current_span("agent-query") as span:
	span.set_attribute("agent.name", agent.name)
	span.set_attribute("gen_ai.request.model", model_name)
	span.set_attribute("gen_ai.prompt", user_prompt)

	response = openai_client.responses.create(
		input=[{"role": "user", "content": user_prompt}],
		extra_body={"agent_reference": {"name": agent.name, "id": agent.id, "type": "agent_reference"}},
	)

	span.set_attribute("gen_ai.response.id", response.id)
	span.set_attribute("gen_ai.completion", response.output_text[:500])
```

4. **Agent correlation in request body (Cell 15)**  
	 Sends `agent_reference` with both `name` and `id`, which improves visibility in Foundry Trace (Preview).

5. **Trace propagation enabled**  
	 Allows client-side and service-side operations to be linked in a single distributed trace view.

Result: you get timeline-level visibility across setup, agent creation, and inference requests in both Application Insights and Foundry Trace (Preview).

---

## Telemetry troubleshooting (Trace preview)

- Ensure `AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=true` is set before instrumentation.
- Keep `enable_performance_counters=False` in telemetry setup to avoid local hangs in some environments.
- Include both `name` and `id` in `agent_reference` for best visibility in Foundry Trace (Preview).
- If telemetry setup gets stuck after multiple reruns, restart the kernel and rerun the cells above in sequence.

---

## Validation checklist (quick smoke test)

- Run Cells 7, 9, 11, 13, and 15 in order after a fresh kernel restart.
- Confirm Cell 11 prints completion for all three telemetry steps and ends with `Tracer ready ✅`.
- In Foundry Trace (Preview), verify you see spans for both `agent-creation` and `agent-query` for the latest run.
- In Application Insights, verify fresh telemetry arrives for the same execution window and includes agent/query-related spans.

---

## Resources

- https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/sdk-overview?pivots=programming-language-python#foundry-tools-sdks
- https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-setup?view=foundry
- https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects#tracing
- https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects/samples/agents/telemetry
