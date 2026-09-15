"""Run-scoped KQL and HTML reporting for the Windows notebook."""

import json
from html import escape
from typing import Any
from uuid import UUID


Row = dict[str, Any]
DETAIL_LIMIT = 200
PREVIEW_LIMIT = 1200


def build_observability_queries(run_id: str, *, include_content: bool = False) -> dict[str, str]:
    run_id = str(UUID(run_id))
    if not isinstance(include_content, bool):
        raise TypeError("include_content must be a boolean.")
    scope = f'''let run_id = "{run_id}";
let run_operations = AppDependencies
| where TimeGenerated > ago(6h)
| where tostring(Properties["demo.run_id"]) == run_id
| distinct OperationId;
let spans = AppDependencies
| where TimeGenerated > ago(6h) and OperationId in (run_operations)
| summarize arg_max(TimeGenerated, *) by _ResourceId, OperationId, Id;
let run_context = spans
| where tostring(Properties["demo.run_id"]) == run_id
| summarize arg_max(TimeGenerated, Properties),
    InteractionLabels=make_set_if(tostring(Properties["app.interaction"]), isnotempty(tostring(Properties["app.interaction"])))
    by OperationId
| project OperationId, InteractionLabels,
    RootInteraction=iff(array_length(InteractionLabels) == 1, tostring(InteractionLabels[0]), ""),
    RootAgent=tostring(Properties["gen_ai.agent.name"]),
    RootAgentVersion=tostring(Properties["gen_ai.agent.version"]),
    RootModel=tostring(Properties["gen_ai.request.model"]);
let correlated_spans = spans
| join kind=leftouter (run_context) on OperationId
| extend IsNotebookRoot=tostring(Properties["demo.run_id"]) == run_id,
    Interaction=iff(isempty(RootInteraction), "setup / other", RootInteraction),
    Agent=coalesce(tostring(Properties["gen_ai.agent.name"]), RootAgent),
    AgentVersion=coalesce(tostring(Properties["gen_ai.agent.version"]), RootAgentVersion),
    Model=coalesce(tostring(Properties["gen_ai.response.model"]), tostring(Properties["gen_ai.request.model"]), RootModel);
'''
    content = '''let content_records = AppGenAIContent
| where TimeGenerated > ago(6h) and TraceId in (run_operations)
| summarize arg_max(TimeGenerated, *) by _ResourceId, Id;
let instruction_messages = content_records
| mv-expand Message=parse_json(InputMessages)
| where tostring(Message.role) in ("system", "developer")
| summarize InstructionMessages=make_list(Message) by _ResourceId, Id;
let content_details = content_records
| join kind=leftouter (instruction_messages) on _ResourceId, Id
| extend Instructions=case(isnotempty(SystemInstructions), SystemInstructions,
        array_length(InstructionMessages) > 0, tostring(InstructionMessages), ""),
    InstructionSource=case(isnotempty(SystemInstructions), "SystemInstructions",
        array_length(InstructionMessages) > 0, "InputMessages (system/developer)", "not recorded"),
    ConversationId=tostring(Attributes["gen_ai.conversation.id"]),
    AgentVersion=tostring(Attributes["gen_ai.agent.version"]),
    Operation=tostring(Attributes["gen_ai.operation.name"]),
    InputState=case(isempty(InputMessages), "not recorded", gettype(parse_json(InputMessages)) != "array",
        "invalid JSON message array", array_length(parse_json(InputMessages)) == 0, "empty array", "recorded"),
    OutputState=case(isempty(OutputMessages), "not recorded", gettype(parse_json(OutputMessages)) != "array",
        "invalid JSON message array", array_length(parse_json(OutputMessages)) == 0, "empty array", "recorded");
let content_by_span = content_details
| summarize ContentRecords=count(), ContentIds=make_set(Id),
    Conversations=make_set_if(ConversationId, isnotempty(ConversationId)),
    WithInput=countif(InputState == "recorded"), WithOutput=countif(OutputState == "recorded"),
    InvalidMessages=countif(InputState == "invalid JSON message array" or OutputState == "invalid JSON message array"),
    WithInstructions=countif(isnotempty(Instructions))
    by _ResourceId, TraceId, SpanId;
let enriched_spans = correlated_spans
| join kind=leftouter (content_by_span)
    on _ResourceId, $left.OperationId == $right.TraceId, $left.Id == $right.SpanId
| extend ContentRecords=coalesce(ContentRecords, 0);
'''
    queries = {
        "coverage": scope + '''correlated_spans
| summarize Spans=count(), Failures=countif(Success == false),
    FailedOperations=count_distinctif(OperationId, Success == false),
    AmbiguousSpans=countif(array_length(InteractionLabels) > 1),
    Interactions=make_set(RootInteraction),
    ResponseDependencies=countif(Name endswith "/responses" and tostring(Properties["gen_ai.operation.name"]) == "responses.create"),
    ResponseInteractions=make_set_if(RootInteraction, Name endswith "/responses" and tostring(Properties["gen_ai.operation.name"]) == "responses.create"),
    GenAiSpans=countif(Name startswith "chat "),
    PersistenceSpans=countif(Name == "persist_story" and RootInteraction == "persistence"),
    SpanNames=make_set(Name), Roles=make_set_if(AppRoleName, isnotempty(AppRoleName)),
    Versions=make_set_if(AppVersion, isnotempty(AppVersion))
''',
        "interactions": scope + '''correlated_spans
| summarize RootOperations=countif(IsNotebookRoot), Spans=count(), Failures=countif(Success == false),
    Responses=countif(Name endswith "/responses" and tostring(Properties["gen_ai.operation.name"]) == "responses.create"),
    ToolSpans=countif(Name startswith "execute_tool "),
    RootDurationMs=round(sumif(DurationMs, IsNotebookRoot), 2),
    Agents=make_set_if(Agent, isnotempty(Agent)), AgentVersions=make_set_if(AgentVersion, isnotempty(AgentVersion)),
    Models=make_set_if(Model, isnotempty(Model)) by Interaction
| order by Interaction asc
''',
        "runs_trend": scope + '''correlated_spans
| where IsNotebookRoot and RootInteraction in ("story", "facts", "sentinel")
| where Name == "sentinel-agent-query" or Name startswith "invoke_agent "
| summarize Calls=count(), Failures=countif(Success == false),
    AvgDurationMs=round(avg(DurationMs), 2), P95DurationMs=round(percentile(DurationMs, 95), 2)
    by bin(TimeGenerated, 15m), Interaction, Agent, AgentVersion, Model, AppRoleInstance
| order by TimeGenerated desc
''',
        "end_to_end": scope + content + f'''enriched_spans
| project TimeGenerated, Interaction, Name, Agent, AgentVersion, Model, Success, DurationMs,
    Role=AppRoleName, Host=AppRoleInstance, Region=tostring(Properties["cloud.region"]),
    OperationId, SpanId=Id, ParentId, ResourceId=_ResourceId,
    ContentRecords, ContentIds, Conversations, WithInput, WithOutput, WithInstructions
| order by TimeGenerated asc, OperationId asc, SpanId asc
| take {DETAIL_LIMIT}
''',
        "content_coverage": scope + content + '''enriched_spans
| summarize Spans=count(), SpansWithContent=countif(ContentRecords > 0),
    SpansWithoutContent=countif(ContentRecords == 0), ContentRecords=sum(ContentRecords),
    RecordsWithInput=sum(WithInput), RecordsWithOutput=sum(WithOutput),
    RecordsWithInstructions=sum(WithInstructions),
    InvalidMessageRecords=sum(InvalidMessages),
    InputInteractions=make_set_if(RootInteraction, WithInput > 0),
    OutputInteractions=make_set_if(RootInteraction, WithOutput > 0)
| extend ScopedContentRecords=toscalar(content_records | count)
| extend UnmatchedContentRecords=ScopedContentRecords - ContentRecords
''',
        "content": scope + content + f'''content_details
| join kind=leftouter (run_context) on $left.TraceId == $right.OperationId
| project TimeGenerated, Interaction=RootInteraction, ConversationId, Operation,
    Agent=coalesce(AgentName, RootAgent), AgentVersion=coalesce(AgentVersion, RootAgentVersion),
    Model=coalesce(ModelName, tostring(Attributes["gen_ai.response.model"]), RootModel),
    Role=RoleName, TraceId, SpanId, ContentId=Id, ResourceId=_ResourceId,
    InstructionSource, InputState, OutputState,
    InputCharacters=strlen(InputMessages), OutputCharacters=strlen(OutputMessages),
    InstructionCharacters=strlen(Instructions), ToolDefinitionsCharacters=strlen(ToolDefinitions),
    ToolArgumentsCharacters=strlen(ToolCallArguments), ToolResultCharacters=strlen(ToolCallResult)'''
        + (f''',
    InputPreview=substring(InputMessages, 0, {PREVIEW_LIMIT}), OutputPreview=substring(OutputMessages, 0, {PREVIEW_LIMIT}),
    InstructionPreview=substring(Instructions, 0, {PREVIEW_LIMIT}), ToolDefinitionsPreview=substring(ToolDefinitions, 0, {PREVIEW_LIMIT}),
    ToolArgumentsPreview=substring(ToolCallArguments, 0, {PREVIEW_LIMIT}), ToolResultPreview=substring(ToolCallResult, 0, {PREVIEW_LIMIT})'''
           if include_content else "")
        + f'''
| order by TimeGenerated asc, TraceId asc, SpanId asc, ContentId asc
| take {DETAIL_LIMIT}
''',
        "failures": scope + f'''correlated_spans
| where Success == false
| project TimeGenerated, Interaction, Name, Agent, AgentVersion,
    ResultCode, ErrorType=tostring(Properties["error.type"]), Role=AppRoleName,
    OperationId, SpanId=Id, ParentId
| order by TimeGenerated asc
| take {DETAIL_LIMIT}
''',
        "exceptions": scope + f'''AppExceptions
| where TimeGenerated > ago(6h) and OperationId in (run_operations)
| summarize FirstSeen=min(TimeGenerated), Occurrences=count()
    by OperationId, ParentId, ExceptionType, Message=substring(InnermostMessage, 0, {PREVIEW_LIMIT})
| order by FirstSeen asc
| take {DETAIL_LIMIT}
''',
    }
    return {name: query.strip() for name, query in queries.items()}


