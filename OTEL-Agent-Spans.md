# OpenTelemetry Agent Spans

## Backend agent endpoint migration

The Windows notebook now supports an explicit **`agent_endpoint`** mode through
[notebook_agent_endpoints.py](notebook_agent_endpoints.py). This uses separate
new-model backend agents with unique identities and fixed version pins, not
Teams/M365 publishing. The original agents and **`project`** mode are retained
for rollback.

| Role | Original agent | New backend agent | Initial migration version |
|---|---|---|---|
| Story / Learn | `ZoDEfendersAgent-1702` | `ZoDEfendersAgent-1702-backend` | `1` |
| Sentinel | `ZoDEfendersAgent-1702-sentinel` | `ZoDEfendersAgent-1702-sentinel-backend` | `1` |

Both backend agents use the existing Terra deployment and unchanged MCP
definitions. They expose Responses with Entra authorization. A direct two-turn
test verified conversation continuation on the new endpoint; unauthenticated
requests to both endpoints were rejected with HTTP 401. No additional role
assignments or OAuth consent were needed for the tested caller/tool path.

### Telemetry changes

- **Section 4:** in backend mode, `resolve_agent` wraps reads of the agent and
  selected version. It verifies unique identity, enabled state, Responses/Entra
  configuration, 100% fixed-version routing and equality with the notebook
  definition under strict `pinned` policy. The demo's newer `sync` policy is
  described below and can activate changed definitions. The shared model
  metadata, request IDs and configuration fingerprint remain present.
- **Identity metadata:** setup spans also record
  `app.agent.identity.principal_id`, `app.agent.identity.client_id` and
  `app.agent.active_version`. These are agent identities, not the caller's UPN.
- **Sections 5/5.1:** each flow uses a client bound to its agent's stable endpoint.
  The request contains no project-dispatch `agent_reference`. The client SDK
  may name its inner span `responses`; the service still identifies the actual
  agent/version in its own spans.
- **HTTP dependency URLs:** now end in
  `/agents/{agent}/endpoint/protocols/openai/responses`. The shared URL builder
  also supports `/openai/v1/responses` for explicit rollback without duplicating
  path segments.
- **Section 6:** counts a Responses wrapper only when its name ends with
  `/responses` and its `gen_ai.operation.name` is `responses.create`. The old
  project-only suffix test would have missed valid backend requests.
- **Records and decks:** record `agent_invocation_mode`; Marp runtime labels say
  `responses.create + agent endpoint` in backend mode. Persistence correlation,
  model version footers and privacy controls are retained.

