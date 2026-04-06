# Executive Summary: Worker Refactor, Agent Framework Adoption, and Observability Enhancements

## Overview

This work modernized the bot and worker architecture without disrupting the existing Teams hosting boundary. The core design choice was to keep the Teams transport and handler model on the Microsoft 365 Agents SDK, while introducing the newer Agent Framework SDK selectively behind existing command handlers. At the same time, the worker automation path was hardened for Azure-hosted execution, and the notebook observability story was upgraded so both the main agent flow and the Microsoft Sentinel MCP flow are now easier to trace and reason about.

The net result is a system that is more reliable, more observable, and easier to evolve. The bot remains responsive for interactive chat scenarios, the worker remains the execution boundary for long-running infrastructure actions, and the notebook now provides much better end-to-end telemetry across both general Foundry interactions and Sentinel-specific flows.

## Worker Refactor

The worker remained the execution boundary for long-running `build`, `teardown`, `build status`, and `list builds` operations, but the surrounding implementation was refactored to make it more deterministic and operationally safe.

Key outcomes:

- The queue-backed worker path was preserved as the authoritative execution model for long-running Azure deployment operations.
- Worker build metadata handling was fixed so the runtime can reliably identify the exact build information associated with the running worker image.
- Requester identity is now passed through more consistently during build and teardown handoff, which improves traceability and operational auditing.
- Worker deployment moved toward immutable image tagging instead of relying on a mutable `latest` image, which improves deployment determinism and rollback safety.
- The bot lifecycle and worker lifecycle were further separated so heartbeat and status broadcasting can remain healthy even when the worker path is disabled or isolated.

In practical terms, this refactor reduced coupling between the bot ingress path and the infrastructure execution path, which lowers operational risk and makes failures easier to isolate.

## Why Azure CLI Was Installed with `apt-get`, Not `pip`

One of the most important hardening changes was in the worker container image. The worker now installs Azure CLI from Microsoft’s supported Debian package repository via `apt-get`, rather than through `pip install azure-cli`.

That change was necessary because the pip-installed Azure CLI inside Azure Container Instances proved unreliable for managed-identity authentication. Specifically, `az login --identity` could fail during `build it` and `teardown` operations even when the container’s managed identity and Az PowerShell authentication were otherwise healthy.

Installing Azure CLI from Microsoft’s native Debian feed solved a real production problem:

- It aligned the worker image with Microsoft’s supported Azure CLI packaging model.
- It avoided Python packaging drift and dependency mismatches inside the worker container.
- It materially improved the reliability of managed-identity bootstrap in Azure-hosted automation.

This change was foundational because the worker automation path depends on dependable non-interactive Azure CLI authentication.

## New Agent Framework SDK Adoption

The new Agent Framework SDK was introduced incrementally rather than through a full runtime rewrite. The bot continues to use the Microsoft 365 Agents SDK at the Teams transport boundary, but an optional internal `AgentFrameworkCommandOrchestrator` was added behind the existing handlers.

This orchestration layer currently targets two scenarios:

- Microsoft Learn grounding for the `msft_docs` command
- Build-guidance responses for the `build it` workflow

Key benefits of this rollout approach:

- It reduced migration risk by keeping the existing Teams host stable.
- It added Agent Framework only where orchestration value was highest.
- It preserved direct-service fallbacks so the bot still functions even when Agent Framework configuration is absent or disabled.
- It kept worker execution, queue dispatch, Graph usage, and storage boundaries unchanged.

This was a deliberate modernization strategy: adopt the new SDK where it improves composition and prompt orchestration, but do not destabilize the operational boundaries that already work.

## Why `httpx` Was Important

`httpx` played an important role in both runtime behavior and observability.

On the runtime side, `httpx` remains part of the MCP client transport path, which is why it stayed in the supported dependency set during the refactor.

On the observability side, `httpx` became significantly more important because we enabled `HTTPXClientInstrumentor` in the notebook tracing flow. That was done to expose the outbound HTTP dependency edges used by the OpenAI / Responses API client stack.

Why this mattered:

- Foundry preview traces alone were not enough to show the full client-side dependency chain in Application Insights and Log Analytics.
- Instrumenting `httpx` allowed the notebook to emit concrete `AppDependencies` rows for outbound `responses.create(...)` calls.
- This made Azure Service Map and end-to-end dependency correlation much more useful.