def coverage_issues(coverage: Row, expected_interactions: set[str]) -> list[str]:
    issues = []
    if coverage["Spans"] == 0:
        issues.append("No current-run spans have arrived.")
    if coverage["Failures"]:
        issues.append(f"{coverage['Failures']} failed spans across {coverage['FailedOperations']} operations.")
    if coverage["AmbiguousSpans"]:
        issues.append("Conflicting interaction labels share a trace; correlation is ambiguous.")
    for field, label in (("Interactions", "interaction spans"), ("ResponseInteractions", "Responses dependencies")):
        missing = expected_interactions - set(coverage[field])
        if missing:
            issues.append(f"Missing {label}: {', '.join(sorted(missing))}.")
    if coverage["GenAiSpans"] == 0:
        issues.append("No GenAI chat spans have arrived.")
    if coverage["PersistenceSpans"] != 1:
        issues.append(f"Expected one persist_story span; found {coverage['PersistenceSpans']}.")
    return issues


def _text(value: Any) -> str:
    if value is None or value == "":
        return "not recorded"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True)
    return str(value)


def _table(rows: list[Row], columns: list[str]) -> str:
    if not rows:
        return "<p>No records in this snapshot.</p>"
    heading = "".join(f"<th>{escape(column)}</th>" for column in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(_text(row.get(column)))}</td>" for column in columns) + "</tr>"
        for row in rows[:DETAIL_LIMIT]
    )
    return f'<div class="otel-scroll"><table><thead><tr>{heading}</tr></thead><tbody>{body}</tbody></table></div>'


