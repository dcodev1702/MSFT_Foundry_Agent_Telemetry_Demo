# Changelog

Notable changes, newest first. Historical entries cover the 340 commits reachable
from `main` through `4ec2fac` (2026-08-22), beginning with `630593a` (2026-03-02).
The local repository is not shallow and has no release tags; dates below are
recorded commit dates, not published release dates. Related changes, merges,
formatting edits, and notebook-output refreshes are consolidated. Local stash
snapshots are excluded.

## Unreleased — Telemetry policy and dependency simplification

- Enforce trace-only export using `OTEL_LOGS_EXPORTER=none`,
  `OTEL_METRICS_EXPORTER=none`, `enable_live_metrics=False` and disabled performance
  counters. Azure Monitor 1.8.10 overwrites the old `disable_logging` /
  `disable_metrics` arguments; the previous trace-only description was inaccurate.
- Use one strict, normalized content-recording flag for SDK and custom spans;
  remove the obsolete Azure flag and the unrelated Agent Framework GenAI opt-in.
- Fix sampling at 100% despite inherited sampler settings. Reject disabled tracing
  and changed/partially failed setup; reuse providers only for identical reruns.
- Let Azure Monitor own HTTPX/HTTPX2 instrumentation. Correct the earlier claim
  that HTTPX instrumentation 0.65b0 lacked HTTPX2 support.
- Replace the Agent Framework resource helper with native OpenTelemetry resources,
  preserving identity and additional resource attributes while adding
  `deployment.environment.name`.
- Split minimal runtime, optional shared packages and validation requirements;
  add a reviewed 100-package Windows/Python 3.14 constraints snapshot. The minimal
  runtime resolves to 82 packages, down from 92. Existing shared packages remain
  installed; tested SDK versions, including Azure Identity, are unchanged.
- Tighten Sentinel instructions after observed KQL errors: return datetime values
  unchanged and use projection aliases without spaces; apply display labels in
  the final answer instead.
- Pass 25 regression tests in both the existing and a clean minimal environment.
  Run all 13 notebook code cells successfully; verify 55 correlated spans, eight
  response dependencies, 13 GenAI spans and zero failed spans. Also verify that
  SDK log/metric providers and Live Metrics/performance-counter processors are
  absent, SDK/custom content capture is off, and HTTPX2 is instrumented.
- Recheck the reduced 82-package runtime against PyPI advisory metadata: no
  reported advisories or unavailable metadata entries.
- Update notebook notes, README and observability guidance; leave the notebook
  outputs cleared.

## 2026-09-14 — Windows dependency and telemetry validation

- Improve spacing and readability in the first three code cells of
  `zolab-ai-agent-demo-win11.ipynb`.
- Add `requirements-notebook.txt` with the resolved Windows SDK matrix:
  Agent Framework core 1.17.0 / OpenAI provider 1.14.2, Azure AI Projects 2.6.0,
  OpenAI 3.8.0, Azure Monitor 1.8.10, and the aligned OpenTelemetry 1.44 / 0.65
  instrumentation train. Remove the former Projects `<2.5` restriction.
- Patch HTTPX2 to 2.12.0 after dependency advisory checks; align the shared
  environment's optional OTLP exporter to 1.44.0 and run `pip check` during setup.
- Fix Sentinel response dependencies losing their parent trace context, and
  add run/interaction correlation attributes to its orchestration span.
- Require plain KQL after Sentinel schema discovery; reject terminal tool
  errors, empty/failed responses and exhausted approval loops.
- Replace the historical/empty-result query display with a current-run telemetry
  gate: resolve the linked workspace, flush, wait for ingestion, and check each
  interaction's response dependency, GenAI spans, failures and service/version.
- Default content recording to off, gate custom payload/identity attributes,
  and remove the target UPN from propagated baggage.
- Update notebook notes, README and observability guidance to describe the actual
  project-backed execution path, HTTPX2 tracing limitations and tested matrix.
- Validate all 13 code cells against Azure, including Learn and Sentinel MCP,
  persistence and Marp generation. Observe 55 correlated dependency spans,
  eight response dependencies, 13 GenAI spans and zero failed spans in the final
  run. Add 12 passing local regression tests and clear notebook outputs.
- Ignore newly generated main/Sentinel Marp decks so private validation output
  is not accidentally committed; existing tracked examples remain unchanged.
- Backfill this changelog from the available Git history through the initial
  March 2, 2026 commit; historical dates are not versioned releases.

## 2026-08-22 — Python 3.14 notebook setup

- Update the Windows notebook setup and kernel-selection checks for Python 3.14,
  while retaining Python 3.13+ guidance for macOS.
