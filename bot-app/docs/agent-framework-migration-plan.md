# Agent Framework Migration Plan

## Scope Guardrails

- Keep the Teams bot host on `microsoft_agents.*`.
- Keep Graph, storage, MCP client, and worker code as-is.
- Add Microsoft Agent Framework only behind existing Teams handlers.
- Start with `msft_docs` and build-guidance orchestration; leave actual build queueing and worker execution unchanged.

## Rollout Shape

1. Add an optional `AgentFrameworkCommandOrchestrator` behind the current handler registration.
2. Use Agent Framework for `msft_docs` and build-guidance prompts when `BOT_AGENT_FRAMEWORK_ENABLED=true`.
3. Keep direct-service fallbacks so the bot still works when Agent Framework configuration is absent.
4. Reuse the same runtime requirements in both bot and worker images.
5. Run unit tests before smoke checks; optionally publish and deploy both images from the smoke script.

## Keep/Replace Matrix

### Bot And Worker Runtime

| Package | Current Role | Decision | Action |
|---|---|---|---|
| `microsoft-agents-activity` | Teams activity models/config | Keep | Bot transport stays on M365 Agents SDK |
| `microsoft-agents-hosting-core` | AgentApplication/turn host | Keep | Do not replace Teams host boundary |
| `microsoft-agents-hosting-aiohttp` | aiohttp adapter + auth pipeline | Keep | Required for current bot ingress |
| `microsoft-agents-hosting-teams` | Teams helpers | Keep | Required for Teams member lookups |
| `microsoft-agents-authentication-msal` | Bot auth | Keep | Required for current channel auth |
| `aiohttp` | Web server + outbound HTTP | Keep | Runtime transport library |
| `python-dotenv` | Local env loading | Keep | Dev/runtime config helper |
| `psutil` | Heartbeat metrics | Keep | Local process telemetry |
| `msgraph-sdk` | Team/channel setup | Keep | Not an agent-framework concern |
| `azure-identity` | Managed identity auth | Keep | Shared Azure auth across runtime and worker |
| `azure-storage-queue` | Job dispatch queue | Keep | Worker handoff remains unchanged |
| `azure-storage-blob` | Conversation/reference store | Keep | Storage boundary remains unchanged |
| `azure-mgmt-resource` | List resource groups for teardown/build status | Keep | ARM management dependency |
| `mcp` | Direct MCP client for Microsoft Learn | Keep | Still the grounding source behind the docs tool |
| `httpx` | Streamable HTTP transport for MCP | Keep | Runtime dependency of MCP client path |
| `openai` | Weather narration client | Keep | Separate from Teams command orchestration |
| `agent-framework-core` | Internal orchestration layer | Add | New optional command orchestration runtime |
| `agent-framework-openai` | Azure OpenAI-backed Agent Framework model client | Add | New optional OpenAI Responses provider for orchestration |

### Notebook And Observability Stack

| Package | Current Role | Decision | Action |
|---|---|---|---|
| `azure-ai-projects` | Direct Foundry project/agent management | Keep for now | Revisit only after telemetry parity is proven |
| `azure-identity` | Notebook auth | Keep | Shared Azure auth dependency |
| `azure-monitor-opentelemetry` | Azure Monitor exporter bootstrap | Keep | Required for current observability story |
| `azure-monitor-opentelemetry-exporter` | Exporter compatibility path | Keep | Leave notebook tracing path stable |
| `azure-core-tracing-opentelemetry` | Azure SDK tracing bridge | Keep | Required for current trace pipeline |
| `opentelemetry-sdk` | Trace runtime | Keep | Core observability dependency |
| `opentelemetry-api` | Trace API surface | Keep | Core observability dependency |
| `ipykernel` | Notebook kernel | Keep | Development/runtime prerequisite |

## Environment Variables For The New Orchestration Layer

| Variable | Purpose |
|---|---|
| `BOT_AGENT_FRAMEWORK_ENABLED` | Enables Agent Framework behind Teams handlers |
| `BOT_AGENT_FRAMEWORK_ENDPOINT` | Overrides the Azure OpenAI endpoint used by the orchestration agent |
| `BOT_AGENT_FRAMEWORK_DEPLOYMENT_NAME` | Overrides the Azure OpenAI responses deployment name |
| `BOT_AGENT_FRAMEWORK_API_VERSION` | Optional Azure OpenAI API version override |

The orchestration layer should use the same user-assigned managed identity already mounted into the bot and worker containers. In this repo that means `DefaultAzureCredential(managed_identity_client_id=AZURE_CLIENT_ID)` plus the long-lived bot-owned Azure OpenAI endpoint, not a separate Foundry project endpoint that would require additional RBAC.

## Smoke-Test Workflow

```bash
RUN_UNIT_TESTS=true SMOKE_PUBLISH_AND_DEPLOY=true bash deployment/run-smoke-checks.sh
```

This flow now:

1. Runs the runtime unit tests.
2. Publishes and deploys the bot image when requested.
3. Publishes and deploys the worker image when requested.
4. Verifies the bot Container App revision state.
5. Executes a live HTTP health probe against `GET /api/messages`.
6. Verifies the worker container state and build metadata.