In short, `httpx` instrumentation closed an important telemetry gap and made the notebook’s tracing story operationally credible, not just conceptually correct.

## Microsoft Sentinel MCP as Its Own Traced Agent

The Microsoft Sentinel MCP path was intentionally kept separate from the main storytelling and Microsoft Learn flow. Rather than treating Sentinel as just another tool invocation buried inside the main path, it now behaves as its own project-backed agent with its own invocation lifecycle.

What changed:

- A dedicated Sentinel project agent is created separately from the main demo agent.
- Sentinel uses its own `agent_reference`, its own prompt, and its own tracing context.
- The Sentinel flow stays on the existing Foundry project-connected MCP dependency because that is the correct path for OAuth passthrough and project connection binding.
- Sentinel runs are now traced independently and persisted independently.

Operationally, this was an important design improvement because Sentinel has different identity, workspace-resolution, and consent requirements than the public Microsoft Learn MCP path.

This design now provides:

- Better failure isolation between main agent behavior and Sentinel-specific issues
- Better trace clarity in Foundry, Application Insights, and Log Analytics
- Clearer reasoning about Sentinel as a first-class agentic flow rather than an implementation detail

Additional Sentinel-specific improvements included instructing the agent to call `list_sentinel_workspaces` first and reuse the returned `workspaceId`, as well as passing the preferred workspace identifier as a resolution hint to reduce empty-workspace tool failures.

## Unit Tests Added and What They Accomplish

A meaningful amount of unit test coverage was added during this work, and those tests serve as regression protection for the most important behavioral changes.

### Agent Framework orchestration tests

Tests were added for the new orchestration layer to validate:

- fallback to direct Microsoft Learn lookup when Agent Framework is disabled
- use of the Agent Framework runner when enabled
- fallback build-guidance behavior when the orchestration layer is unavailable

This ensures the new Agent Framework integration is additive, not brittle.

### Authentication retry tests

Tests were added for transient JWT / JWKS authorization failures to validate:

- retry behavior for transient connection-reset and JWKS fetch issues
- non-retry behavior for non-transient token failures
- correct classification of transient failures through exception-chain analysis

This improves resilience at the bot’s authorization boundary.

### Worker and teardown tests

Worker-related tests were expanded to validate:

- teardown command parsing
- teardown session defaults
- requester identity propagation into teardown execution
- completion message chunking behavior
- active and orphaned build-info discovery behavior
- worker build metadata lookup correctness

This improves confidence in the operational flows that are most expensive to get wrong.

### Heartbeat and lifecycle tests

Additional tests validate:

- default and overridden heartbeat intervals
- heartbeat payload content
- lifecycle behavior when heartbeat runs independently of the worker
- lifecycle behavior when both worker and heartbeat run together

This confirms that background service separation works as intended.

### PowerShell managed-identity auth helper tests

PowerShell tests were added for managed-identity Azure CLI bootstrap helper logic. These validate retry behavior and failure behavior when a usable CLI context cannot be established.

That matters because it directly protects the Azure-hosted worker automation path from transient managed-identity login failures.

## Other Notable Improvements

Several other changes are worth noting as part of the broader modernization effort:

- The bot entry point was cleaned up with more explicit lifecycle handling and retry-aware authorization middleware.
- The Teams bot remained on the M365 Agents SDK handler model while still gaining an internal Agent Framework orchestration path.
- The runtime and deployment documentation were updated to explain the migration strategy, supported dependency matrix, and operational rollout shape.
- The notebook tracing model was improved with explicit dependency spans and better trace correlation across Foundry, Application Insights, and Log Analytics.
- Worker-side Azure authentication bootstrap was hardened so managed-identity execution behaves more consistently in Azure-hosted automation.

## Bottom Line

This was not a cosmetic refactor. It was a structural modernization effort that improved reliability, observability, and maintainability across the bot, worker, deployment, and notebook layers.

The main business and engineering outcomes were:

- a safer and more deterministic worker runtime
- a supported and more reliable Azure CLI installation model
- better observability through `httpx` instrumentation and explicit dependency spans
- a properly isolated and independently traced Sentinel MCP agent flow
- a low-risk, incremental adoption of the new Agent Framework SDK
- stronger test coverage around the most operationally sensitive behaviors

Taken together, these changes put the system on a better foundation for future expansion without sacrificing the boundaries that already worked well in production-oriented scenarios.