- Refresh dependency constraints, including the Azure AI Projects `<2.5` bound
  for compatibility with the Agent Framework OpenAI dependency, and archive the
  pre-upgrade Python 3.13.14 package inventory. (`4ec2fac`)

## 2026-07-03 — Deployment documentation

- Refresh the bot deployment architecture diagram. (`0684ff8`)

## 2026-07-02 — Azure resource client compatibility

- Add a shared `ResourceManagementClient` import fallback for the bot and worker
  to accommodate Azure management SDK package layouts. (`9c1f1cb`)

## 2026-07-01 — Cross-subscription recovery

- Extend RBAC repair to restore build and Log Analytics access across
  subscriptions, and support explicit subscription-ID overrides.
- Allow read-only build inventory/status operations to continue when the
  Security subscription is unavailable; deployment and cleanup still require
  that access. (`e133da9`)

## 2026-06-11 — Aspire startup and observability guidance

- Harden the Agent Framework demo's Aspire startup with Docker Desktop
  readiness handling, available-port selection, and console-exporter fallback.
- Add an observability demo talk track and an import/library alignment appendix;
  clean generated demo artifacts. (`7ba2501`, `c8f6004`, `9ae0630`)

## 2026-06-10 — RBAC repair tooling

- Add `deployment\repair-bot-rbac.ps1` and runbook guidance for restoring bot and
  worker role assignments removed by governance sweeps. (`c879a36`)

## 2026-06-08 — Documentation and workflow definition

- Add SVG architecture, runtime, and walkthrough diagrams for the Agent
  Framework PoC, and update its README to use them.
- Refresh Windows notebook dependency documentation and add a corrected Logic
  App workflow definition in `logicapp.json`. (`725c043`, `dbec448`, `7be3a89`,
  `98f855b`)

## 2026-05-14 — Sentinel diagnostics and telemetry refresh

- Improve Sentinel MCP failure diagnostics with workspace, subscription,
  identity, and PIM/RBAC troubleshooting context.
- Refresh notebook package requirements and telemetry service-version metadata;
  refine Microsoft Learn prompts.
- Update observability guidance and stack diagrams, and add example main and
  Sentinel presentation outputs. (`10e92f0`, `1b6abb3`, `977d148`, `d6fdc96`)

## 2026-04-16 — Marp presentation output

- Generate dark-themed Marp Markdown decks for the main project-agent and
  Sentinel flows, alongside persisted results and run metadata. (`7d1d25c`)

## 2026-04-07 to 2026-04-08 — Dedicated Agent Framework demo

- Add the Agent Framework SDK notebook, PoC README, and MCP helper; move them
  into the dedicated `agent-framework-demo` directory.
- Refine telemetry, local-agent revision tracking, tool metadata, and helper
  generation; suppress Azure CLI upgrade checks in the worker image.
  (`afd0417`, `f632907`, `3f7d53f`, `d683488`, `3f56f08`)

## 2026-04-05 to 2026-04-06 — Agent orchestration and trace correlation

- Update the primary notebook and add optional Agent Framework orchestration
  for bot Microsoft Learn and build-guidance requests, preserving queue-backed
  build/teardown execution. (`5c17bb8`)
- Restore Foundry trace correlation, add response-client dependency spans, and
  refine Sentinel identity, approval, workspace-resolution, and query handling.
- Improve local authentication behavior and document agent roles, telemetry
  phases, and the refactor in `observability.md` and related guidance.
  (`8d9bdfe`, `3a470bb`, `84e0df3`, `3e033ff`, `1682099`)

## 2026-04-03 — Sentinel MCP integration

- Extend the Windows notebook with optional Microsoft Sentinel Data Exploration
  MCP configuration, OAuth-passthrough project-connection discovery, and
  Sentinel query flow. (`cd4544b`)

## 2026-03-28 to 2026-03-29 — Build and authentication resilience

- Harden Foundry cleanup and worker inventory; fix managed-identity deployment
  parsing and worker build-metadata lookup.
- Improve bot authentication retries and shell preflight portability, simplify
  tracing status, and restore local notebook IMDS suppression.
  (`3eb323b`, `779a3b9`, `5b4b2aa`, `d1a660a`, `053abbe`)

## 2026-03-22 to 2026-03-24 — Deployment configuration and preflight

- Parameterize deployment environment identifiers and remove remaining
  hard-coded deployment IDs.
- Strengthen purge retries and teardown boundaries; add deployment/private-DNS
  preflight checks, Log Analytics shared-key access requirements, and worker
  endpoint synchronization checks.
- Increase the bot heartbeat default in stages to six hours and refresh
  deployment architecture documentation. (`06c2fb3`, `66f1a26`, `c5622dc`,
  `8eb3142`, `aec1518`, `a8d93e1`)