def _section(title: str, rows: list[Row], columns: list[str]) -> str:
    return f"<details><summary>{escape(title)}</summary>{_table(rows, columns)}</details>"


def _frame(body: str) -> str:
    return '''<section class="otel-report" aria-label="Foundry demo observability">
<style>
.otel-report { font: 14px/1.5 system-ui, sans-serif; color: inherit; max-width: 100%; }
.otel-report h2, .otel-report h3 { color: #0078d4; }
.otel-report table { border-collapse: collapse; width: 100%; text-align: left; }
.otel-report th, .otel-report td { border: 1px solid #8886; padding: 6px 9px; text-align: left; overflow-wrap: anywhere; }
.otel-report th { background: #0078d41a; }
.otel-report details { border: 1px solid #8886; border-radius: 5px; margin: 10px 0; padding: 10px; }
.otel-report summary { cursor: pointer; font-weight: 600; }
.otel-report .otel-scroll { max-height: 450px; overflow: auto; }
.otel-report pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 360px; overflow: auto; text-align: left; }
</style>''' + body + "</section>"


def render_failure_report(run_id: str, issues: list[str], failures: list[Row], exceptions: list[Row]) -> str:
    body = f"<h2>FAIL - current-run telemetry</h2><p>Run: <code>{escape(run_id)}</code></p>"
    body += "<ul>" + "".join(f"<li>{escape(issue)}</li>" for issue in issues) + "</ul>"
    body += """<p>One failed call can mark several parent/child spans as failed.
Group by OperationId and follow ParentId to the cause. This strict gate includes earlier
attempts with the same run ID, even if a later retry succeeds. Rerunning Section 6 does not
erase failures. For a clean run, restart the kernel and run the runtime cells, skipping installs.</p>"""
    body += _section("Failed spans (up to 200)", failures, [
        "TimeGenerated", "Interaction", "Name", "ResultCode", "ErrorType", "OperationId", "SpanId", "ParentId",
    ])
    body += "<p>Exception messages may contain sensitive data; they are separate from message-preview controls.</p>"
    body += _section("Correlated exception details (up to 200 groups; messages capped at 1,200 characters)", exceptions, [
        "FirstSeen", "ExceptionType", "Message", "Occurrences", "OperationId", "ParentId",
    ])
    return _frame(body)


