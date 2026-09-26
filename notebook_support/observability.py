"""Run- and stage-scoped KQL and HTML reporting for the MAF notebooks."""

import json
from collections.abc import Mapping
from html import escape
from typing import Any
from uuid import UUID

from .response_observability import TokenPricing, price_response_usage


Row = dict[str, Any]
DETAIL_LIMIT = 200
PREVIEW_LIMIT = 1200


def build_observability_queries(
    run_id: str, *, include_content: bool = False, include_tool_content: bool = False,
) -> dict[str, str]:
    run_id = str(UUID(run_id))
    if not isinstance(include_content, bool):
        raise TypeError("include_content must be a boolean.")
    if not isinstance(include_tool_content, bool):
        raise TypeError("include_tool_content must be a boolean.")
    scope = f'''let run_id = "{run_id}";
let run_operations = AppDependencies
| where TimeGenerated > ago(6h)
| where tostring(Properties["demo.run_id"]) == run_id
| distinct OperationId;
let spans = materialize(AppDependencies
| where TimeGenerated > ago(6h) and OperationId in (run_operations)
| summarize arg_max(TimeGenerated, *) by _ResourceId, OperationId, Id);
let span_context = materialize(spans
| extend NodeKey=strcat(OperationId, "/", Id),
    ParentKey=iff(isnotempty(ParentId), strcat(OperationId, "/", ParentId), ""),
    IsRunTagged=tostring(Properties["demo.run_id"]) == run_id,
    GenAiOperation=tostring(Properties["gen_ai.operation.name"]),
    OwnAgent=tostring(Properties["gen_ai.agent.name"]),
    OwnAgentId=tostring(Properties["gen_ai.agent.id"]),
    OwnAgentVersion=tostring(Properties["gen_ai.agent.version"]),
    OwnModel=coalesce(tostring(Properties["gen_ai.response.model"]), tostring(Properties["gen_ai.request.model"])),
    NativeWorkflowName=tostring(Properties["workflow.name"]),
    NativeWorkflowId=tostring(Properties["workflow.id"]),
    IsWorkflowRun=Name == "workflow.run" or Name startswith "workflow.run ",
    IsExecutor=Name == "executor.process" or Name startswith "executor.process "
| extend IsNotebookRoot=IsRunTagged and coalesce(tobool(Properties["app.interaction.root"]), false),
    IsWorkflowRoot=IsRunTagged and coalesce(tobool(Properties["app.workflow.root"]), false),
    TaggedInteraction=iff(IsRunTagged, tostring(Properties["app.interaction"]), ""),
    TaggedWorkflow=iff(IsRunTagged, tostring(Properties["app.workflow.name"]), ""),
    TaggedStep=iff(IsRunTagged, tostring(Properties["app.workflow.step"]), ""),
    IsResponseDependency=Name endswith "/responses" and GenAiOperation == "responses.create",
    IsGenAiSpan=Name == "chat" or Name startswith "chat " or GenAiOperation in ("chat", "generate_content", "text_completion"),
    IsToolSpan=Name == "execute_tool" or Name startswith "execute_tool " or GenAiOperation == "execute_tool",
    IsToolObservation=coalesce(tobool(Properties["app.tool.observation"]), false)
| extend IsWorkflowPlumbing=Name startswith "workflow." or Name startswith "edge." or Name startswith "message."
        or (Name startswith "executor." and not(IsNotebookRoot)),
    IsCriticalSpan=IsNotebookRoot or IsExecutor or IsResponseDependency or IsGenAiSpan or IsToolSpan or IsToolObservation
        or Name endswith "/responses" or Name == "responses" or Name startswith "responses.create"
        or GenAiOperation in ("responses", "responses.create", "invoke_agent")
        or Name == "invoke_agent" or Name startswith "invoke_agent "
);
let graph_nodes = span_context | distinct NodeKey;
let parent_edges = span_context
| where isnotempty(ParentKey)
| distinct NodeKey, ParentKey;
let parent_conflicts = span_context
| summarize ParentKeys=make_set(ParentKey) by NodeKey
| project NodeKey, NodeConflict=array_length(ParentKeys) > 1;
let ancestor_links = materialize(union
    (graph_nodes | project NodeKey, AncestorKey=NodeKey, Depth=tolong(0)),
    (parent_edges
    | make-graph NodeKey --> ParentKey with graph_nodes on NodeKey
    | graph-match cycles=none (child)-[parents*1..64]->(ancestor)
        where isnotempty(child.NodeKey) and isnotempty(ancestor.NodeKey)
        project NodeKey=tostring(child.NodeKey), AncestorKey=tostring(ancestor.NodeKey), ParentPath=map(parents, ParentKey)
    | project NodeKey, AncestorKey, Depth=tolong(array_length(ParentPath)))
| summarize Depth=min(Depth) by NodeKey, AncestorKey);
let ancestor_context = span_context
| join kind=leftouter (parent_conflicts) on NodeKey
| project AncestorKey=NodeKey, AncestorInteraction=TaggedInteraction,
    AncestorWorkflow=TaggedWorkflow, AncestorStep=TaggedStep,
    AncestorIsRoot=IsNotebookRoot, AncestorIsWorkflowRoot=IsWorkflowRoot,
    AncestorIsWorkflowRun=IsWorkflowRun, AncestorNativeName=NativeWorkflowName,
    AncestorNativeId=NativeWorkflowId, AncestorAgent=OwnAgent, AncestorAgentId=OwnAgentId,
    AncestorAgentVersion=OwnAgentVersion, AncestorModel=OwnModel, AncestorConflict=NodeConflict;
let ancestors = materialize(ancestor_links | join kind=inner (ancestor_context) on AncestorKey);
let ancestry_context = ancestors
| summarize StageRootKeys=make_set_if(AncestorKey, AncestorIsRoot),
    RootInteractions=make_set_if(AncestorInteraction, AncestorIsRoot and isnotempty(AncestorInteraction)),
    RootSteps=make_set_if(AncestorStep, AncestorIsRoot and isnotempty(AncestorStep)),
    RootWorkflows=make_set_if(AncestorWorkflow, AncestorIsRoot and isnotempty(AncestorWorkflow)),
    InteractionLabels=make_set_if(AncestorInteraction, isnotempty(AncestorInteraction)),
    StepLabels=make_set_if(AncestorStep, isnotempty(AncestorStep)),
    WorkflowRootKeys=make_set_if(AncestorKey, AncestorIsWorkflowRoot),
    WorkflowRootNames=make_set_if(AncestorWorkflow, AncestorIsWorkflowRoot and isnotempty(AncestorWorkflow)),
    WorkflowLabels=make_set_if(AncestorWorkflow, isnotempty(AncestorWorkflow)),
    NativeWorkflowKeys=make_set_if(AncestorKey, AncestorIsWorkflowRun),
    NativeWorkflowNames=make_set_if(AncestorNativeName, AncestorIsWorkflowRun and isnotempty(AncestorNativeName)),
    NativeWorkflowIds=make_set_if(AncestorNativeId, AncestorIsWorkflowRun and isnotempty(AncestorNativeId)),
    AncestryConflicts=countif(AncestorConflict)
    by NodeKey
| extend WorkflowLabels=set_union(WorkflowLabels, NativeWorkflowNames)
| extend AncestryAmbiguous=AncestryConflicts > 0 or array_length(StageRootKeys) > 1
        or array_length(InteractionLabels) > 1 or array_length(StepLabels) > 1
        or array_length(WorkflowRootKeys) > 1 or array_length(NativeWorkflowKeys) > 1
        or array_length(WorkflowLabels) > 1 or array_length(NativeWorkflowIds) > 1
        or (array_length(RootInteractions) == 1 and array_length(RootSteps) == 1
            and tostring(RootInteractions[0]) != tostring(RootSteps[0])),
    WorkflowName=iff(array_length(WorkflowLabels) == 1, tostring(WorkflowLabels[0]), ""),
    WorkflowId=iff(array_length(NativeWorkflowIds) == 1, tostring(NativeWorkflowIds[0]), ""),
    WorkflowStep=iff(array_length(RootSteps) == 1, tostring(RootSteps[0]), ""),
    WorkflowRootKey=iff(array_length(WorkflowRootKeys) == 1, tostring(WorkflowRootKeys[0]), ""),
    WorkflowRunKey=iff(array_length(NativeWorkflowKeys) == 1, tostring(NativeWorkflowKeys[0]), "")
| extend WorkflowCorrelationState=case(AncestryAmbiguous, "ambiguous",
        array_length(WorkflowRootKeys) == 1 and array_length(WorkflowRootNames) == 1
        and array_length(NativeWorkflowKeys) == 1 and array_length(NativeWorkflowNames) == 1
        and array_length(NativeWorkflowIds) == 1 and array_length(WorkflowLabels) == 1, "correlated",
        "unmapped");
let nearest_metadata = ancestors
| extend Metadata=bag_pack("Agent", AncestorAgent, "AgentId", AncestorAgentId,
        "AgentVersion", AncestorAgentVersion, "Model", AncestorModel)
| mv-expand MetadataKey=bag_keys(Metadata) to typeof(string)
| extend MetadataValue=tostring(Metadata[MetadataKey])
| where isnotempty(MetadataValue)
| summarize MetadataValues=make_set(MetadataValue) by NodeKey, MetadataKey, Depth
| summarize arg_min(Depth, MetadataValues) by NodeKey, MetadataKey
| summarize NearestMetadata=make_bag(bag_pack(MetadataKey,
        iff(array_length(MetadataValues) == 1, tostring(MetadataValues[0]), ""))),
    AmbiguousMetadata=countif(array_length(MetadataValues) > 1) by NodeKey;
let correlated_spans = materialize(span_context
| join kind=leftouter (ancestry_context) on NodeKey
| join kind=leftouter (nearest_metadata) on NodeKey
| extend CorrelationState=case(AncestryAmbiguous or coalesce(AmbiguousMetadata, 0) > 0, "ambiguous",
        array_length(StageRootKeys) == 1 and array_length(RootInteractions) == 1
        and array_length(RootSteps) == 1 and array_length(RootWorkflows) == 1
        and WorkflowCorrelationState == "correlated", "correlated", "unmapped")
| extend RootInteraction=iff(CorrelationState == "correlated", tostring(RootInteractions[0]), ""),
    StageRootKey=iff(CorrelationState == "correlated", tostring(StageRootKeys[0]), ""),
    Agent=iff(CorrelationState == "correlated", tostring(NearestMetadata.Agent), OwnAgent),
    AgentId=iff(CorrelationState == "correlated", tostring(NearestMetadata.AgentId), OwnAgentId),
    AgentVersion=iff(CorrelationState == "correlated", tostring(NearestMetadata.AgentVersion), OwnAgentVersion),
    Model=iff(CorrelationState == "correlated", tostring(NearestMetadata.Model), OwnModel)
| extend Interaction=case(IsWorkflowPlumbing, "workflow / setup",
        CorrelationState == "correlated", RootInteraction, CorrelationState == "ambiguous", "ambiguous",
        IsCriticalSpan, "unmapped / critical", "workflow / setup"),
    SpanCategory=case(IsNotebookRoot and IsExecutor, "executor",
        IsWorkflowPlumbing or IsWorkflowRoot, "workflow / setup",
        IsResponseDependency, "responses", IsGenAiSpan, "model", IsToolObservation, "tool observation",
        IsToolSpan, "tool", "dependency / setup")
);
'''
    content = '''let content_records = AppGenAIContent
| where TimeGenerated > ago(6h) and TraceId in (run_operations)
| summarize arg_max(TimeGenerated, *) by _ResourceId, TraceId, SpanId, Id;
let instruction_messages = content_records
| mv-expand Message=parse_json(InputMessages)
| where tostring(Message.role) in ("system", "developer")
| summarize InstructionMessages=make_list(Message) by _ResourceId, TraceId, SpanId, Id;
let content_details = content_records
| join kind=leftouter (instruction_messages) on _ResourceId, TraceId, SpanId, Id
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
let content_context = correlated_spans
| project _ResourceId, TraceId=OperationId, SpanId=Id, Interaction, CorrelationState,
    WorkflowName, WorkflowId, WorkflowStep, Agent, AgentId, SpanAgentVersion=AgentVersion, Model;
'''
    responses = '''let response_requests = materialize(correlated_spans
| where IsResponseDependency
| extend ResponseId=tostring(Properties["gen_ai.response.id"]),
    Deployment=tostring(Properties["app.model.deployment"]),
    Model=coalesce(tostring(Properties["gen_ai.response.model"]), Model),
    UsageSource=tostring(Properties["app.usage.source"]),
    ReportedUsageState=tostring(Properties["app.usage.state"]),
    ResponseStatus=coalesce(tostring(Properties["app.response.status"]),
        iff(Success == false, "request failed", "not recorded")),
    InputTokens=tolong(Properties["gen_ai.usage.input_tokens"]),
    OutputTokens=tolong(Properties["gen_ai.usage.output_tokens"]),
    CachedInputTokens=tolong(Properties["app.usage.cached_input_tokens"]),
    ReasoningTokens=tolong(Properties["app.usage.reasoning_tokens"]),
    McpToolCalls=tolong(Properties["app.mcp.tool_calls"]),
    McpToolErrors=tolong(Properties["app.mcp.tool_errors"])
| extend UsageState=case(UsageSource == "notebook.responses", coalesce(ReportedUsageState, "invalid"),
        Success == false, "request failed", "not captured; rerun runtime cells"),
    UsageKey=iff(isnotempty(ResponseId),
        strcat("response/", _ResourceId, "/", tostring(Properties["server.address"]), "/", ResponseId),
        strcat("span/", _ResourceId, "/", NodeKey))
| extend UsageState=iff(isempty(ResponseId) and UsageState == "reported", "response id not reported", UsageState),
    UsageIssue=tostring(Properties["app.usage.issue"])
);
let canonical_responses = materialize(response_requests
| extend UsageSignature=tostring(pack_array(StageRootKey, Deployment, Model, UsageState,
    InputTokens, OutputTokens, CachedInputTokens, ReasoningTokens, McpToolCalls, McpToolErrors))
| summarize SnapshotVariants=make_set(UsageSignature, 2), arg_max(TimeGenerated, *) by UsageKey
| extend UsageState=iff(array_length(SnapshotVariants) > 1, "conflicting response snapshots", UsageState)
);
'''
    mcp_events = '''let event_context = correlated_spans
| project _ResourceId, OperationId, ParentId=Id, StageRootKey, Interaction,
    WorkflowName, WorkflowId, WorkflowStep, CorrelationState;
let mcp_events = materialize(AppTraces
| where TimeGenerated > ago(6h) and OperationId in (run_operations)
| where Message in ("mcp.approval.auto_approved", "mcp.tool.error", "sentinel.mcp_tool_error")
| extend EventId=tostring(Properties["app.mcp.event.id"])
| extend EventKey=strcat(_ResourceId, "/", OperationId, "/", ParentId, "/",
    iff(isnotempty(EventId), EventId, strcat(TimeGenerated, "/", Message, "/", tostring(Properties))))
| summarize arg_max(TimeGenerated, *) by EventKey
| join kind=leftouter (event_context) on _ResourceId, OperationId, ParentId
| extend Interaction=coalesce(Interaction, "unmatched / event"),
    CorrelationState=coalesce(CorrelationState, "unmatched span"),
    ApprovalRound=tolong(Properties["app.approval.round"]),
    ApprovedRequests=tolong(Properties["app.approval.count"]),
    ResponseId=tostring(Properties["app.response.id"]),
    ToolCallId=tostring(Properties["app.mcp.call.id"]),
    ToolName=tostring(Properties["app.mcp.tool.name"]),
    ToolStatus=tostring(Properties["app.mcp.tool.status"]),
    Server=tostring(Properties["app.mcp.server"])
);
'''
    queries = {
        "coverage": scope + '''correlated_spans
| summarize Spans=count(), Failures=countif(Success == false),
    FailedOperations=count_distinctif(OperationId, Success == false),
    AmbiguousSpans=countif(CorrelationState == "ambiguous" or WorkflowCorrelationState == "ambiguous"),
    UnmappedCriticalSpans=countif(IsCriticalSpan and CorrelationState == "unmapped"),
    AmbiguousCriticalSpans=countif(IsCriticalSpan and CorrelationState == "ambiguous"),
    Interactions=make_set_if(RootInteraction, IsNotebookRoot and CorrelationState == "correlated"),
    ResponseDependencies=countif(IsResponseDependency),
    ResponseInteractions=make_set_if(RootInteraction, IsResponseDependency and CorrelationState == "correlated"),
    GenAiSpans=countif(IsGenAiSpan and not(IsWorkflowPlumbing)),
    PersistenceSpans=countif(Name == "persist_story" and RootInteraction == "persistence"),
    WorkflowRuns=countif(IsWorkflowRun), WorkflowRootSpans=countif(IsWorkflowRoot),
    ExecutorSpans=countif(IsNotebookRoot and IsExecutor),
    WorkflowNames=make_set_if(WorkflowName, IsWorkflowRun and WorkflowCorrelationState == "correlated"),
    WorkflowIds=make_set_if(WorkflowId, IsWorkflowRun and WorkflowCorrelationState == "correlated"),
    WorkflowSteps=make_set_if(strcat(WorkflowName, "/", WorkflowStep),
        IsNotebookRoot and IsExecutor and CorrelationState == "correlated"),
    WorkflowFailures=countif((IsWorkflowRoot or IsWorkflowRun or IsExecutor) and Success == false),
    UncorrelatedWorkflowSpans=countif((IsWorkflowRun or IsExecutor) and WorkflowCorrelationState != "correlated"),
    SpanNames=make_set(Name), Roles=make_set_if(AppRoleName, isnotempty(AppRoleName)),
    Versions=make_set_if(AppVersion, isnotempty(AppVersion))
''',
        "interactions": scope + '''correlated_spans
| summarize RootOperations=countif(IsNotebookRoot), Spans=count(), Failures=countif(Success == false),
    Responses=countif(IsResponseDependency), ToolSpans=countif(IsToolSpan),
    UnmappedCriticalSpans=countif(IsCriticalSpan and CorrelationState == "unmapped"),
    AmbiguousSpans=countif(CorrelationState == "ambiguous"),
    RootDurationMs=round(sumif(DurationMs, IsNotebookRoot), 2),
    Agents=make_set_if(Agent, isnotempty(Agent)), AgentVersions=make_set_if(AgentVersion, isnotempty(AgentVersion)),
    Models=make_set_if(Model, isnotempty(Model)) by Interaction
| order by Interaction asc
''',
        "workflows": scope + f'''let executor_summary = correlated_spans
| where IsNotebookRoot and IsExecutor and CorrelationState == "correlated"
| summarize ExecutorCount=count(), ExecutorSteps=make_set(WorkflowStep),
    ExecutorFailures=countif(Success == false), ExecutorSpanIds=make_set(Id) by WorkflowRunKey;
let workflow_span_summary = correlated_spans
| where isnotempty(WorkflowRunKey)
| summarize Spans=count(), Failures=countif(Success == false),
    UnmappedCriticalSpans=countif(IsCriticalSpan and CorrelationState == "unmapped"),
    AmbiguousSpans=countif(CorrelationState == "ambiguous") by WorkflowRunKey;
correlated_spans
| where IsWorkflowRun
| join kind=leftouter (executor_summary) on WorkflowRunKey
| join kind=leftouter (workflow_span_summary) on WorkflowRunKey
| project TimeGenerated, WorkflowName=coalesce(WorkflowName, NativeWorkflowName),
    WorkflowId=coalesce(WorkflowId, NativeWorkflowId), NativeDurationMs=DurationMs, Success,
    ExecutorCount=coalesce(ExecutorCount, 0), ExecutorSteps, ExecutorFailures=coalesce(ExecutorFailures, 0),
    Spans, Failures, UnmappedCriticalSpans, AmbiguousSpans, WorkflowCorrelationState,
    WorkflowRootKey, OperationId, SpanId=Id, ParentId, ResourceId=_ResourceId, ExecutorSpanIds
| order by TimeGenerated asc, OperationId asc, SpanId asc
| take {DETAIL_LIMIT}
''',
        "runs_trend": scope + '''correlated_spans
| where IsNotebookRoot and RootInteraction in ("story", "facts", "sentinel")
| where IsExecutor and CorrelationState == "correlated"
| summarize Calls=count(), Failures=countif(Success == false),
    AvgDurationMs=round(avg(DurationMs), 2), P95DurationMs=round(percentile(DurationMs, 95), 2)
    by bin(TimeGenerated, 15m), Interaction, Agent, AgentVersion, Model, AppRoleInstance
| order by TimeGenerated desc
''',
        "end_to_end": scope + content + f'''enriched_spans
| project TimeGenerated, Interaction, SpanCategory, CorrelationState, WorkflowCorrelationState,
    WorkflowName, WorkflowId, WorkflowStep, Name, Agent, AgentId, AgentVersion, Model, Success, DurationMs,
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
    InputInteractions=make_set_if(RootInteraction, WithInput > 0 and CorrelationState == "correlated"),
    OutputInteractions=make_set_if(RootInteraction, WithOutput > 0 and CorrelationState == "correlated")
| extend ScopedContentRecords=toscalar(content_records | count),
    ScopedInvalidMessageRecords=toscalar(content_details
        | where InputState == "invalid JSON message array" or OutputState == "invalid JSON message array" | count)
| extend UnmatchedContentRecords=ScopedContentRecords - ContentRecords,
    UnmatchedInvalidMessageRecords=ScopedInvalidMessageRecords - InvalidMessageRecords
''',
        "content": scope + content + f'''content_details
| join kind=leftouter (content_context) on _ResourceId, TraceId, SpanId
| project TimeGenerated, Interaction=coalesce(Interaction, "unmatched / content"),
    CorrelationState=coalesce(CorrelationState, "unmatched span"), WorkflowName, WorkflowId, WorkflowStep,
    ConversationId, Operation, Agent=coalesce(AgentName, Agent), AgentId,
    AgentVersion=coalesce(AgentVersion, SpanAgentVersion),
    Model=coalesce(ModelName, tostring(Attributes["gen_ai.response.model"]), Model),
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
| where Success == false or (IsCriticalSpan and CorrelationState != "correlated")
    or ((IsWorkflowRun or IsExecutor) and WorkflowCorrelationState != "correlated")
| project TimeGenerated, Interaction, Name, Agent, AgentVersion, Success,
    WorkflowName, WorkflowStep, CorrelationState, WorkflowCorrelationState,
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
        "usage": scope + responses + '''canonical_responses
| project TimeGenerated, UsageKey, Interaction, WorkflowName, WorkflowStep, CorrelationState,
    Deployment, Model, ResponseId, ResponseStatus, Success, UsageState, UsageIssue,
    InputTokens, OutputTokens, CachedInputTokens, ReasoningTokens,
    OperationId, SpanId=Id, ResourceId=_ResourceId
| order by TimeGenerated asc, UsageKey asc
''',
        "mcp": scope + responses + mcp_events + '''let request_summary = response_requests
| where isnotempty(StageRootKey)
| summarize Requests=count(), FailedRequests=countif(Success == false),
    UnsuccessfulResponses=countif(ResponseStatus in ("failed", "incomplete", "cancelled")),
    arg_max(TimeGenerated, ResponseStatus, ResponseId) by StageRootKey
| project StageRootKey, Requests, FailedRequests, UnsuccessfulResponses,
    LatestResponseStatus=ResponseStatus, LatestResponseId=ResponseId;
let tool_summary = canonical_responses
| where isnotempty(StageRootKey)
| summarize CanonicalResponses=count(), McpReportedResponses=countif(isnotnull(McpToolCalls)),
    ReportedToolCalls=sum(McpToolCalls), ReportedToolErrors=sum(McpToolErrors) by StageRootKey;
let event_summary = mcp_events
| where isnotempty(StageRootKey)
| summarize ApprovalRounds=countif(Message == "mcp.approval.auto_approved"),
    ApprovedRequests=sumif(ApprovedRequests, Message == "mcp.approval.auto_approved"),
    ApprovalCountMissing=countif(Message == "mcp.approval.auto_approved" and isnull(ApprovedRequests)),
    ToolErrorEvents=countif(Message in ("mcp.tool.error", "sentinel.mcp_tool_error")) by StageRootKey;
correlated_spans
| where IsNotebookRoot and RootInteraction in ("story", "facts", "sentinel")
| join kind=leftouter (request_summary) on StageRootKey
| join kind=leftouter (tool_summary) on StageRootKey
| join kind=leftouter (event_summary) on StageRootKey
| project TimeGenerated, Interaction=RootInteraction, WorkflowName, WorkflowId, WorkflowStep,
    FinalStageOutcome=coalesce(tostring(Properties["app.workflow.step.status"]), "not recorded"),
    LatestResponseStatus=coalesce(LatestResponseStatus, "not recorded"), LatestResponseId,
    Requests=coalesce(Requests, 0), FailedRequests=coalesce(FailedRequests, 0),
    UnsuccessfulResponses=coalesce(UnsuccessfulResponses, 0),
    ReportedToolCalls=coalesce(ReportedToolCalls, 0), ReportedToolErrors=coalesce(ReportedToolErrors, 0),
    MissingMcpMetadata=coalesce(CanonicalResponses, 0) - coalesce(McpReportedResponses, 0),
    ApprovalRounds=coalesce(ApprovalRounds, 0), ApprovedRequests=coalesce(ApprovedRequests, 0),
    ApprovalCountMissing=coalesce(ApprovalCountMissing, 0), ToolErrorEvents=coalesce(ToolErrorEvents, 0),
    OperationId, SpanId=Id
| order by TimeGenerated asc, OperationId asc, SpanId asc
''',
        "mcp_events": scope + mcp_events + '''mcp_events
| project TimeGenerated, Interaction, WorkflowName, WorkflowStep, CorrelationState,
    Event=Message, ResponseId, ToolCallId, ToolName, ToolStatus, Server, ApprovalRound, ApprovedRequests,
    OperationId, ParentId, ResourceId=_ResourceId'''
        + (f''',
    ErrorDetailPreview=substring(coalesce(tostring(Properties["app.mcp.error.detail"]),
        tostring(Properties["app.sentinel.error.details"])), 0, {PREVIEW_LIMIT}),
    DetailTruncated=coalesce(tobool(Properties["app.mcp.error.detail_truncated"]), false)
        or strlen(tostring(Properties["app.sentinel.error.details"])) > {PREVIEW_LIMIT}'''
           if include_content else "")
        + f'''
| order by TimeGenerated asc, OperationId asc, ParentId asc
| take {DETAIL_LIMIT}
''',
    }
    if include_tool_content:
        tool_content = '''let tool_payloads = content_records
| summarize arg_max(TimeGenerated, *) by _ResourceId, TraceId, SpanId
| project _ResourceId, TraceId, SpanId, ContentId=Id,
    ToolCallArguments, ToolCallResult, ToolDefinitions;
let tool_observations = materialize(correlated_spans
| where IsToolObservation
| join kind=leftouter (tool_payloads)
    on _ResourceId, $left.OperationId == $right.TraceId, $left.Id == $right.SpanId
| extend OutputItemId=tostring(Properties["app.tool.output_item.id"]),
    ItemType=tostring(Properties["app.tool.output_item.type"]),
    ResponseId=tostring(Properties["gen_ai.response.id"]),
    ConversationId=tostring(Properties["gen_ai.conversation.id"]),
    ToolCallId=tostring(Properties["gen_ai.tool.call.id"]),
    ApprovalId=tostring(Properties["app.mcp.approval.id"]),
    ToolName=tostring(Properties["gen_ai.tool.name"]),
    Server=tostring(Properties["app.mcp.server"]),
    ToolStatus=tostring(Properties["app.mcp.tool.status"]),
    ReturnedError=coalesce(tobool(Properties["app.tool.returned_error"]), false),
    ArgumentsReturned=coalesce(tobool(Properties["app.tool.arguments.present"]), false),
    ResultReturned=coalesce(tobool(Properties["app.tool.result.present"]), false),
    DefinitionsReturned=coalesce(tobool(Properties["app.tool.definitions.present"]), false),
    ArgumentsCharacters=tolong(Properties["app.tool.arguments.characters"]),
    ResultCharacters=tolong(Properties["app.tool.result.characters"]),
    DefinitionsCharacters=tolong(Properties["app.tool.definitions.characters"])
| extend PayloadState=case(
    not(ArgumentsReturned or ResultReturned or DefinitionsReturned), "no payload returned",
    coalesce(ArgumentsCharacters, 0) + coalesce(ResultCharacters, 0) + coalesce(DefinitionsCharacters, 0) == 0, "empty payload returned",
    isempty(ContentId), "waiting / not recorded",
    (ArgumentsReturned and strlen(ToolCallArguments) != ArgumentsCharacters)
        or (ResultReturned and strlen(ToolCallResult) != ResultCharacters)
        or (DefinitionsReturned and strlen(ToolDefinitions) != DefinitionsCharacters), "incomplete payload",
    "available")
);
'''
        tool_scope = scope + content + tool_content
        queries["tool_content_coverage"] = tool_scope + '''let capture_requests = correlated_spans
| where IsResponseDependency and (tostring(Properties["app.usage.source"]) == "notebook.responses"
    or isnotnull(tobool(Properties["app.tool.content.enabled"])))
| extend CaptureEnabled=tobool(Properties["app.tool.content.enabled"]),
    ExpectedItems=tolong(Properties["app.tool.content.items"])
| summarize Requests=count(), CaptureEnabledRequests=countif(CaptureEnabled == true),
    CaptureDisabledRequests=countif(CaptureEnabled == false),
    PolicyNotRecorded=countif(isnull(CaptureEnabled)),
    OutputNotReported=countif(CaptureEnabled == true and isnull(ExpectedItems)),
    ExpectedItems=sum(ExpectedItems) by StageRootKey;
let observed_items = tool_observations
| summarize ObservedItems=count(), McpCalls=countif(ItemType == "mcp_call"),
    ApprovalRequests=countif(ItemType == "mcp_approval_request"), ToolLists=countif(ItemType == "mcp_list_tools"),
    PayloadsAvailable=countif(PayloadState in ("available", "empty payload returned")),
    WaitingContent=countif(PayloadState == "waiting / not recorded"),
    IncompleteContent=countif(PayloadState == "incomplete payload"),
    ReportedErrors=countif(ReturnedError) by StageRootKey;
correlated_spans
| where IsNotebookRoot and RootInteraction in ("story", "facts", "sentinel")
| join kind=leftouter (capture_requests) on StageRootKey
| join kind=leftouter (observed_items) on StageRootKey
| project Interaction=RootInteraction, Requests=coalesce(Requests, 0),
    CaptureEnabledRequests=coalesce(CaptureEnabledRequests, 0),
    CaptureDisabledRequests=coalesce(CaptureDisabledRequests, 0), PolicyNotRecorded=coalesce(PolicyNotRecorded, 0),
    OutputNotReported=coalesce(OutputNotReported, 0),
    ExpectedItems=coalesce(ExpectedItems, 0), ObservedItems=coalesce(ObservedItems, 0),
    McpCalls=coalesce(McpCalls, 0), ApprovalRequests=coalesce(ApprovalRequests, 0), ToolLists=coalesce(ToolLists, 0),
    PayloadsAvailable=coalesce(PayloadsAvailable, 0), WaitingContent=coalesce(WaitingContent, 0),
    IncompleteContent=coalesce(IncompleteContent, 0), ReportedErrors=coalesce(ReportedErrors, 0),
    OperationId, SpanId=Id
| order by Interaction asc
'''
        queries["tool_content"] = tool_scope + '''tool_observations
| project TimeGenerated, Interaction, WorkflowName, WorkflowId, WorkflowStep, CorrelationState,
    ItemType, ToolName, Server, ToolStatus, ReturnedError, PayloadState,
    ResponseId, ConversationId, OutputItemId, ToolCallId, ApprovalId,
    ArgumentsReturned, ResultReturned, DefinitionsReturned,
    ArgumentsCharacters, ResultCharacters, DefinitionsCharacters,
    OperationId, SpanId=Id, ParentId, ContentId, ResourceId=_ResourceId'''
        if include_content:
            queries["tool_content"] += f''',
    ArgumentsPreview=substring(ToolCallArguments, 0, {PREVIEW_LIMIT}),
    ResultPreview=substring(ToolCallResult, 0, {PREVIEW_LIMIT}),
    DefinitionsPreview=substring(ToolDefinitions, 0, {PREVIEW_LIMIT})'''
        queries["tool_content"] += f'''
| order by TimeGenerated asc, OperationId asc, SpanId asc
| take {DETAIL_LIMIT}
'''
    return {name: query.strip() for name, query in queries.items()}


def coverage_issues(coverage: Row, expected_interactions: set[str]) -> list[str]:
    """Validate span health and this notebook's required native MAF workflows."""
    issues = []
    if coverage["Spans"] == 0:
        issues.append("No current-run spans have arrived.")
    if coverage["Failures"]:
        issues.append(f"{coverage['Failures']} failed spans across {coverage['FailedOperations']} operations.")
    if coverage["AmbiguousSpans"]:
        issues.append("Conflicting span ancestry or metadata makes correlation ambiguous.")
    if coverage["UnmappedCriticalSpans"]:
        issues.append(
            f"{coverage['UnmappedCriticalSpans']} model/Responses/tool/executor spans have no correlated "
            "executor ancestor; inspect missing parents, root attributes or the 64-edge ancestry limit."
        )
    if coverage["AmbiguousCriticalSpans"]:
        issues.append(f"{coverage['AmbiguousCriticalSpans']} critical spans have ambiguous ancestry or metadata.")
    for field, label in (("Interactions", "interaction spans"), ("ResponseInteractions", "Responses dependencies")):
        missing = expected_interactions - set(coverage[field])
        if missing:
            issues.append(f"Missing {label}: {', '.join(sorted(missing))}.")
    if coverage["GenAiSpans"] == 0:
        issues.append("No GenAI chat spans have arrived.")
    if coverage["PersistenceSpans"] != 1:
        issues.append(f"Expected one persist_story span; found {coverage['PersistenceSpans']}.")
    required_workflows = {"story-facts"}
    required_steps = {"story-facts/story", "story-facts/facts", "story-facts/persistence"}
    if "sentinel" in expected_interactions:
        required_workflows.add("sentinel")
        required_steps.add("sentinel/sentinel")
    missing_workflows = required_workflows - set(coverage["WorkflowNames"])
    if missing_workflows:
        issues.append(f"Missing correlated native workflows: {', '.join(sorted(missing_workflows))}.")
    missing_steps = required_steps - set(coverage["WorkflowSteps"])
    if missing_steps:
        issues.append(f"Missing workflow executor steps: {', '.join(sorted(missing_steps))}.")
    if coverage["WorkflowRuns"] < len(required_workflows):
        issues.append("Missing native workflow.run spans.")
    if coverage["WorkflowRootSpans"] < len(required_workflows):
        issues.append("Missing notebook.workflow root spans.")
    if len(set(coverage["WorkflowIds"])) < len(required_workflows):
        issues.append("Missing distinct native workflow IDs.")
    if coverage["ExecutorSpans"] < len(required_steps):
        issues.append("Missing explicitly tagged native executor roots.")
    if coverage["WorkflowFailures"]:
        issues.append(f"{coverage['WorkflowFailures']} failed workflow/executor spans.")
    if coverage["UncorrelatedWorkflowSpans"]:
        issues.append(
            f"{coverage['UncorrelatedWorkflowSpans']} native workflow/executor spans have missing or "
            "contradictory workflow name, ID or parent correlation."
        )
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


def _validate_content_policy(content_recording_enabled: bool, show_content: bool) -> None:
    if not isinstance(content_recording_enabled, bool) or not isinstance(show_content, bool):
        raise TypeError("content_recording_enabled and show_content must be booleans.")
    if show_content and not content_recording_enabled:
        raise ValueError("Message previews require the notebook content-recording policy to be enabled.")


def _response_diagnostics(
    results: dict[str, list[Row]], pricing: Mapping[str, TokenPricing], *, show_content: bool,
) -> str:
    summaries, responses = price_response_usage(results["usage"], pricing)
    body = """<h3>Response token usage and estimated cost</h3>
<p>Only the notebook's explicit Responses request spans supply usage. SDK/model/transport spans
and content snapshots are not added to these totals. Response IDs are deduplicated per resource/host;
requests without a response ID remain separate trace/span records and are not priced.
Conflicting snapshots are explicitly unpriced. Cached input is part of input tokens;
reasoning is part of output tokens, not an additional charge.</p>"""
    body += f"<p>{len(responses)} canonical response/request records; totals use all records, not just the first {DETAIL_LIMIT} displayed.</p>"
    if not pricing:
        body += "<p><strong>Pricing not configured.</strong> Set NOTEBOOK_MODEL_PRICING_JSON to your verified deployment/model rates per million tokens and rerun Section 6. Missing prices are not zero cost.</p>"
    body += """<p>Estimates use supplied rates at report time, with an exact deployment match preferred
over an exact response-model match. No Azure billing lookup or currency conversion is performed.
Missing/invalid usage, absent cache details, and missing rates remain unpriced.
A partial estimate is only the known subtotal, not the run's total cost.
Tool charges, special cache-write charges, provisioned capacity, storage, taxes and other charges are excluded.</p>"""
    body += _table(summaries, [
        "Interaction", "Deployment", "Model", "ResponseRecords", "UsageReported", "UsageMissing",
        "KnownInputTokens", "KnownOutputTokens", "KnownCachedInputTokens", "KnownReasoningTokens",
        "CachedUsageMissing", "ReasoningUsageMissing", "Currency", "EstimatedCost",
        "PricedResponses", "UnpricedResponses", "EstimateCoverage",
    ])
    if len(summaries) > DETAIL_LIMIT:
        body += f"<p>Showing {DETAIL_LIMIT} of {len(summaries)} summary groups; no cross-currency grand total is implied.</p>"
    body += _section("Configured token rates (per million tokens)", [
        {"PricingKey": name, "Currency": rates.currency, "InputRate": rates.input_per_million,
         "CachedInputRate": rates.cached_input_per_million, "OutputRate": rates.output_per_million,
         "Source": rates.source}
        for name, rates in pricing.items()
    ], ["PricingKey", "Currency", "InputRate", "CachedInputRate", "OutputRate", "Source"])
    body += _section("Canonical response accounting (up to 200 records)", responses, [
        "TimeGenerated", "Interaction", "WorkflowStep", "Deployment", "Model", "ResponseId",
        "ResponseStatus", "Success", "UsageState", "UsageIssue", "InputTokens", "OutputTokens",
        "CachedInputTokens", "ReasoningTokens", "PricingKey", "Currency", "EstimatedCost", "CostState",
        "OperationId", "SpanId",
    ])
    body += """<h3>MCP approvals, tool errors and final outcomes</h3>
<p>Requests counts explicit Responses API invocations, including approval continuations;
it does not count each SDK-internal HTTP retry. Failed requests remain visible beside the latest
response and final executor outcome. FailedRequests counts SDK invocations that raised;
UnsuccessfulResponses counts returned failed/incomplete/cancelled responses even with HTTP 200.
Reported tool counts describe MCP items returned by Responses,
not independent measurements of remote tool execution. Missing MCP metadata is not zero tool use.</p>"""
    body += _table(results["mcp"], [
        "Interaction", "WorkflowName", "WorkflowId", "FinalStageOutcome", "LatestResponseStatus",
        "Requests", "FailedRequests", "UnsuccessfulResponses", "ApprovalRounds", "ApprovedRequests", "ApprovalCountMissing",
        "ReportedToolCalls", "ReportedToolErrors", "MissingMcpMetadata", "ToolErrorEvents",
        "LatestResponseId", "OperationId", "SpanId",
    ])
    body += """<p>Approval rounds count approval events; approved requests sum their request counts.
ToolErrorEvents can include both individual tool errors and Sentinel summary events, so it is not
a distinct-tool-failure count. Events export with spans to AppTraces even with log collection disabled.
They join to their exact resource/trace/parent span before stage attribution. Unmatched events stay visible.
The strict whole-run failure gate is unchanged: an eventual successful outcome does not erase earlier failures.
Ingestion is asynchronous; absent events are not proof that a remote tool ran without errors.</p>"""
    columns = [
        "TimeGenerated", "Interaction", "WorkflowStep", "Event", "Server", "ToolName", "ToolStatus",
        "ApprovalRound", "ApprovedRequests", "ResponseId", "ToolCallId", "CorrelationState",
        "OperationId", "ParentId",
    ]
    events = results["mcp_events"]
    if show_content:
        columns += ["ErrorDetailPreview", "DetailTruncated"]
        events = [
            dict(row, ErrorDetailPreview=str(row["ErrorDetailPreview"])[:PREVIEW_LIMIT])
            for row in events[:DETAIL_LIMIT]
        ]
        body += "<p><strong>Sensitive MCP error previews enabled:</strong> at most 1,200 characters per event; previews are saved in notebook outputs.</p>"
    else:
        body += "<p>MCP error payloads are not requested or displayed. Bounded previews require both content recording and SHOW_GENAI_CONTENT.</p>"
    body += _section("MCP event evidence (up to 200 events)", events, columns)
    if {"tool_content", "tool_content_coverage"} & results.keys():
        body += """<h3>Tool-content observations (OTEL_LOG_TOOL_CONTENT)</h3>
<p>These are MCP output items observed after each Responses request, including
approval continuations. They are not additional tool invocations, measured remote
tool durations, or extra token usage. Calls, approval requests, and tool discovery
are counted separately. A returned tool error does not mean the local observation
failed; the existing response/executor failure policy is unchanged.</p>
<p>Coverage uses all observation spans. ExpectedItems comes from the request span;
fewer ObservedItems can mean ingestion is pending. CaptureDisabledRequests records
an explicit opt-out, while PolicyNotRecorded identifies older/missing instrumentation.
OutputNotReported means no output list was returned (including failed requests), not zero tool use.
Tool payload ingestion is separate from span-health PASS; absent results are not
invented and an empty returned string is distinct from an unavailable result.</p>"""
        body += _table(results["tool_content_coverage"], [
            "Interaction", "Requests", "CaptureEnabledRequests", "CaptureDisabledRequests", "PolicyNotRecorded", "OutputNotReported",
            "ExpectedItems", "ObservedItems", "McpCalls", "ApprovalRequests", "ToolLists",
            "PayloadsAvailable", "WaitingContent", "IncompleteContent", "ReportedErrors",
        ])
        tool_rows = results["tool_content"]
        tool_columns = [
            "TimeGenerated", "Interaction", "WorkflowStep", "ItemType", "ToolName", "Server",
            "ToolStatus", "ReturnedError", "PayloadState", "ArgumentsReturned", "ResultReturned", "DefinitionsReturned",
            "ArgumentsCharacters", "ResultCharacters", "DefinitionsCharacters",
            "ResponseId", "ConversationId", "OutputItemId", "ToolCallId", "ApprovalId",
            "CorrelationState", "OperationId", "SpanId", "ParentId", "ContentId",
        ]
        if show_content:
            preview_columns = ["ArgumentsPreview", "ResultPreview", "DefinitionsPreview"]
            visible_rows = []
            for row in tool_rows[:DETAIL_LIMIT]:
                previews = {}
                for label in ("Arguments", "Result", "Definitions"):
                    value = str(row[label + "Preview"])[:PREVIEW_LIMIT]
                    if not row[label + "Returned"]:
                        value = "not returned"
                    elif row[label + "Characters"] == 0:
                        value = "empty returned string"
                    previews[label + "Preview"] = value
                visible_rows.append(dict(row, **previews))
            tool_rows = visible_rows
            tool_columns += preview_columns
            body += "<p>Tool previews show at most 1,200 characters per field. Returned text is submitted in the GenAI attributes; this Azure Monitor exporter caps each at 262,144 characters, and service limits still apply. Length differences are reported as incomplete payloads.</p>"
        else:
            body += "<p>Tool payload previews are not requested or displayed; enable SHOW_GENAI_CONTENT to view them.</p>"
        body += _section("MCP tool content (up to 200 observations)", tool_rows, tool_columns)
    return body


def render_failure_report(
    run_id: str, issues: list[str], failures: list[Row], exceptions: list[Row], *,
    diagnostics: dict[str, list[Row]] | None = None,
    model_pricing: Mapping[str, TokenPricing] | None = None,
    content_recording_enabled: bool = False, show_content: bool = False,
) -> str:
    _validate_content_policy(content_recording_enabled, show_content)
    body = f"<h2>FAIL - current-run telemetry</h2><p>Run: <code>{escape(run_id)}</code></p>"
    body += "<ul>" + "".join(f"<li>{escape(issue)}</li>" for issue in issues) + "</ul>"
    body += """<p>One failed call can mark several parent/child spans as failed.
Group by OperationId and follow ParentId to the cause. This strict gate includes earlier
attempts with the same run ID, even if a later retry succeeds. Rerunning Section 6 does not
erase failures. For a clean run, restart the kernel and run the runtime cells, skipping installs.</p>"""
    body += _section("Failed spans and critical correlation diagnostics (up to 200)", failures, [
        "TimeGenerated", "Interaction", "WorkflowName", "WorkflowStep", "Name", "Success",
        "CorrelationState", "WorkflowCorrelationState", "ResultCode", "ErrorType", "OperationId", "SpanId", "ParentId",
    ])
    body += "<p>Exception messages may contain sensitive data; they are separate from message-preview controls.</p>"
    body += _section("Correlated exception details (up to 200 groups; messages capped at 1,200 characters)", exceptions, [
        "FirstSeen", "ExceptionType", "Message", "Occurrences", "OperationId", "ParentId",
    ])
    if diagnostics is not None:
        body += _response_diagnostics(
            diagnostics, model_pricing if model_pricing is not None else {}, show_content=show_content,
        )
    return _frame(body)


def render_observability_report(
    run_id: str, workspace_id: str, coverage: Row, results: dict[str, list[Row]],
    queries: dict[str, str], expected_interactions: set[str], *,
    content_recording_enabled: bool, show_content: bool = False,
    model_pricing: Mapping[str, TokenPricing] | None = None,
) -> str:
    _validate_content_policy(content_recording_enabled, show_content)
    issues = coverage_issues(coverage, expected_interactions)
    if issues:
        raise ValueError("Cannot render a passing report: " + " ".join(issues))
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
        {"Signal": "Native workflow runs / executor roots", "Value": f"{coverage['WorkflowRuns']} / {coverage['ExecutorSpans']}", "Meaning": "MAF workflow.run spans and explicitly tagged executor.process roots."},
        {"Signal": "Unmapped / ambiguous critical spans", "Value": f"{coverage['UnmappedCriticalSpans']} / {coverage['AmbiguousCriticalSpans']}", "Meaning": "Missing or contradictory model/Responses/tool/executor ancestry is a health failure."},
        {"Signal": "Workflow failures / uncorrelated workflow spans", "Value": f"{coverage['WorkflowFailures']} / {coverage['UncorrelatedWorkflowSpans']}", "Meaning": "Native workflow identity and executor correlation are required, independently of content."},
    ], ["Signal", "Value", "Meaning"])
    body += f"<p>Observed services: {escape(_text(coverage['Roles']))}<br>Observed service versions: {escape(_text(coverage['Versions']))}</p>"
    body += """<h3>2. Follow the notebook workflow</h3>
<p>MAF orchestrates unchanged Foundry API calls. The story-facts workflow executes story, facts and persistence;
the separate optional sentinel workflow executes Sentinel and its existing persistence.
NativeDurationMs is the native workflow.run wall-clock duration, never a sum of nested spans.
Workflow/graph plumbing remains visible as workflow / setup, not model calls.</p>"""
    body += _table(results["workflows"], [
        "TimeGenerated", "WorkflowName", "WorkflowId", "NativeDurationMs", "Success",
        "ExecutorCount", "ExecutorSteps", "ExecutorFailures", "Spans", "Failures",
        "UnmappedCriticalSpans", "AmbiguousSpans", "WorkflowCorrelationState",
        "OperationId", "SpanId", "ParentId",
    ])
    body += f"<p>Validated workflow steps: {escape(_text(coverage['WorkflowSteps']))}</p>"
    body += """<p>RootOperations counts only app.interaction.root=true executor spans, not every span tagged with demo.run_id.
RootDurationMs sums root durations in the stage, not nested child durations or total wall-clock time.
Sibling story/facts/persistence stages can share one trace; each child follows ParentId to its own executor.</p>"""
    body += _table(results["interactions"], [
        "Interaction", "RootOperations", "Spans", "Responses", "ToolSpans", "Failures",
        "RootDurationMs", "UnmappedCriticalSpans", "AmbiguousSpans", "Agents", "AgentVersions", "Models",
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
    if content.get("UnmatchedInvalidMessageRecords", 0):
        body += f"<p><strong>WARNING:</strong> {content['UnmatchedInvalidMessageRecords']} unmatched content records contain invalid JSON message arrays.</p>"
    body += _section("Conversation and tool content index (up to 200 snapshots)", results["content"], [
        "TimeGenerated", "Interaction", "CorrelationState", "WorkflowName", "WorkflowStep",
        "ConversationId", "Operation", "Agent", "AgentVersion", "Model", "Role",
        "InputState", "OutputState", "InputCharacters", "OutputCharacters", "InstructionSource", "ToolArgumentsCharacters", "ToolResultCharacters",
        "TraceId", "SpanId", "ContentId",
    ])
    if show_content:
        body += "<p><strong>Sensitive previews enabled:</strong> these values are saved in notebook outputs. Clear outputs before sharing.</p>"
        for row in results["content"][:DETAIL_LIMIT]:
            title = f"{row['TimeGenerated']} | {row['Interaction']} | {row['Operation']} | span {row['SpanId']}"
            payloads = ""
            for label in ("Input", "Output", "Instruction", "ToolDefinitions", "ToolArguments", "ToolResult"):
                length = row[label + "Characters"]
                if length:
                    value = row[label + "Preview"]
                    note = f"first {PREVIEW_LIMIT} of {length} characters; truncated" if length > PREVIEW_LIMIT else f"{length} characters"
                    payloads += f"<h4>{label} ({note})</h4><pre>{escape(value[:PREVIEW_LIMIT])}</pre>"
            body += f"<details><summary>{escape(title)}</summary>{payloads or 'No payload recorded.'}</details>"
    else:
        body += "<p>Message/tool payloads are not requested or displayed by default. Set SHOW_GENAI_CONTENT=True in Section 6 to opt into bounded previews; local content recording must also be enabled.</p>"
    body += "<h3>4. Inspect latency, span relationships and exceptions</h3>"
    body += _section("End-to-end span inventory with content links (up to 200)", results["end_to_end"], [
        "TimeGenerated", "Interaction", "SpanCategory", "CorrelationState", "WorkflowCorrelationState",
        "WorkflowName", "WorkflowStep", "Name", "Agent", "AgentVersion", "Model", "Success", "DurationMs",
        "Role", "Host", "Region", "OperationId", "SpanId", "ParentId", "ContentRecords", "ContentIds",
    ])
    body += _section("Root-call trend (15-minute UTC bins; this run only)", results["runs_trend"], [
        "TimeGenerated", "Interaction", "Agent", "AgentVersion", "Model", "Calls", "Failures", "AvgDurationMs", "P95DurationMs",
    ])
    body += "<p>Trends count only explicitly tagged native story/facts/Sentinel executor roots, not nested invoke_agent or Responses spans. P95 with one call equals that executor's duration.</p>"
    body += _section("Correlated exceptions (up to 200 groups; potentially sensitive messages)", results["exceptions"], [
        "FirstSeen", "ExceptionType", "Message", "Occurrences", "OperationId", "ParentId",
    ])
    body += _response_diagnostics(
        results, model_pricing if model_pricing is not None else {}, show_content=show_content,
    )
    body += """<h3>5. Reproduce and explain the evidence</h3>
<p>OperationId = TraceId groups a distributed trace, which can include several interactions;
SpanId identifies one operation within it. ParentId ancestry (at most 64 edges) determines stage attribution,
using trace + span across resources, never a single label guessed for the entire trace.
Missing or contradictory critical ancestry fails validation. ConversationId threads multiple turns/traces.
ContentId identifies a content record, not a span. Detail views cap at 200 rows;
previews cap each payload at 1,200 characters. Coverage counts are uncapped.
Queries run sequentially and ingestion is asynchronous, so snapshots may grow between views.
Content availability is separate from span-health PASS; rerun Section 6 to refresh it.</p>
<p>Before sharing, review notebook outputs and table access: prompts, tool results and exceptions may contain PII.
No message text is read from legacy span content attributes.</p>"""
    for name, query in queries.items():
        body += f"<details><summary>KQL: {escape(name)}</summary><pre><code>{escape(query)}</code></pre></details>"
    return _frame(body)