## 2026-03-11 to 2026-03-14 — Bot identity, networking, and operations

- Cut over bot deployment to managed identity, improve worker identity
  bootstrap, and introduce private storage networking for bot and worker.
  (`ee50b77`, `b681e84`, `25f981a`)
- Add weather and Microsoft Learn commands, give the bot a model endpoint
  independent of disposable Foundry builds, and refine weather matching,
  retries, and Fahrenheit output. (`45ecd85`, `82f77c0`, `bf5bbe8`)
- Reorganize bot runtime files, stabilize requester identity and image rollouts,
  improve heartbeat/status messaging and build-info downloads, and harden
  targeted teardown cleanup.
- Add smoke-check scripts and an operations runbook; refresh Teams packaging,
  architecture documentation, and the MacBook notebook.
  (`03fe2e5`, `ff6a839`, `cb7d172`)

## 2026-03-10 — Hosted bot and queue-backed worker

- Add Azure Container Instances worker infrastructure, Azure Queue/Blob Storage
  persistence, Bot Service, and a Teams channel.
- Move bot hosting from the App Service approach to Azure Container Apps and
  route runtime logs to the shared Log Analytics workspace.
- Add the sideloadable Teams app package and Bot-The-Builder branding; refine
  interactive teardown, build-info delivery, managed-identity authentication,
  and worker build-status recovery. (`2f49ee3`, `814d7d0`, `f357cbc`, `510b236`)

## 2026-03-08 to 2026-03-09 — Microsoft 365 Agents SDK migration

- Migrate the Teams bot to the Microsoft 365 Agents SDK with worker,
  conversation-state, proactive-message, and heartbeat components.
- Add local Agents Playground configuration, interactive model/build and
  teardown selection, build-info attachments, confirmation/abort prompts, and
  consistent five-minute interaction timeouts.
- Parameterize Bicep values and harden deployment scripts for Windows and
  macOS. (`86da6cb`, `ba037d1`, `175015a`, `1d08a7a`, `a66f391`, `a9cb722`)

## 2026-03-07 — Bot sample and Teams workflow refinement

- Add the Python Teams bot sample and teardown menu, then reorganize bot code
  and design documentation under `bot-app`.
- Improve Teams startup, heartbeat, progress reporting, empty-message handling,
  deployment access, and shared-resource teardown protections.
- Refine notebook tracing and capture deployment build metadata in story
  results. (`26ceba8`, `70fbe2b`, `8add5f0`, `084a5e0`, `e6ad27e`)

## 2026-03-06 — Deployment metadata and Teams automation

- Expose deployment endpoints and `build_info` output for notebook setup; add
  selectable deployment models and cleanup of generated build metadata.
- Introduce Teams chat/command automation and expand listener operations.
  (`c524648`, `7936672`, `8a7dbd6`, `296bb06`, `f1534ab`)

## 2026-03-05 — Deployment lifecycle and telemetry connections

- Add randomized deployment suffixes and deployer-group membership lifecycle
  management.
- Connect Application Insights to Foundry, refine connection-string and project
  scoping, and resolve Log Analytics workspace details dynamically.
- Add the `gpt-5.3-chat` deployment and correct its SKU to `GlobalStandard`.
  (`763ee18`, `c08e85f`, `97d6027`, `602f7dd`, `df51553`, `f5f1532`)

## 2026-03-04 — Infrastructure as code

- Add Bicep infrastructure and PowerShell deployment automation with RBAC and
  Log Analytics integration.
- Resolve subscriptions dynamically, parameterize the Security subscription,
  and add Key Vault and Blob Storage diagnostic settings.
- Add deployment prerequisites, usage, and architecture documentation, and
  reorganize the root README. (`64df4cb`, `aa3bbe2`, `0388807`, `85888cd`,
  `c58f673`, `105c1a6`)

## 2026-03-03 — MCP flows and notebook usability

- Separate story generation from Microsoft Learn inquiries and refine MCP
  approval handling across the Windows and MacBook notebooks.
- Align authentication, prerequisites, validation, and telemetry guidance;
  improve collapsible sections, output presentation, and notebook readability.
- Ignore virtual environments and locally generated story output.
  (`9c50677`, `a0d0df1`, `653dd93`, `eab0822`, `fbb7443`)

## 2026-03-02 — Initial project

- Create the repository with its introductory README and MIT license.
  (`630593a`)
- Add the original Foundry agent notebook and a Windows 11 variant; rename the
  original to `zolab-ai-agent-demo-macbook.ipynb`.
- Establish virtual-environment/kernel setup, agent creation and querying,
  story persistence, and Azure Monitor/Application Insights tracing guidance.
  (`676a01f`, `604b301`, `e3e8fe5`)