def render_observability_report(
    run_id: str, workspace_id: str, coverage: Row, results: dict[str, list[Row]],
    queries: dict[str, str], expected_interactions: set[str], *,
    content_recording_enabled: bool, show_content: bool = False,
) -> str:
    issues = coverage_issues(coverage, expected_interactions)
    if issues:
        raise ValueError("Cannot render a passing report: " + " ".join(issues))
    if show_content and not content_recording_enabled:
        raise ValueError("Message previews require the notebook content-recording policy to be enabled.")
    content = results["content_coverage"][0]
    missing_content = expected_interactions - (
        set(content["InputInteractions"]) & set(content["OutputInteractions"])
    )
    if not content_recording_enabled:
        content_status = "Content capture disabled locally; content is not required for span health."
    elif missing_content:
        content_status = "WAITING / NOT RECORDED - input/output content missing for: " + ", ".join(sorted(missing_content))
    else:
        content_status = "AVAILABLE - input/output content present for each expected interaction."
    body = f"""<h2>Foundry demo: telemetry and observability</h2>
<p><strong>PASS - span health and service identity validated</strong></p>
<p>Run: <code>{escape(run_id)}</code><br>Workspace customer ID: <code>{escape(workspace_id)}</code><br>
Scope: this run's traces within the last six hours; timestamps are UTC. Read-only queries, no agent invocation.</p>
<h3>1. Did the demo produce the required telemetry?</h3>"""
    body += _table([
        {"Signal": "Unique dependency spans", "Value": coverage["Spans"], "Meaning": "Notebook, SDK, transport and Foundry service spans; not a call count."},
        {"Signal": "Failed spans / operations", "Value": f"{coverage['Failures']} / {coverage['FailedOperations']}", "Meaning": "Strict whole-run check, including earlier attempts."},
        {"Signal": "Responses wrappers", "Value": coverage["ResponseDependencies"], "Meaning": "Explicit request spans; approval continuations can add requests."},
        {"Signal": "GenAI chat spans", "Value": coverage["GenAiSpans"], "Meaning": "Observed model-execution spans; not additional notebook interactions."},
        {"Signal": "Persistence", "Value": coverage["PersistenceSpans"], "Meaning": "Exactly one run-correlated persist_story; not an LLM request."},
    ], ["Signal", "Value", "Meaning"])
    body += f"<p>Observed services: {escape(_text(coverage['Roles']))}<br>Observed service versions: {escape(_text(coverage['Versions']))}</p>"
    body += """<h3>2. Follow the notebook workflow</h3>
<p>Setup: Section 4; story and Learn facts: Section 5; persistence: Section 5;
Sentinel: Section 5.1. RootOperations counts notebook orchestration only.
RootDurationMs sums root durations in the stage, not nested child durations or total wall-clock time.</p>"""
    body += _table(results["interactions"], [
        "Interaction", "RootOperations", "Spans", "Responses", "ToolSpans", "Failures",
        "RootDurationMs", "Agents", "AgentVersions", "Models",
    ])
    body += f"""<h3>3. What did the LLM and its tools see and return?</h3>
<p><strong>{escape(content_status)}</strong></p>
<p>AppGenAIContent describes content attached to spans; it does not replace the span inventory.
Blank fields mean not recorded on that record, not an empty answer or a successful tool call.</p>"""
    body += _table([content], [
        "Spans", "SpansWithContent", "SpansWithoutContent", "ContentRecords",
        "RecordsWithInput", "RecordsWithOutput", "RecordsWithInstructions", "UnmatchedContentRecords",
    ])
    body += """<p>Content is joined on resource + trace + span, aggregated before the left join to prevent fan-out.
Spans without content remain visible. Conversation IDs come from Attributes;
instructions prefer SystemInstructions, then structured system/developer input messages.
Client and service snapshots can repeat conversation history: do not sum them as unique turns or token usage.</p>"""
    if content["UnmatchedContentRecords"]:
        body += "<p><strong>WARNING:</strong> Some content has no matching dependency span yet; inspect ingestion/correlation.</p>"
    if content["InvalidMessageRecords"]:
        body += f"<p><strong>WARNING:</strong> {content['InvalidMessageRecords']} matched content records contain invalid JSON message arrays; inspect their InputState/OutputState. They are not counted as valid input/output coverage.</p>"
    body += _section("Conversation and tool content index (up to 200 snapshots)", results["content"], [
        "TimeGenerated", "Interaction", "ConversationId", "Operation", "Agent", "AgentVersion", "Model", "Role",
        "InputState", "OutputState", "InputCharacters", "OutputCharacters", "InstructionSource", "ToolArgumentsCharacters", "ToolResultCharacters",
        "TraceId", "SpanId", "ContentId",
    ])
    if show_content:
        body += "<p><strong>Sensitive previews enabled:</strong> these values are saved in notebook outputs. Clear outputs before sharing.</p>"
        for row in results["content"]:
            title = f"{row['TimeGenerated']} | {row['Interaction']} | {row['Operation']} | span {row['SpanId']}"
            payloads = ""
            for label in ("Input", "Output", "Instruction", "ToolDefinitions", "ToolArguments", "ToolResult"):
                length = row[label + "Characters"]
                if length:
                    value = row[label + "Preview"]
                    note = f"first {PREVIEW_LIMIT} of {length} characters; truncated" if length > PREVIEW_LIMIT else f"{length} characters"
                    payloads += f"<h4>{label} ({note})</h4><pre>{escape(value)}</pre>"
            body += f"<details><summary>{escape(title)}</summary>{payloads or 'No payload recorded.'}</details>"
    else:
        body += "<p>Message/tool payloads are not requested or displayed by default. Set SHOW_GENAI_CONTENT=True in Section 6 to opt into bounded previews; local content recording must also be enabled.</p>"
    body += "<h3>4. Inspect latency, span relationships and exceptions</h3>"
    body += _section("End-to-end span inventory with content links (up to 200)", results["end_to_end"], [
        "TimeGenerated", "Interaction", "Name", "Agent", "AgentVersion", "Model", "Success", "DurationMs",
        "Role", "Host", "Region", "OperationId", "SpanId", "ParentId", "ContentRecords", "ContentIds",
    ])
    body += _section("Root-call trend (15-minute UTC bins; this run only)", results["runs_trend"], [
        "TimeGenerated", "Interaction", "Agent", "AgentVersion", "Model", "Calls", "Failures", "AvgDurationMs", "P95DurationMs",
    ])
    body += "<p>Trends count only notebook story/facts/Sentinel roots, not nested service invoke_agent spans. P95 with one call equals that call's duration.</p>"
    body += _section("Correlated exceptions (up to 200 groups; potentially sensitive messages)", results["exceptions"], [
        "FirstSeen", "ExceptionType", "Message", "Occurrences", "OperationId", "ParentId",
    ])
    body += """<h3>5. Reproduce and explain the evidence</h3>
<p>OperationId = TraceId groups one execution; SpanId identifies one operation within it;
ParentId links to the parent span. ConversationId threads multiple turns/traces.
ContentId identifies a content record, not a span. Detail views cap at 200 rows;
previews cap each payload at 1,200 characters. Coverage counts are uncapped.
Queries run sequentially and ingestion is asynchronous, so snapshots may grow between views.
Content availability is separate from span-health PASS; rerun Section 6 to refresh it.</p>
<p>Before sharing, review notebook outputs and table access: prompts, tool results and exceptions may contain PII.
No message text is read from legacy span content attributes.</p>"""
    for name, query in queries.items():
        body += f"<details><summary>KQL: {escape(name)}</summary><pre><code>{escape(query)}</code></pre></details>"
    return _frame(body)