The 49/50-span inventories below are historical **project-endpoint** runs.
They remain valid audit evidence, but are not fixed expected counts or exact
span names for backend mode. See [observability.md](observability.md#backend-endpoint-migration)
and [runtime configuration / rollback](README.md#backend-agent-endpoints).

### Version synchronization: current demo policy

The initial read-only pin policy was too restrictive for notebook edits.
The demo now explicitly selects `backend_version_policy=sync`:

1. Read the actual fixed active version; do not assume the local checkpoint is current.
2. Reuse its definition if it matches the notebook; otherwise reuse a matching
   latest candidate or call `create_version`.
3. Verify the returned definition, check for intervening endpoint/identity changes,
   and update only the fixed-version selector.
4. Read back the endpoint and identity, then persist the selected version locally
   and update the notebook runtime object.

Sync does not delete old versions, switch identities, use `@latest`, remove
authorization or silently fall back to the project endpoint. It updates the
version served to **all consumers** of the stable endpoint; it verifies definition
and routing consistency, not a complete evaluation suite before activation.
Use `pinned` policy when release approval must remain separate.

Setup spans use `sync_agent` and retain the existing diagnostic fields. New fields
are `app.agent.version_policy`, `app.agent.version_action`,
`app.agent.previous_version` and the verified `app.agent.active_version`.
Activation and local checkpoint completion have separate events. POST/PATCH
dependency spans occur when needed; unchanged reruns perform reads only.

**Validated run:** `447909c6-f671-4146-bade-cf841fd3644a` completed all 10 runtime
cells and live assertions (exit 0). Both actual edited definitions were activated
as version **2** from version **1**, and local/runtime selections matched the
endpoint. Identity and endpoint continuity, retained version 1, real Learn and
SigninLogs calls, response metadata and persistence all passed.
The gate snapshot was **64 spans, 6 Responses dependencies, 1 persistence span,
0 failures**. Two extra unchanged sync rounds per agent were run with create and
update calls blocked: version 2 was reused and the local file was unchanged.
All **80 regression tests** pass.

If cloud activation succeeds but local saving fails, the error explicitly names
the active version. Rerunning sync reconciles the checkpoint. Concurrent edits
are checked before endpoint activation and before atomic local-file replacement;
there is no cross-service/file transaction or guarantee against races after those
checks. Avoid competing releases. All earlier inventories below are historical
snapshots of their stated version policies.

### Backend validation snapshot (initial pinned policy)

Run `b4591fc5-2289-4527-8e81-a9e97e153f34` used the saved local backend
configuration, with no injected candidate settings. All **10 runtime cells** and
the live data/model/identity/pin/persistence checks completed with **exit code 0**.
All **65 regression tests** passed. Existing environment setup cells were skipped;
there were no dependency installs or interactive authentication prompts.

| Cell / work | Observed spans |
|---|---:|
| `586f0511`: main pinned-agent setup | 5 |
| `586f0511`: Sentinel pinned-agent setup | 5 |
| `2692d274`: story | 6 |
| `2692d274`: Learn | 18 |
| `2692d274`: persistence | 1 |
| `ef551c01`: Sentinel | 19 |
| **Total** | **54** |

Each five-span setup operation consists of one explicit `resolve_agent` span,
one SDK agent-detail GET span and its HTTP dependency, and one SDK version GET
span and its HTTP dependency. Model/request-ID/fingerprint metadata is on the
explicit setup span. The setup introduces no model call or version promotion.

The snapshot contained **7 Responses dependencies**, **11 service chat spans**,
**1 correlated persistence span**, **zero failures**, and **no `search_tables`**.
The actual dependencies used the configured agents' stable endpoint paths.
The main and Sentinel answers, real tool calls, sign-in identity/IP and saved
Marp metadata were checked. Both replacement agents stayed pinned at version 1;
the original agent definitions/versions were preserved.
The agent identity/client IDs and active version were also verified on the
actual setup spans. Both exported deck types passed browser checks: correct
backend runtime label and unclipped model metadata footers on all seven slides.

This is four more spans than the comparable 50-span project-mode run because
setup performs two reads per agent rather than one creation request. It is not
a fixed backend span-count requirement. In the earlier candidate rehearsal,
additional model/tool work produced a different total.

## Shared diagnostics and project-mode baseline

Updated September 15, 2026. The existing main/Sentinel creation spans are now
enriched, and `persist_story` is explicitly run-correlated. **No synthetic
Foundry service span or additional creation wrapper was introduced.**

The [historical 49-span inventory](#historical-49-spans) remains below as an audit
snapshot of the earlier run. Its excluded persistence span and old code excerpts
describe that earlier execution, not the corrected current implementation.

### Creation attributes

In project mode, both explicit `create_agent` spans in Section 4 / cell `586f0511` add the
following attributes while preserving their existing GenAI/run/tool attributes:

| Attribute | Purpose / source |
|---|---|
| `app.model.publisher` | Verified deployment publisher, such as `OpenAI`; not the telemetry service role. |
| `app.model.name` | Underlying model name, such as `gpt-5.6-terra`; not a guessed deployment alias. |
| `app.model.version` | Publisher-specific model version from the deployment metadata. |
| `app.model.deployment` | Deployment name supplied to the agent definition. |
| `app.agent.config.sha256` | Stable 64-character lowercase SHA-256 digest of canonical agent-definition/model JSON. |
| `app.azure.request_id` | Preferred nonempty request ID from an allowlisted response header. |
| `app.azure.request_id_header` | The header from which that request ID was obtained. |
| `app.azure.apim_request_id` | Separate API Management gateway request ID, when present. |
| `app.azure.request_id_available` | Initially false; true only when an actual response request ID is found. |

The request-ID preference order is `x-request-id`, `x-ms-request-id`,
`apim-request-id`, then `request-id`; header-name matching is case-insensitive.
Only those IDs are exported. Authorization/cookie/other headers are not copied.
A received response without an ID adds the
`create_agent.request_id_unavailable` event rather than inventing an ID. The
exception path also inspects the error response, records the exception/type,
marks the creation span failed and rethrows.

Fingerprint inputs are:

```python
model = {field: metadata[field] for field in ("type", "name", "version", "deployment")}
configuration = json.dumps(
    {"definition": definition.as_dict(), "model": model},
    sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
)
span.set_attribute(
    "app.agent.config.sha256",
    hashlib.sha256(configuration.encode("utf-8")).hexdigest(),
)
```

The complete helper adds model attributes too. Object-key order does not change
the digest; array order remains meaningful. Instructions, tools and resolved model
version changes affect the digest. Run IDs, request IDs and the returned agent
version are not included. A fingerprint is a comparison aid, not encryption:
existing prompt/content capture is unchanged and may still export sensitive
content when enabled.

In project mode, the actual definition used to compute the fingerprint is also sent to the SDK:

```python
main_agent_definition = PromptAgentDefinition(
    model=model_name,
    instructions=main_agent_instructions,
    tools=[mcp_tool_spec],
)
add_agent_creation_metadata(span, main_agent_definition, model_deployment_metadata)
main_agent = project_client.agents.create_version(
    agent_name=main_agent_name,
    definition=main_agent_definition,
    raw_response_hook=make_creation_response_hook(span),
)
```

The Sentinel path uses the same enrichment/hook helpers with its own definition.
The hook returns the unmodified pipeline response, so it does not replace or
transform the SDK's returned agent object.

### Persistence correlation and validation

Section 5 / cell `2692d274` retains the existing internal persistence span and
explicitly labels it instead of relying on baggage:

```python
with tracer.start_as_current_span("persist_story", context=persist_context) as persist_span:
    persist_span.set_attribute("demo.run_id", run_id)
    persist_span.set_attribute("app.session.id", session_id)
    persist_span.set_attribute("app.interaction", "persistence")
    persist_span.set_attribute("gen_ai.agent.name", main_agent_display_name)
    persist_span.set_attribute("gen_ai.agent.id", main_agent_id)
    persist_span.set_attribute("gen_ai.agent.version", main_agent_version)
    persist_span.set_attribute("agent.runtime", "azure.ai.projects")
```

The rest of the block writes the existing record and attributes. Write errors
retain run context, add `error.type`, and propagate through the span context
manager, which records the exception and error status.

Persistence remains a **separate operation**: the change makes it discoverable by
`demo.run_id`, not a child of whichever story/facts span happened to finish last.
It is not an LLM interaction and must not be required to have a Responses request.
Section 6 / cell `6e3dcab6` now summarizes:

```kusto
PersistenceSpans=countif(Name == "persist_story" and RootInteraction == "persistence")
```

The ingestion success condition requires `coverage["PersistenceSpans"] == 1`
alongside the existing model-interaction/Responses coverage and zero failures.
The gate waits for persistence ingestion rather than accepting a run before that
span arrives. Total span count is not hardcoded; model/tool/approval behavior can
still vary.

### Model-version representation in Log Analytics

The first enhanced live check exposed a representation difference:
`app.model.version` is sent as the string `2026-07-09`, but the dynamic property
bag exposed it as a **datetime**, `2026-07-09T00:00:00.0000000Z`.
A read-only `gettype(...)` query confirmed this for both creation spans.
For display/validation, normalize only datetime-typed values:

```kusto
| extend ModelVersion=iff(
    gettype(Properties["app.model.version"]) == "datetime",
    format_datetime(todatetime(Properties["app.model.version"]), "yyyy-MM-dd"),
    tostring(Properties["app.model.version"]))
```

This preserves non-date version strings. It is not a fallback to a different
model, a fingerprint-input change or a modification to the Sentinel KQL query.

## Latest validated run: 50 correlated spans

**Run:** `2855bde8-64be-45fd-b58f-5db1dbde25ce`

**First span:** `2026-09-15T16:37:43.416255Z`

**Runtime:** GPT-5.6 Terra; main agent version 33, Sentinel version 38;
notebook service version `2026.09.15`.

All **53 regression tests passed**. The fresh notebook execution then completed
all **10 runtime cells** and the model/data/telemetry assertions with **exit code
0**, without library installation or interactive authentication.

| Verified requirement | Evidence |
|---|---|
| Real model, not alias guessing | Both creation spans contain publisher `OpenAI`, model `gpt-5.6-terra`, normalized version `2026-07-09`, deployment `gpt-5.6-terra`. |
| Actual response request IDs | Both spans report `app.azure.request_id_available=true`, source header `x-request-id`, and nonempty separate API Management IDs. |
| Correct configuration fingerprints | Both hashes were independently recomputed from the exact definitions and resolved metadata in the running kernel and matched Azure telemetry. |
| Fingerprint stability | The unchanged definitions produced the same hashes in the two enhanced live runs; unit tests also verify object-key reordering is stable and instruction/tool/model-version changes alter the hash. |
| Persistence discoverable | `persist_story`, span `d3ada501c3c5b3be`, has the exact run ID, session ID, main agent/version, `persistence` interaction and saved record ID `144`. Duration: **37 ms**, type `InProc`, success true. |
| Existing behavior retained | Completed story, real Learn search/fetch, one matching interactive `SigninLogs` record, persisted records and Marp model footers all validated. |
| No synthetic backend tracing | Creation still has two notebook, two SDK and two HTTP spans, with no added service-origin creation span. |
| No table discovery | No `search_tables` call or span. |
| Final telemetry | **50 unique spans**, **7 Responses dependencies**, **11 chat spans**, **1 persistence span**, **0 failures** across **6 operations**. |

Creation diagnostics from this run:

| Field | Main creation | Sentinel creation |
|---|---|---|
| Span ID | `26ff107e10c73578` | `2e568ff69d92e2dc` |
| Duration | 841 ms | 266 ms |
| Service request ID (`x-request-id`) | `f8f691d95084102cffe33abd5d1accd6` | `eade51764f10a543601afcdc185e4ede` |
| API Management request ID | `6fe891da-e09e-4f4f-9107-b0d647899ce7` | `47b13a91-477c-4686-a873-a6e401ee9a5e` |
| Configuration SHA-256 | `632d6c281b30db5df25403e2b30750880953d137f871304c6d1d4c8f6bc0f2a8` | `97c9a2ef7cc1ee1bea61676ccd2cdd220e063efa700ad86f52f51d5285f629fa` |

**Ingestion timing matters:** the execution gate first passed with 43 rows and
`PersistenceSpans=1`. The immediate detailed check observed 49 rows; a later
read-only re-query observed the final **50 unique rows** below. All new creation
and persistence assertions passed. The gate verifies required coverage, not
that every asynchronously exported service span has arrived. The displayed total
must therefore be identified as a snapshot, not a fixed property of the code.

An earlier enhanced-run test exited 1 only because its extra metadata assertion
expected a string date instead of Kusto's datetime representation. After that
representation was verified and the test normalization corrected, the full
runtime was rerun successfully; that earlier assertion is not counted as a pass.

### Updated section totals

| Section / work | Cell ID | Notebook | SDK | HTTP | Foundry service | Total |
|---|---|---:|---:|---:|---:|---:|
| 4: create both agents | `586f0511` | 2 | 2 | 2 | 0 | **6** |
| 5: story | `2692d274` | 2 | 2 | 0 | 2 | **6** |
| 5: Learn | `2692d274` | 4 | 4 | 0 | 10 | **18** |
| 5: persistence | `2692d274` | 1 | 0 | 0 | 0 | **1** |
| 5.1: Sentinel | `ef551c01` | 4 | 5 | 0 | 10 | **19** |
| **Total** | | **13** | **13** | **2** | **22** | **50** |

Section 5 now contributes **25** correlated spans. In this pair of runs the
model/tool request pattern is the same, so the delta from 49 to 50 is precisely
the newly discoverable persistence span. This is not a guarantee that every
future run totals 50.

### Updated per-span inventory

`E` numbering distinguishes this enhanced run from the historical numbered rows.
The name tokens (`MAIN`, `SENTINEL`, `MODEL`, endpoint tokens) and origin legend
are defined in the historical guide below and have the same expansions here.
Parent references are local to this table. Code links show the corresponding
call path; creation/persistence additions are detailed above.

| # | Span ID | Span name | Origin | Parent | Duration ms | Purpose / code |
|---|---|---|---|---|---:|---|
| E01 | `26ff107e10c73578` | `create_agent MAIN` | N | Root | 841.000 | Main creation with enriched diagnostics. [C4-M](#c4-main) |
| E02 | `2403a62362ce74db` | `create_agent MAIN` | SDK | E01 | 839.000 | SDK creation operation. [C4-M](#c4-main) |
| E03 | `6e3ea856ae69596a` | `POST MAIN_VERSIONS` | HTTP | E02 | 837.000 | Main definition request. [C4-M](#c4-main) |
| E04 | `2e568ff69d92e2dc` | `create_agent SENTINEL` | N | Root | 266.000 | Sentinel creation with enriched diagnostics. [C4-S](#c4-sentinel) |
| E05 | `096e5e47dfd4d8dc` | `create_agent SENTINEL` | SDK | E04 | 266.000 | SDK specialist creation. [C4-S](#c4-sentinel) |
| E06 | `6d2a4b7daf517ab9` | `POST SENTINEL_VERSIONS` | HTTP | E05 | 263.000 | Specialist definition request. [C4-S](#c4-sentinel) |
| E07 | `f684a4efd3080d5d` | `invoke_agent MAIN` | N | Root | 11077.000 | Whole story interaction. [C5-O](#c5-orchestration) |
| E08 | `408af6160c8d1ad2` | `create_conversation` | SDK | E07 | 2901.000 | Story conversation. [C5-R](#c5-response) |
| E09 | `adc3eeefcf17a568` | `POST RESPONSES` | N | E07 | 8175.000 | Story request wrapper. [C5-R](#c5-response) |
| E10 | `538a81b3b3402f17` | `invoke_agent MAIN` | SDK | E09 | 8175.000 | Story client invocation. [C5-R](#c5-response) |
| E11 | `ecbb3ef531f26847` | `invoke_agent MAIN:33` | F | E10 | 7005.107 | Story service execution. [C5-R](#c5-response) |
| E12 | `b844a98b6cfb9914` | `chat MODEL` | F | E11 | 3088.284 | Story model work. [C5-R](#c5-response) |
| E13 | `5590b486f5f62817` | `invoke_agent MAIN` | N | Root | 13266.000 | Whole Learn interaction. [C5-O](#c5-orchestration) |
| E14 | `75bb8249e0d87053` | `create_conversation` | SDK | E13 | 235.000 | Learn conversation. [C5-R](#c5-response) |
| E15 | `639aef40c3856197` | `POST RESPONSES` | N | E13 | 1824.000 | Initial Learn request. [C5-R](#c5-response) |
| E16 | `0e1c156582ab5126` | `invoke_agent MAIN` | SDK | E15 | 1824.000 | Initial Learn client invocation. [C5-R](#c5-response) |
| E17 | `50114b5326a1e2c1` | `invoke_agent MAIN:33` | F | E16 | 1136.555 | Initial Learn service execution. [C5-R](#c5-response) |
| E18 | `f5c9518d8b014560` | `chat MODEL` | F | E17 | 148.408 | Initial approval-producing model stage. [C5-R](#c5-response) |
| E19 | `b1491b5a21809d1f` | `POST RESPONSES` | N | E13 | 3787.000 | First Learn continuation. [C5-A](#c5-approval) |
| E20 | `7655e7ea5b2bb651` | `invoke_agent MAIN` | SDK | E19 | 3787.000 | First continuation client span. [C5-A](#c5-approval) |
| E21 | `925b7c81816a38e9` | `invoke_agent MAIN:33` | F | E20 | 2881.613 | Learn search service execution. [C5-A](#c5-approval) |
| E22 | `2c9fa2f1c23265aa` | `chat MODEL` | F | E21 | 1123.218 | Model work before search. [C5-A](#c5-approval) |
| E23 | `5eb231ccfb3cc850` | `execute_tool mcp_msft-learn.microsoft_docs_search` | F | E21 | 896.131 | Learn document search. [C-TOOLS](#tool-setup) |
| E24 | `243a85c0d89784d9` | `chat MODEL` | F | E21 | 156.508 | Model stage after search. [C5-A](#c5-approval) |
| E25 | `d1ee995e450170ab` | `POST RESPONSES` | N | E13 | 7416.000 | Second Learn continuation. [C5-A](#c5-approval) |
| E26 | `8bbfc73d311c7a9b` | `invoke_agent MAIN` | SDK | E25 | 7416.000 | Second continuation client span. [C5-A](#c5-approval) |
| E27 | `8ff39ec938b60748` | `invoke_agent MAIN:33` | F | E26 | 6526.320 | Learn fetch/final answer execution. [C5-A](#c5-approval) |
| E28 | `7e2372b3014ba91b` | `chat MODEL` | F | E27 | 1168.610 | Model work before document fetch. [C5-A](#c5-approval) |
| E29 | `8bb2cf3d804d41af` | `execute_tool mcp_msft-learn.microsoft_docs_fetch` | F | E27 | 955.584 | Learn document retrieval. [C-TOOLS](#tool-setup) |
| E30 | `b9ef63c050746bad` | `chat MODEL` | F | E27 | 3571.590 | Grounded final answer model work. [C5-A](#c5-approval) |
| E31 | `d3ada501c3c5b3be` | `persist_story` | N | Root | 37.000 | Now-correlated internal record persistence; see current persistence code above. |
| E32 | `d3726068774357e8` | `sentinel-agent-query` | N | Root | 25683.000 | Whole Sentinel interaction and persistence. [C51-O](#c51-orchestration) |
| E33 | `aab8d4833c9085d5` | `AIProjectClient.get_openai_client` | SDK | E32 | 1.000 | Specialist client construction. [C51-O](#c51-orchestration) |
| E34 | `f775ac5e669a11b9` | `create_conversation` | SDK | E32 | 3731.000 | Sentinel conversation. [C51-O](#c51-orchestration) |
| E35 | `603e748b689d9d9c` | `POST RESPONSES` | N | E32 | 3276.000 | Initial Sentinel request. [C51-R](#c51-response) |
| E36 | `468b401928f82f0d` | `invoke_agent SENTINEL` | SDK | E35 | 3275.000 | Initial specialist client invocation. [C51-R](#c51-response) |
| E37 | `5cbee26fbeb79baa` | `invoke_agent SENTINEL:38` | F | E36 | 2066.822 | Initial specialist service execution. [C51-R](#c51-response) |
| E38 | `b346927e4bc41594` | `chat MODEL` | F | E37 | 0.450 | Initial approval-producing model stage. [C51-R](#c51-response) |
| E39 | `0a61bc6de1f3420c` | `POST RESPONSES` | N | E32 | 8128.000 | Workspace approval continuation. [C51-A](#c51-approval) |
| E40 | `920060b9d6e8dde4` | `invoke_agent SENTINEL` | SDK | E39 | 8127.000 | Workspace continuation client span. [C51-A](#c51-approval) |
| E41 | `819a8558932b8bf6` | `invoke_agent SENTINEL:38` | F | E40 | 6930.229 | Workspace-listing service execution. [C51-A](#c51-approval) |
| E42 | `25c6556bf6c56648` | `chat MODEL` | F | E41 | 1442.490 | Model work before workspace listing. [C51-A](#c51-approval) |
| E43 | `b20ae8df399daa15` | `execute_tool mcp_microsoft-sentinel-data.list_sentinel_workspaces` | F | E41 | 3391.537 | Resolve the authorized SDL workspace. [C-TOOLS](#tool-setup) |
| E44 | `45e9ffe55b6b1ab3` | `chat MODEL` | F | E41 | 1.037 | Model stage before query approval. [C51-A](#c51-approval) |
| E45 | `b5f7c7fda0c5031c` | `POST RESPONSES` | N | E32 | 10486.000 | SDL query approval continuation. [C51-A](#c51-approval) |
| E46 | `1f2ff60405739643` | `invoke_agent SENTINEL` | SDK | E45 | 10486.000 | Query continuation client invocation. [C51-A](#c51-approval) |
| E47 | `4df4ad113e66dec2` | `invoke_agent SENTINEL:38` | F | E46 | 8530.841 | Query/final response service execution. [C51-A](#c51-approval) |
| E48 | `bc978801c228d19f` | `chat MODEL` | F | E47 | 1384.657 | Model work before SDL query. [C51-A](#c51-approval) |
| E49 | `1dac55561e8e6753` | `execute_tool mcp_microsoft-sentinel-data.query_lake` | F | E47 | 5386.592 | Execute the explicit SigninLogs query. [C-TOOLS](#tool-setup) |
| E50 | `582a390a111f4784` | `chat MODEL` | F | E47 | 523.288 | Model work after query / final response. [C51-A](#c51-approval) |

To query this enhanced run using the [inventory query below](#section-6-reproduce-the-inventory),
change `run_id` to `2855bde8-64be-45fd-b58f-5db1dbde25ce`, `start` to
`datetime(2026-09-15T16:37:00Z)` and `stop` to
`datetime(2026-09-15T16:45:00Z)`. The run-selection logic is unchanged; it now
selects the persistence operation too. To inspect the added fields, project the
attributes in the creation table above and normalize the date-shaped model
version as shown earlier.

<a id="historical-49-spans"></a>
## Historical baseline: the original 49-span run

### Historical purpose and scope

This guide itemizes the **49 unique telemetry spans** observed for the Windows
[Foundry agent notebook](zolab-ai-agent-demo-win11.ipynb) run
`69fb2e74-eabf-4142-b4e2-352e3e7d4844`. It connects each span to a notebook section,
cell, purpose, parent, emitting component and relevant code.

These are **historical measured records**, not a predicted list of what instrumentation
might emit. A read-only re-query of `AppDependencies` confirmed **49 rows, 49
unique `(OperationId, Id)` pairs and zero failed spans**. No model inference was
rerun to reconstruct this historical inventory. The enhanced implementation is
validated separately above.

| Run property | Observed value |
|---|---|
| Execution date | September 15, 2026 |
| First counted span start | `2026-09-15T15:00:22.474980Z` |
| Run correlation attribute | `demo.run_id=69fb2e74-eabf-4142-b4e2-352e3e7d4844` |
| Model deployment | `gpt-5.6-terra` |
| Service-reported model | `gpt-5.6-terra-2026-07-09` |
| Main agent version | `ZoDEfendersAgent-1702:33` |
| Sentinel agent version | `ZoDEfendersAgent-1702-sentinel:38` |
| Notebook service version | `2026.09.15` |
| Client role | `foundry-agent-demo.foundry-agent-framework-demo` |
| Foundry service role | `responsesapi` |
| Client SDK stamp | `uwm_py3.14.7:otel1.44.0:dst1.8.10` |
| Service SDK stamp | `aifoundry-v1.0` |
| Data source | `AppDependencies` in the Application Insights-linked Log Analytics workspace |
| Query workspace for this run | `DIBSecCom`, customer ID `7e9298ab-22e6-4a82-a53e-c5ed7faee977` |
| Model interactions | Story, Microsoft Learn grounding, Sentinel `SigninLogs` |
| Validation | 10 runtime cells executed; installation/setup cells skipped |

**The historical 49 is a run-filtered dependency inventory, not every telemetry
item produced by that notebook execution.** In that baseline, the separately
emitted `persist_story` span was outside the filter. This is fixed in the
enhanced run above. See [Historical excluded work](#excluded-work).
The model version, agent version and notebook service version above describe
different things and must not be interchanged.

Cell IDs below are the notebook's persistent JSON `id` values, not execution
counts or VS Code's temporary selection IDs. Code line numbers are **within the
executed cell's source**, not line numbers in the notebook's JSON serialization.
The examples describe this run; future edits, tool choices and approval rounds
can change the counts.

## Count reconciliation by section and origin

| Section / work | Cell ID | Explicit notebook spans | Automatic SDK spans | Automatic HTTP spans | Foundry service spans | Total |
|---|---|---:|---:|---:|---:|---:|
| 4: create main and Sentinel agents | `586f0511` | 2 | 2 | 2 | 0 | **6** |
| 5: story interaction | `2692d274` | 2 | 2 | 0 | 2 | **6** |
| 5: Microsoft Learn interaction | `2692d274` | 4 | 4 | 0 | 10 | **18** |
| 5.1: Sentinel interaction | `ef551c01` | 4 | 5 | 0 | 10 | **19** |
| **Total** | **3 emitting cells in this inventory** | **12** | **13** | **2** | **22** | **49** |

Thus Section 5 contributes **24** spans in total: six for the story and eighteen
for Learn. Sections 3.1, 3.2 and 3.3 configure the instrumentation/tools; they do
not contribute rows to these five selected operations. Section 6 retrieves and
checks the telemetry rather than producing the 49 rows itself.

Another useful breakdown:

| Measured category | Count | Meaning |
|---|---:|---|
| Operations / traces | 5 | Main creation, Sentinel creation, story, facts, Sentinel |
| Responses API dependency wrappers | 7 | One story request, three Learn requests, three Sentinel requests |
| Service-side `invoke_agent` spans | 7 | One service execution for each Responses request |
| Service-side `chat` spans | 11 | One story, five Learn, five Sentinel |
| Service-side MCP execution spans | 4 | Learn search, Learn fetch, workspace listing, SDL query |
| `search_tables` spans | 0 | The Sentinel table was explicitly supplied |
| Failed spans | 0 | All 49 have `Success=true` |

The notebook's `GenAiSpans` coverage field is specifically
`countif(Name startswith "chat ")`: **11**, not a count of every span with GenAI
attributes. Agent and tool spans also carry GenAI context.

### Origin legend and important naming differences

- **N**: an explicit notebook `tracer.start_as_current_span(...)`.
- **SDK**: automatic Azure AI Projects / Responses instrumentation or an Azure
  SDK tracing decorator, running in the notebook process.
- **HTTP**: automatic HTTP request tracing on the Azure SDK agent-creation path.
- **F**: a span emitted by the Foundry service (`AppRoleName=responsesapi`).
  The notebook triggers these through `responses.create`; it does not directly
  create the service's `chat` or `execute_tool` spans.

The seven HTTP-looking **Responses** rows are **N**, not seven additional
HTTPX-created spans. The notebook explicitly creates them and sets HTTP
attributes. The Azure Monitor exporter builds the displayed name from the HTTP
method and URL path. This is why the source's `POST /openai/v1/responses` appears
as the longer project-prefixed path in Log Analytics.

Repeated names are not duplicate rows: for example, an explicit `create_agent`
parent contains an SDK `create_agent` child with a different span ID.
Similarly, an unversioned SDK `invoke_agent` contains the versioned service
`invoke_agent`. Parent IDs, role names and source code distinguish them.

### Exact-name shorthand

To keep the inventory readable, tokens in backticks below expand as follows.
They are documentation shorthand, **not literal telemetry names**.

| Token | Exact replacement |
|---|---|
| `MAIN` | `ZoDEfendersAgent-1702` |
| `SENTINEL` | `ZoDEfendersAgent-1702-sentinel` |
| `MODEL` | `gpt-5.6-terra-2026-07-09` |
| `RESPONSES` | `/api/projects/zolabai-fndry-proj-u49gco/openai/v1/responses` |
| `MAIN_VERSIONS` | `/api/projects/zolabai-fndry-proj-u49gco/agents/ZoDEfendersAgent-1702/versions` |
| `SENTINEL_VERSIONS` | `/api/projects/zolabai-fndry-proj-u49gco/agents/ZoDEfendersAgent-1702-sentinel/versions` |

Every table row is one unique span. `Parent` refers to another inventory row
number. `Root` means no parent span is present in this inventory; for these roots,
the exported `ParentId` equals the operation ID rather than a listed span ID.
Durations are measured milliseconds, rounded to three decimal places.

## Section 4: create the agents -- 6 spans

**Cell:** `586f0511` -- **Create the AI Agents**

**Purpose:** create or resolve the main and Sentinel agent versions, including
their instructions, selected model and attached tools. Creating an agent version
is configuration work; it is not model inference.

Main creation operation: `fe7a15a60a2a51302f32d2d90b31970b`.
Sentinel creation operation: `ef9be3c0b50580d6150c5ce5c16d255b`.

| # | Span ID | Span name (tokens expanded above) | Origin | Parent | Duration ms | Purpose and code |
|---:|---|---|---|---:|---:|---|
| 1 | `d610aa054bbf9677` | `create_agent MAIN` | N | Root | 666.000 | Main-agent setup boundary; adds run ID, model and agent attributes. [C4-M](#c4-main) |
| 2 | `6d137ca1f1df54c5` | `create_agent MAIN` | SDK | 1 | 663.000 | Instruments `agents.create_version` and records the returned agent identity/version. [C4-M](#c4-main) |
| 3 | `238fd493af0f9bfe` | `POST MAIN_VERSIONS` | HTTP | 2 | 661.000 | HTTP request to the main agent's versions endpoint. [C4-M](#c4-main) |
| 4 | `926354e80b909d25` | `create_agent SENTINEL` | N | Root | 257.000 | Sentinel-agent setup boundary; attaches run/model/tool context. [C4-S](#c4-sentinel) |
| 5 | `512d3629711d6eba` | `create_agent SENTINEL` | SDK | 4 | 256.000 | Instruments creation/resolution of the Sentinel specialist version. [C4-S](#c4-sentinel) |
| 6 | `4b124d1bea4d1954` | `POST SENTINEL_VERSIONS` | HTTP | 5 | 255.000 | HTTP request carrying the specialist's model, instructions and MCP connection. [C4-S](#c4-sentinel) |

## Section 5: story -- 6 spans

**Cell:** `2692d274` -- **Query the Agent, Pass 1**

**Purpose:** run the main agent's fictional-story prompt in a new conversation.
This pass required one Responses request and no MCP execution or approval
continuation. The presence of `mcp_list_tools` in a response output is not an
executed `search_tables` call.

Operation: `b9481c0848180a248bf87582e99e351a`.

| # | Span ID | Span name | Origin | Parent | Duration ms | Purpose and code |
|---:|---|---|---|---:|---:|---|
| 7 | `dd06baca38e3d1f0` | `invoke_agent MAIN` | N | Root | 8996.000 | Whole story interaction; carries `app.interaction=story` and the run ID. [C5-O](#c5-orchestration) |
| 8 | `a0efddf61aa5032a` | `create_conversation` | SDK | 7 | 2905.000 | Creates the story conversation before inference. [C5-R](#c5-response) |
| 9 | `a9d5e2e87698e618` | `POST RESPONSES` | N | 7 | 6089.000 | Explicit dependency wrapper around the story Responses request. [C5-R](#c5-response) |
| 10 | `f744933ae3649054` | `invoke_agent MAIN` | SDK | 9 | 6089.000 | Client-side instrumentation of the agent-reference Responses call. [C5-R](#c5-response) |
| 11 | `d5a160ee9eab412a` | `invoke_agent MAIN:33` | F | 10 | 4489.486 | Foundry's execution of main-agent version 33 for the story request. [C5-R](#c5-response) |
| 12 | `9c131b950b4b5625` | `chat MODEL` | F | 11 | 3148.932 | Service-reported model work for the story response. [C5-R](#c5-response) |

## Section 5: Microsoft Learn -- 18 spans

**Cell:** `2692d274` -- **Query the Agent, Pass 2**

**Purpose:** ground the answer in Microsoft Learn using the public Learn MCP
tool. The recorded flow was:

1. Initial Responses request returns a tool-approval request.
2. Approval continuation executes `microsoft_docs_search`, then requests another approval.
3. Second continuation executes `microsoft_docs_fetch` and returns the answer.

These are **three successful requests**, not retries after failures.
Operation: `14a94aadc161d367a734afd350023571`.

| # | Span ID | Span name | Origin | Parent | Duration ms | Purpose and code |
|---:|---|---|---|---:|---:|---|
| 13 | `b648afe7ca543a97` | `invoke_agent MAIN` | N | Root | 15780.000 | Whole facts interaction, including conversation, requests and approvals; `app.interaction=facts`. [C5-O](#c5-orchestration) |
| 14 | `434b48ec9e732b7d` | `create_conversation` | SDK | 13 | 259.000 | Creates a conversation separate from the fictional story. [C5-R](#c5-response) |
| 15 | `e7156990ab5547ed` | `POST RESPONSES` | N | 13 | 1929.000 | Initial Learn-grounded request. [C5-R](#c5-response) |
| 16 | `5eb5b8d2c407d472` | `invoke_agent MAIN` | SDK | 15 | 1929.000 | Client SDK wrapper for the initial facts request. [C5-R](#c5-response) |
| 17 | `729e2613f8818604` | `invoke_agent MAIN:33` | F | 16 | 1239.931 | Service execution that returns the first MCP approval request. [C5-R](#c5-response) |
| 18 | `b6b6f9e267226e79` | `chat MODEL` | F | 17 | 0.373 | Service chat span in the initial approval-producing execution; not proof of a full inference in 0.373 ms. [C5-R](#c5-response) |
| 19 | `151a65372ac7faa6` | `POST RESPONSES` | N | 13 | 6323.000 | First approval continuation sent to Responses. [C5-A](#c5-approval) |
| 20 | `9aa00997dcb2a812` | `invoke_agent MAIN` | SDK | 19 | 6323.000 | Client SDK wrapper for the first continuation. [C5-A](#c5-approval) |
| 21 | `e97ecfadc8285d8c` | `invoke_agent MAIN:33` | F | 20 | 5447.157 | Service execution covering the approved Learn search. [C5-A](#c5-approval) |
| 22 | `5f01131bcca3f809` | `chat MODEL` | F | 21 | 1088.504 | Model work preceding the Learn search within this execution. [C5-A](#c5-approval) |
| 23 | `fccd65ba05076210` | `execute_tool mcp_msft-learn.microsoft_docs_search` | F | 21 | 3119.550 | Searches Microsoft Learn for grounding material. [C-TOOLS](#tool-setup) and [C5-A](#c5-approval) |
| 24 | `045dce05846c5fbe` | `chat MODEL` | F | 21 | 6.764 | Post-search model-stage span before the next approval response. [C5-A](#c5-approval) |
| 25 | `23d0d976ac14e5d5` | `POST RESPONSES` | N | 13 | 7265.000 | Second approval continuation. [C5-A](#c5-approval) |
| 26 | `3979d5af7802e9aa` | `invoke_agent MAIN` | SDK | 25 | 7265.000 | Client SDK wrapper for the second continuation. [C5-A](#c5-approval) |
| 27 | `6d0f0a1aa8446ecc` | `invoke_agent MAIN:33` | F | 26 | 6378.525 | Service execution covering document retrieval and the final grounded answer. [C5-A](#c5-approval) |
| 28 | `ad0d09730ff5c2e6` | `chat MODEL` | F | 27 | 1092.009 | Model work preceding the approved document fetch. [C5-A](#c5-approval) |
| 29 | `e7ca695597759abb` | `execute_tool mcp_msft-learn.microsoft_docs_fetch` | F | 27 | 905.373 | Retrieves the selected Learn document. [C-TOOLS](#tool-setup) and [C5-A](#c5-approval) |
| 30 | `7a4ddac3de32ff73` | `chat MODEL` | F | 27 | 3621.822 | Model work after the fetch, in the execution that returns the final answer. [C5-A](#c5-approval) |

## Section 5.1: Sentinel -- 19 spans

**Cell:** `ef551c01` -- **Query Microsoft Sentinel Data Exploration MCP**

**Purpose:** retrieve the latest interactive sign-in for the requested identity
from the explicitly named `SigninLogs` table through the existing project-connected
Sentinel MCP tool. The run returned one matching record.

The observed request sequence is initial request, workspace-listing approval
continuation, then query approval continuation. `search_tables` is never called.
Operation: `af9375e9d951cc797c9e677a48a9cc9a`.

| # | Span ID | Span name | Origin | Parent | Duration ms | Purpose and code |
|---:|---|---|---|---:|---:|---|
| 31 | `c09039f1e4c51a02` | `sentinel-agent-query` | N | Root | 25526.000 | Entire Sentinel flow, including client/conversation setup, approvals, result handling and persistence. [C51-O](#c51-orchestration) |
| 32 | `9e4764cfb66746d8` | `AIProjectClient.get_openai_client` | SDK | 31 | 2.000 | Constructs the project-backed OpenAI client inside the Sentinel parent context. [C51-O](#c51-orchestration) |
| 33 | `c1724b3eb1576cef` | `create_conversation` | SDK | 31 | 3774.000 | Creates the specialist's new conversation. [C51-O](#c51-orchestration) |
| 34 | `476b1c85777cc79d` | `POST RESPONSES` | N | 31 | 5269.000 | Initial Sentinel request. [C51-R](#c51-response) |
| 35 | `c094d36f611eca22` | `invoke_agent SENTINEL` | SDK | 34 | 5269.000 | Client SDK wrapper for the initial specialist request. [C51-R](#c51-response) |
| 36 | `63eb87458d440fca` | `invoke_agent SENTINEL:38` | F | 35 | 1400.451 | Service execution returning the workspace-tool approval request. [C51-R](#c51-response) |
| 37 | `7eadfa415329df0c` | `chat MODEL` | F | 36 | 0.504 | Model-stage span in the initial approval-producing execution. [C51-R](#c51-response) |
| 38 | `1298c73260866a6a` | `POST RESPONSES` | N | 31 | 7613.000 | First approval continuation. [C51-A](#c51-approval) |
| 39 | `8d00583489d58ba0` | `invoke_agent SENTINEL` | SDK | 38 | 7613.000 | Client SDK wrapper for the workspace-listing continuation. [C51-A](#c51-approval) |
| 40 | `a0d42034b21a9ac6` | `invoke_agent SENTINEL:38` | F | 39 | 6720.588 | Service execution containing the workspace tool call. [C51-A](#c51-approval) |
| 41 | `495102eb675d8bcd` | `chat MODEL` | F | 40 | 1179.039 | Model work preceding workspace resolution. [C51-A](#c51-approval) |
| 42 | `7795bcd28e0cb171` | `execute_tool mcp_microsoft-sentinel-data.list_sentinel_workspaces` | F | 40 | 3329.204 | Resolves an available authorized SDL workspace ID; not table discovery. [C-TOOLS](#tool-setup) |
| 43 | `e437cc3c7ad572a5` | `chat MODEL` | F | 40 | 0.388 | Model-stage span after listing, before the query approval is returned. [C51-A](#c51-approval) |
| 44 | `ad6c57cbffc8c9fc` | `POST RESPONSES` | N | 31 | 8807.000 | Second approval continuation, authorizing the SDL query. [C51-A](#c51-approval) |
| 45 | `f2a38ad7812fd3c7` | `invoke_agent SENTINEL` | SDK | 44 | 8806.000 | Client SDK wrapper for the final Sentinel request. [C51-A](#c51-approval) |
| 46 | `6e00a739d1aa93c9` | `invoke_agent SENTINEL:38` | F | 45 | 7693.414 | Service execution containing the query and final result formatting. [C51-A](#c51-approval) |
| 47 | `a3bc3a749ea2c4f9` | `chat MODEL` | F | 46 | 1937.187 | Model work preceding the approved `SigninLogs` query. [C51-A](#c51-approval) |
| 48 | `6d2666c2675f494a` | `execute_tool mcp_microsoft-sentinel-data.query_lake` | F | 46 | 4013.793 | Executes the supplied-table KQL against SDL and returns raw result data. [C-TOOLS](#tool-setup) |
| 49 | `b6cafcd901270475` | `chat MODEL` | F | 46 | 459.585 | Model work following the query, in the execution returning the final answer. [C51-A](#c51-approval) |

## Code-to-span reference

The following are **excerpts from the executed notebook cells**, not standalone
programs. They reuse notebook globals. Some intervening attribute assignments,
logging and exception-handling lines are omitted for readability; the notebook
contains the complete implementations. For service spans, the snippet is the
**triggering client code**, not a claim that the service implementation lives in
the notebook.

### C0: enable and correlate tracing

**Section 3.1, cell `3c78effc`, source lines 84-110.**
This configures later spans; it is not itself one of the 49 listed operations.

```python
os.environ["OTEL_LOGS_EXPORTER"] = "none"
os.environ["OTEL_METRICS_EXPORTER"] = "none"
os.environ["OTEL_TRACES_SAMPLER"] = "microsoft.fixed_percentage"
os.environ["OTEL_TRACES_SAMPLER_ARG"] = "1.0"

configure_azure_monitor(
    connection_string=connection_string,
    resource=Resource.create(attributes),
    sampling_ratio=1.0,
    enable_live_metrics=False,
    enable_performance_counters=False,
)
settings.tracing_implementation = "opentelemetry"
instrumentor = AIProjectInstrumentor()
instrumentor.instrument(
    enable_content_recording=content_enabled,
    enable_trace_context_propagation=True,
    enable_baggage_propagation=True,
)
```

The complete setup temporarily disables Azure-core tracing during backend
initialization, verifies that instrumentation applied, and prevents incompatible
reinitialization. Azure Monitor owns HTTPX/HTTPX2 auto-instrumentation; the notebook
does not install a second HTTPX2 instrumentor.

**Source lines 125-136** supply the tracer and propagation helper:

```python
tracer = trace.get_tracer("foundry_agent_framework_notebook")

def make_baggage_context(values: dict[str, object]):
    context = otel_context.get_current()
    for key, value in values.items():
        if value is None:
            continue
        value_text = str(value).strip()
        if value_text:
            context = baggage.set_baggage(key, value_text, context=context)
    return context
```

Baggage is propagated context, **not automatically a span attribute**.
Five explicit root spans separately set `demo.run_id`. The query finds those
operations and includes their children, even when the children lack that attribute.

<a id="c4-main"></a>
### C4-M: main agent creation

**Section 4, cell `586f0511`, source lines 107-138.** Produces rows **1-3**.

```python
with tracer.start_as_current_span(
    f"create_agent {main_agent_name}",
    kind=SpanKind.CLIENT,
    context=main_agent_creation_context,
) as span:
    span.set_attribute("demo.run_id", demo_run_id)
    main_agent = project_client.agents.create_version(
        agent_name=main_agent_name,
        definition=PromptAgentDefinition(
            model=model_name,
            instructions=main_agent_instructions,
            tools=[mcp_tool_spec],
        ),
    )
    span.set_attribute("gen_ai.agent.id", main_agent.id)
    span.set_attribute("gen_ai.agent.version", str(main_agent.version))
```

The outer context manager is row 1; SDK instrumentation supplies row 2; the
underlying HTTP request supplies row 3. The returned version can be an existing
identical version rather than a newly incremented version.

<a id="c4-sentinel"></a>
### C4-S: Sentinel agent creation

**Section 4, cell `586f0511`, source lines 169-215.** Produces rows **4-6**.

```python
if sentinel_tool is not None:
    sentinel_agent_name = f"{main_agent_name}-sentinel"
    with tracer.start_as_current_span(
        f"create_agent {sentinel_agent_name}",
        kind=SpanKind.CLIENT,
        context=sentinel_creation_context,
    ) as span:
        span.set_attribute("demo.run_id", demo_run_id)
        sentinel_project_agent = project_client.agents.create_version(
            agent_name=sentinel_agent_name,
            definition=PromptAgentDefinition(
                model=model_name,
                instructions=sentinel_agent_instructions,
                tools=[sentinel_tool],
            ),
        )
```

The existing `sentinel_tool` includes the Foundry project connection. This is
not the public Microsoft Learn tool, and it does not call Microsoft Graph AH.

<a id="c5-orchestration"></a>
### C5-O: main interaction boundary

**Section 5, cell `2692d274`, source lines 215-267.** The same helper produces
row **7** for `story` and row **13** for `facts`.

```python
with tracer.start_as_current_span(
    f"invoke_agent {main_agent_display_name}",
    kind=SpanKind.CLIENT,
    context=interaction_context,
) as interaction_span:
    if run_id:
        interaction_span.set_attribute("demo.run_id", run_id)
    interaction_span.set_attribute("app.interaction", interaction_name)
```

Later in this block, `run_query_with_auto_approval(...)` runs the request loop
and records the response/conversation metadata on this interaction span.
The two call sites supply `interaction_name="story"` / `"facts"` and the
corresponding prompt. This is why the same name belongs to two different traces.

<a id="c5-response"></a>
### C5-R: conversation and Responses request

**Section 5, cell `2692d274`, source lines 94-150.**
The conversation call creates rows **8** and **14**:

```python
conversation = openai_client.conversations.create()
interaction_span.add_event(
    "conversation.created",
    {
        "app.conversation.id": conversation.id,
        "app.interaction": interaction_name,
    },
)
```

The `conversation.created` event is attached to an existing span; it is **not
another span**. Each call to the following helper creates the explicit HTTP
wrapper plus a nested SDK agent span and service-side descendants:

```python
with tracer.start_as_current_span(
    "POST /openai/v1/responses", kind=SpanKind.CLIENT
) as response_http_span:
    response_http_span.set_attribute("http.request.method", "POST")
    response_http_span.set_attribute("url.full", responses_url)
    response_http_span.set_attribute("gen_ai.operation.name", "responses.create")
    response = openai_client.responses.create(
        conversation=conversation.id,
        input=response_input,
        extra_body=agent_reference_payload,
    )
    response_http_span.set_attribute("http.response.status_code", 200)
```

The main helper attaches baggage before the wrapper and detaches it afterward.
Initial calls pass the prompt. Subsequent calls pass MCP approval responses.
The SDK uses an `invoke_agent` span name because the request references an agent;
the service supplies the separately observed `chat` spans.

<a id="c5-approval"></a>
### C5-A: continue after MCP approval

**Section 5, cell `2692d274`, source lines 184-204.** Rows **19-30** are the two
continuations through the same response helper, not independent notebook cells.

```python
interaction_span.add_event(
    "mcp.approval.auto_approved",
    {
        "app.approval.round": round_number,
        "app.approval.count": len(approval_ids),
    },
)
response = create_agent_response(
    [
        {
            "type": "mcp_approval_response",
            "approve": True,
            "approval_request_id": request_id,
        }
        for request_id in approval_ids
    ]
)
```

Approvals are bounded by the helper's limit. The approval event itself does not
increase the span count; the resulting Responses call and service work do.

<a id="c51-orchestration"></a>
### C51-O: Sentinel orchestration, client and conversation

**Section 5.1, cell `ef551c01`, source lines 514-536.** Produces rows **31-33**.

```python
with tracer.start_as_current_span(
    "sentinel-agent-query", kind=SpanKind.CLIENT, context=context
) as span:
    span.set_attribute("demo.run_id", run_id)
    span.set_attribute("app.interaction", "sentinel")
    with project_client.get_openai_client() as openai_client:
        conversation = openai_client.conversations.create()
```

Unlike the main cell, this client acquisition occurs **inside** a run-tagged
parent. The SDK's `get_openai_client` tracing decorator therefore contributes
row 32 to this inventory. It measures client construction, not an LLM call.
The outer span stays open through error checks, model metadata capture and
writing the Sentinel record/deck.

<a id="c51-response"></a>
### C51-R: Sentinel Responses dependency

**Section 5.1, cell `ef551c01`, source lines 538-564.**
Creates rows **34**, **38** and **44**, with SDK/service children.

```python
with tracer.start_as_current_span(
    "POST /openai/v1/responses", kind=SpanKind.CLIENT
) as response_http_span:
    response_http_span.set_attribute("http.request.method", "POST")
    response_http_span.set_attribute("url.full", responses_url)
    response_http_span.set_attribute("gen_ai.operation.name", "responses.create")
    response = openai_client.responses.create(
        conversation=conversation.id,
        input=response_input,
        extra_body=sentinel_agent_reference_payload,
    )
    response_http_span.set_attribute("http.response.status_code", 200)
```

It retains the currently active Sentinel parent context. It does not reattach a
context captured before the parent existed; doing so would separate the request
children into a different trace.

<a id="c51-approval"></a>
### C51-A: Sentinel approval continuation

**Section 5.1, cell `ef551c01`, source lines 597-610.**
The first continuation authorizes workspace listing; the second authorizes KQL.

```python
if round_number > 5:
    raise RuntimeError("Sentinel exceeded 5 MCP approval rounds.")
sentinel_approval_rounds += len(approval_ids)
sentinel_response = create_agent_response(
    [
        {
            "type": "mcp_approval_response",
            "approve": True,
            "approval_request_id": approval_id,
        }
        for approval_id in approval_ids
    ]
)
```

The `chat` / `execute_tool` ordering in the inventory is established by start
times and service-parent IDs. The notebook output confirms two approval rounds.
Very short `chat` spans are preserved as reported; their durations alone do not
establish cache hits, complete inference time, or token throughput.

<a id="tool-setup"></a>
### C-TOOLS: why these four tools appear

**Section 3.2, cell `9789978a`** configures the public Microsoft Learn MCP tool:

```python
msft_learn_mcp_url = "https://learn.microsoft.com/api/mcp"
mcp_tool_spec = MCPTool(
    server_label="msft-learn",
    server_url=msft_learn_mcp_url,
)
```

The main agent receives `tools=[mcp_tool_spec]` in Section 4. Its facts prompt
requests Microsoft Learn grounding. The service selected `microsoft_docs_search`
and `microsoft_docs_fetch`, observed as rows **23** and **29**. Configuring the
tool does not guarantee that both calls occur in every future run.

**Section 3.3, cell `377478c3`** supplies the Sentinel tool on the existing
Foundry project connection:

```python
sentinel_mcp_tool_spec = MCPTool(
    server_label="microsoft-sentinel-data",
    server_url=sentinel_mcp_url,
    require_approval="always",
    project_connection_id=sentinel_project_connection_id,
)
```

**Section 4, cell `586f0511`, source lines 57-88** explicitly instructs the
specialist to resolve the workspace, then query `SigninLogs` without discovery.
The supplied KQL template is:

```kusto
SigninLogs
| where IsInteractive == true
| where UserPrincipalName =~ '<signed-in UPN>'
| top 1 by TimeGenerated desc
| project TimeGenerated, UserPrincipalName, IsInteractive, IPAddress,
          Location, LocationDetails, AppDisplayName, ResourceDisplayName
```

The placeholder is replaced with the requested identity when the agent invokes
the tool; the actual identity and returned sign-in values are intentionally
omitted here. Rows **42** and **48** demonstrate workspace resolution and query
execution. There is no `search_tables` execution to itemize.

## Reading the trace hierarchy

This representative main-agent request tree explains why one Responses request
can account for several rows:

```text
N: invoke_agent MAIN                         whole story/facts interaction
  SDK: create_conversation
  N: POST RESPONSES                          explicit HTTP dependency wrapper
    SDK: invoke_agent MAIN                  instrumented client call
      F: invoke_agent MAIN:33               Foundry execution
        F: chat MODEL
        F: execute_tool ...                 only when a tool is executed
        F: chat MODEL                       possible continuation after tool
  N: POST RESPONSES                          another approval continuation
    ...
```

For Sentinel, `sentinel-agent-query` replaces the main interaction root, an SDK
client-construction span is also present, and the versioned service agent is
`SENTINEL:38`.

**Do not sum parent and child durations to calculate notebook elapsed time.**
They measure overlapping intervals. For example, story rows 9 and 10 both report
6,089 ms because one wraps the other. Useful interaction-level measurements are:

| Boundary | Duration |
|---|---:|
| Main agent creation, row 1 | 666 ms |
| Sentinel agent creation, row 4 | 257 ms |
| Story interaction, row 7 | 8,996 ms |
| Learn interaction, row 13 | 15,780 ms |
| Sentinel interaction, row 31 | 25,526 ms |

The Sentinel query tool alone took **4,013.793 ms**, while workspace listing took
**3,329.204 ms**. Those are not the full 25,526 ms orchestration time.
They also differ from the earlier 4,028.57 / 2,933.00 ms measurements documented
for a different run in [observability.md](observability.md).
Telemetry ingestion waits in Section 6 are not added to these span durations.

<a id="excluded-work"></a>
## Historical excluded work: what was not in the 49

### `persist_story` existed outside the baseline run filter

**Section 5, cell `2692d274`, source lines 503-543.**
The notebook constructs baggage and then starts a separate persistence span
after both main interaction roots have ended:

```python
persist_context = make_baggage_context(
    {
        "demo-run-id": run_id,
        "agent-name": main_agent_display_name,
        "model-name": model_name,
    }
)

with tracer.start_as_current_span("persist_story", context=persist_context) as persist_span:
    story_id = append_story(stories_file, story_record)
    persist_span.set_attribute("app.story.id", story_id)
    persist_span.set_attribute("app.stories.path", str(stories_file))
```

The excerpt omits the other baggage fields and record construction. Crucially,
the historical span did **not** call
`persist_span.set_attribute("demo.run_id", run_id)`. The `demo_run_id` field in
the saved story dictionary is also not a span attribute.

A separate read-only lookup confirmed:

| Property | Observed value |
|---|---|
| Name | `persist_story` |
| Span ID | `c447cdebad4ad003` |
| Operation ID | `f1416fe1f60834bb13445f0fc16df5b0` |
| Start | `2026-09-15T15:00:48.341640Z` |
| Duration | 35 ms |
| Dependency type | `InProc` |
| Success | true |
| `demo.run_id` span attribute | empty |
| `app.story.id` | `140`, matching the main record from this run |

That operation was not one of the five operations selected by the baseline query.
Consequently, it is **not row 50 in the historical inventory**: it is additional, excluded
telemetry. The advertised 49 should not be interpreted as the total number of
spans emitted across all notebook work. The current code now supplies the explicit
attributes; the enhanced inventory documents the corrected, included span E31.

### Other boundaries and exclusions

| Section / cell | Relationship to this inventory |
|---|---|
| Environment/kernel/library setup | Skipped for this execution; no installation activity is attributed to the 49. |
| Deployment confirmation / imports / credentials | Run before the counted interaction roots; no rows assigned to them in this inventory. |
| 3.1 / `3c78effc` | Establishes providers, export controls and propagation. Configuration flags and resource attributes are not individual spans. |
| 3.2 / `9789978a` | Constructs the Learn tool definition. Remote Learn execution is attributed to the Section 5 calling cell. |
| 3.3 / `377478c3` | Resolves project/workspace configuration and constructs the Sentinel tool. Remote Sentinel execution is attributed to Section 5.1. |
| 4 / `586f0511`: `deployments.get` | Resolves model metadata before the explicit creation roots. Not a row in these selected operations. |
| 5: `get_openai_client` | Occurs before the main run-tagged interaction roots, unlike the counted Sentinel call in row 32. |
| 5: writing the main Marp deck | Happens after `persist_story`; there is no dedicated Marp span in the 49. |
| 5.1: writing the Sentinel record/deck | Happens inside row 31's orchestration scope; there is no separate persistence/Marp span among the 49. |
| 6 / `6e3dcab6` | Flushes tracing and reads Log Analytics using CLI/HTTPS helpers. These validation queries are not additional rows in the five selected traces. |
| Post-run test assertions / HTML export / browser preview | Validation/presentation work after the runtime cells, not part of the 49. |

Span events such as `create_agent.start`, `create_agent.success`,
`conversation.created`, `response.completed` and `mcp.approval.auto_approved`
are **not extra dependency spans**. Neither are attributes, response-output items,
log records, metric points or content-capture payloads.

## Section 6: reproduce the inventory

**Cell:** `6e3dcab6` -- **Validate Observability (Traces) in Log Analytics**

The run uses `demo.run_id` to find operation IDs, then expands to all dependency
rows in those operations. Filtering every row directly by `demo.run_id` would
return only the **five explicitly tagged roots**, omitting 44 children.

Use a fixed event-time window when revisiting this run rather than a rolling
`ago(6h)` window that eventually expires:

```kusto
let run_id = "69fb2e74-eabf-4142-b4e2-352e3e7d4844";
let start = datetime(2026-09-15T14:59:00Z);
let stop = datetime(2026-09-15T15:10:00Z);
let run_operations = AppDependencies
| where TimeGenerated between (start .. stop)
| where tostring(Properties["demo.run_id"]) == run_id
| distinct OperationId;
AppDependencies
| where TimeGenerated between (start .. stop)
| where OperationId in (run_operations)
| project TimeGenerated, Id, ParentId, OperationId, Name, DurationMs,
          Success, DependencyType, AppRoleName, SDKVersion, AppVersion,
          Interaction=tostring(Properties["app.interaction"]),
          RequestModel=tostring(Properties["gen_ai.request.model"]),
          ResponseModel=tostring(Properties["gen_ai.response.model"])
| order by TimeGenerated asc, Id asc
```

This ordering is the numbering used by the 49 inventory rows. The query avoids
the prompt/completion/tool-output properties and returns span metadata only.
Normal workspace retention and access requirements still apply.

To reproduce the cell/operation totals, replace the final projection/order with:

```kusto
| summarize Spans=count(), Failures=countif(Success == false) by OperationId
```

Expected operation counts:

| Operation ID | Cell / purpose | Count |
|---|---|---:|
| `fe7a15a60a2a51302f32d2d90b31970b` | `586f0511` / main creation | 3 |
| `ef9be3c0b50580d6150c5ce5c16d255b` | `586f0511` / Sentinel creation | 3 |
| `b9481c0848180a248bf87582e99e351a` | `2692d274` / story | 6 |
| `14a94aadc161d367a734afd350023571` | `2692d274` / Learn | 18 |
| `af9375e9d951cc797c9e677a48a9cc9a` | `ef551c01` / Sentinel | 19 |
| **Total** | | **49** |

The notebook's coverage query also joins interaction labels by `OperationId`.
For this run, each tagged interaction operation has one distinct interaction
label, so that join does not multiply the rows. The raw unique-span re-query
above independently confirmed the total.

The execution gate flushes the exporter, checks for failed spans, and waits for
story/facts/Sentinel coverage:

```python
if not trace.get_tracer_provider().force_flush(timeout_millis=30000):
    raise RuntimeError("OpenTelemetry export did not flush within 30 seconds.")

coverage = log_query_rows(query_log_analytics(workspace_customer_id, kql_coverage))[0]
if coverage["Failures"]:
    raise RuntimeError(f"Current-run telemetry contains {coverage['Failures']} failed spans.")
```

The full cell polls at 15-second intervals for up to 12 waits, checks Responses
dependency coverage and verifies the notebook's service role/version. It is a
validation gate, not an extra LLM request.

## Evidence, interpretation and references

- **Notebook evidence:** the executed copy from this run, including its actual
  cell source, response statuses, two Learn approval rounds and two Sentinel
  approval rounds.
- **Telemetry evidence:** a sanitized read-only query returning 49 unique span
  identities, durations, parent/operation IDs, SDK stamps and roles. Cell mapping
  is derived from those roots and call sites; a `cell_id` property is not
  automatically present on each telemetry row.
- **Implementation checks:** installed Azure AI Projects instrumentation wraps
  `AgentsOperations.create_version`, `conversations.create` and
  `responses.create`; `get_openai_client` has Azure SDK distributed tracing.
  The installed Azure Monitor exporter normalizes HTTP dependency names from
  request attributes and maps internal spans such as persistence to `InProc`.
- **Privacy:** this document does not contain sign-in IPs/UPNs, returned records,
  bearer tokens, connection strings, prompts or completions. Content recording
  was enabled in the demo; access to the underlying telemetry must still be
  treated as access to potentially sensitive data.
- **Limits:** zero failed spans does not independently prove answer correctness,
  security or completeness. The separate runtime assertions checked completed
  responses, real Learn/Sentinel tool use, the matching sign-in record and
  persisted outputs. A different run can legitimately have a different span count.

Related repository guidance:
[README](README.md), [observability guide](observability.md), and
[notebook](zolab-ai-agent-demo-win11.ipynb).

Microsoft references:

- [Application Insights telemetry data model](https://learn.microsoft.com/en-us/azure/azure-monitor/app/data-model-complete)
- [AppDependencies table reference](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/appdependencies)
- [Sentinel MCP data exploration tools](https://learn.microsoft.com/en-us/azure/sentinel/datalake/sentinel-mcp-data-exploration-tool)
- [SigninLogs table schema](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/signinlogs)
