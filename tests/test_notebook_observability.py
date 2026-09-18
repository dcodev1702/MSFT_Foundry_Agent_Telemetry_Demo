import ast
import json
import unittest
from pathlib import Path

from notebook_support.observability import (
    build_observability_queries, coverage_issues, render_failure_report,
    render_observability_report,
)


RUN_ID = "00000000-0000-0000-0000-000000000001"
EXPECTED = {"story", "facts", "sentinel"}


def passing_coverage():
    return {
        "Spans": 64, "Failures": 0, "FailedOperations": 0, "AmbiguousSpans": 0,
        "UnmappedCriticalSpans": 0, "AmbiguousCriticalSpans": 0,
        "Interactions": list(EXPECTED), "ResponseInteractions": list(EXPECTED),
        "ResponseDependencies": 8, "GenAiSpans": 13, "PersistenceSpans": 1,
        "WorkflowRuns": 2, "WorkflowRootSpans": 2, "ExecutorSpans": 4,
        "WorkflowNames": ["story-facts", "sentinel"], "WorkflowIds": ["main-id", "sentinel-id"],
        "WorkflowSteps": ["story-facts/story", "story-facts/facts", "story-facts/persistence", "sentinel/sentinel"],
        "WorkflowFailures": 0, "UncorrelatedWorkflowSpans": 0,
        "Roles": ["client", "responsesapi"], "Versions": ["2026.09.15"],
    }


def report_results():
    return {
        "content_coverage": [{
            "Spans": 64, "SpansWithContent": 34, "SpansWithoutContent": 30,
            "ContentRecords": 34, "ScopedContentRecords": 34, "UnmatchedContentRecords": 0,
            "RecordsWithInput": 29, "RecordsWithOutput": 29, "RecordsWithInstructions": 18,
            "InvalidMessageRecords": 0, "ScopedInvalidMessageRecords": 0, "UnmatchedInvalidMessageRecords": 0,
            "InputInteractions": list(EXPECTED), "OutputInteractions": list(EXPECTED),
        }],
        "workflows": [{
            "WorkflowName": "story-facts", "WorkflowId": "main-id", "NativeDurationMs": 1200,
            "ExecutorCount": 3, "ExecutorSteps": ["story", "facts", "persistence"],
            "ExecutorFailures": 0, "WorkflowCorrelationState": "correlated",
        }, {
            "WorkflowName": "sentinel", "WorkflowId": "sentinel-id", "NativeDurationMs": 800,
            "ExecutorCount": 1, "ExecutorSteps": ["sentinel"],
            "ExecutorFailures": 0, "WorkflowCorrelationState": "correlated",
        }],
        "interactions": [], "content": [], "end_to_end": [], "runs_trend": [], "exceptions": [],
    }


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.queries = build_observability_queries(RUN_ID)

    def test_run_id_is_validated_before_interpolation(self):
        with self.assertRaises(ValueError):
            build_observability_queries('"; AppDependencies | take 100; //')
        with self.assertRaises(TypeError):
            build_observability_queries(RUN_ID, include_content="false")

    def test_every_query_is_current_run_scoped_and_bounded_in_time(self):
        self.assertEqual(set(self.queries), {
            "coverage", "interactions", "workflows", "runs_trend", "end_to_end",
            "content_coverage", "content", "failures", "exceptions",
        })
        for query in self.queries.values():
            self.assertIn(f'let run_id = "{RUN_ID}";', query)
            self.assertIn('Properties["demo.run_id"]', query)
            self.assertIn("ago(6h)", query)

    def test_health_and_trends_do_not_depend_on_content(self):
        for name in ("coverage", "interactions", "workflows", "runs_trend", "failures", "exceptions"):
            self.assertNotIn("AppGenAIContent", self.queries[name])
        self.assertIn('where IsNotebookRoot and RootInteraction in ("story", "facts", "sentinel")', self.queries["runs_trend"])

    def test_join_cannot_multiply_metrics_or_drop_spans_without_content(self):
        query = self.queries["end_to_end"]
        self.assertIn("arg_max(TimeGenerated, *) by _ResourceId, OperationId, Id", query)
        self.assertIn("arg_max(TimeGenerated, *) by _ResourceId, TraceId, SpanId, Id", query)
        self.assertIn("by _ResourceId, TraceId, SpanId;", query)
        self.assertIn("join kind=leftouter (content_by_span)", query)
        self.assertIn("on _ResourceId, $left.OperationId == $right.TraceId, $left.Id == $right.SpanId", query)
        self.assertIn("AmbiguousSpans=countif", self.queries["coverage"])

    def test_executor_roots_are_explicit_and_manual_children_are_not_calls(self):
        for query in self.queries.values():
            self.assertIn(
                'IsNotebookRoot=IsRunTagged and coalesce(tobool(Properties["app.interaction.root"]), false)',
                query,
            )
            self.assertIn('IsWorkflowRoot=IsRunTagged and coalesce(tobool(Properties["app.workflow.root"]), false)', query)
            self.assertNotIn('IsNotebookRoot=tostring(Properties["demo.run_id"]) == run_id', query)
        self.assertIn("RootOperations=countif(IsNotebookRoot)", self.queries["interactions"])
        self.assertIn('where IsExecutor and CorrelationState == "correlated"', self.queries["runs_trend"])
        self.assertNotIn('where Name == "sentinel-agent-query"', self.queries["runs_trend"])

    def test_trace_span_ancestry_preserves_cross_resource_parents_and_is_bounded(self):
        query = self.queries["coverage"]
        self.assertIn('NodeKey=strcat(OperationId, "/", Id)', query)
        self.assertIn('strcat(OperationId, "/", ParentId)', query)
        self.assertIn("make-graph NodeKey --> ParentKey with graph_nodes on NodeKey", query)
        self.assertIn("graph-match cycles=none (child)-[parents*1..64]->(ancestor)", query)
        self.assertIn("AncestorKey=NodeKey, Depth=tolong(0)", query)
        self.assertIn("Depth=tolong(array_length(ParentPath))", query)
        self.assertIn("summarize Depth=min(Depth) by NodeKey, AncestorKey", query)
        self.assertNotIn('NodeKey=strcat(_ResourceId', query)

    def test_multiple_stages_share_a_trace_without_single_label_attribution(self):
        query = self.queries["coverage"]
        self.assertNotIn("let run_context", query)
        self.assertNotIn("arg_max(TimeGenerated, Properties)", query)
        self.assertNotIn("join kind=leftouter (run_context) on OperationId", query)
        self.assertIn("InteractionLabels=make_set_if(AncestorInteraction", query)
        self.assertIn("RootInteractions=make_set_if(AncestorInteraction, AncestorIsRoot", query)
        self.assertIn("join kind=leftouter (ancestry_context) on NodeKey", query)

    def test_nearest_metadata_and_all_left_join_mappings_are_collapsed(self):
        query = self.queries["end_to_end"]
        self.assertIn("summarize ParentKeys=make_set(ParentKey) by NodeKey", query)
        self.assertIn("AncestryConflicts=countif(AncestorConflict)\n    by NodeKey", query)
        self.assertIn("summarize MetadataValues=make_set(MetadataValue) by NodeKey, MetadataKey, Depth", query)
        self.assertIn("summarize arg_min(Depth, MetadataValues) by NodeKey, MetadataKey", query)
        self.assertIn("AmbiguousMetadata=countif(array_length(MetadataValues) > 1) by NodeKey", query)
        self.assertIn("join kind=leftouter (nearest_metadata) on NodeKey", query)
        self.assertIn('CorrelationState == "correlated", tostring(NearestMetadata.Agent)', query)

    def test_content_index_never_joins_on_trace_alone(self):
        query = self.queries["content"]
        self.assertIn("let content_context = correlated_spans\n| project _ResourceId, TraceId=OperationId, SpanId=Id", query)
        self.assertIn("join kind=leftouter (content_context) on _ResourceId, TraceId, SpanId", query)
        self.assertIn("join kind=leftouter (instruction_messages) on _ResourceId, TraceId, SpanId, Id", query)
        self.assertIn('CorrelationState=coalesce(CorrelationState, "unmatched span")', query)
        self.assertNotIn("on $left.TraceId == $right.OperationId", query)

    def test_unmapped_and_contradictory_critical_spans_fail_explicitly(self):
        query = self.queries["coverage"]
        for field in ("UnmappedCriticalSpans", "AmbiguousCriticalSpans", "UncorrelatedWorkflowSpans"):
            self.assertIn(f"{field}=countif(", query)
        for predicate in (
            "array_length(StageRootKeys) > 1", "array_length(InteractionLabels) > 1",
            "array_length(StepLabels) > 1", "array_length(WorkflowLabels) > 1",
            "array_length(NativeWorkflowIds) > 1", "AncestryConflicts > 0",
        ):
            self.assertIn(predicate, query)
        self.assertIn('IsCriticalSpan and CorrelationState != "correlated"', self.queries["failures"])
        self.assertIn('"unmapped / critical"', query)
        self.assertIn('or Name == "responses"', query)
        self.assertIn("tostring(RootInteractions[0]) != tostring(RootSteps[0])", query)

    def test_workflow_view_uses_native_wall_clock_and_unique_summary_keys(self):
        query = self.queries["workflows"]
        self.assertIn("where IsWorkflowRun\n| join kind=leftouter (executor_summary) on WorkflowRunKey", query)
        self.assertIn("ExecutorSpanIds=make_set(Id) by WorkflowRunKey", query)
        self.assertIn('AmbiguousSpans=countif(CorrelationState == "ambiguous") by WorkflowRunKey', query)
        self.assertIn("NativeDurationMs=DurationMs", query)
        self.assertNotIn("sum(DurationMs)", query)
        self.assertNotIn("sumif(DurationMs", query)
        self.assertIn("take 200", query)
        coverage = self.queries["coverage"]
        self.assertIn("WorkflowNames=make_set_if(WorkflowName, IsWorkflowRun", coverage)
        self.assertIn('WorkflowSteps=make_set_if(strcat(WorkflowName, "/", WorkflowStep)', coverage)
        self.assertIn('IsNotebookRoot and IsExecutor and CorrelationState == "correlated"', coverage)

    def test_previews_are_opt_in_and_size_limited(self):
        self.assertNotIn("InputPreview=", self.queries["content"])
        visible = build_observability_queries(RUN_ID, include_content=True)["content"]
        for field in ("InputMessages", "OutputMessages", "Instructions", "ToolDefinitions", "ToolCallArguments", "ToolCallResult"):
            self.assertIn(f"substring({field}, 0, 1200)", visible)
        self.assertIn("take 200", visible)
        self.assertNotIn("take 200", self.queries["coverage"])
        self.assertNotIn("take 200", self.queries["content_coverage"])

    def test_structured_instructions_and_conversations_use_dedicated_table(self):
        query = self.queries["content"]
        self.assertIn('tostring(Attributes["gen_ai.conversation.id"])', query)
        self.assertIn('tostring(Message.role) in ("system", "developer")', query)
        self.assertIn("join kind=leftouter (instruction_messages)", query)
        self.assertIn('case(isnotempty(SystemInstructions), SystemInstructions', query)
        for generated in self.queries.values():
            for legacy in ("gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions", "app.prompt", "app.completion"):
                self.assertNotIn(f'Properties["{legacy}"]', generated)

    def test_empty_or_invalid_message_arrays_do_not_count_as_content(self):
        query = self.queries["content_coverage"]
        self.assertIn('WithInput=countif(InputState == "recorded")', query)
        self.assertIn('WithOutput=countif(OutputState == "recorded")', query)
        self.assertIn('array_length(parse_json(InputMessages)) == 0, "empty array"', query)
        self.assertIn("InvalidMessageRecords=sum(InvalidMessages)", query)

    def test_notebook_uses_report_and_retains_service_and_flush_gates(self):
        notebook = json.loads((Path(__file__).resolve().parents[1] / "zolab-ai-agent-demo-win11.ipynb").read_text(encoding="utf-8"))
        source = "".join(next(c["source"] for c in notebook["cells"] if c["id"] == "6e3dcab6"))
        assignments = [
            node.value for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "SHOW_GENAI_CONTENT" for target in node.targets)
        ]
        self.assertEqual(len(assignments), 1)
        self.assertIsInstance(assignments[0], ast.Constant)
        self.assertIs(type(assignments[0].value), bool)
        self.assertIn("include_content=SHOW_GENAI_CONTENT", source)
        self.assertIn("show_content=SHOW_GENAI_CONTENT", source)
        self.assertIn("content_recording_enabled=", source)
        self.assertIn("display(HTML(observability_html))", source)
        self.assertIn("render_failure_report(", source)
        self.assertIn("force_flush(timeout_millis=30000)", source)
        self.assertIn('os.environ["OTEL_SERVICE_VERSION"] not in coverage["Versions"]', source)
        self.assertNotIn("[:4000]", source)


class CoverageTests(unittest.TestCase):
    def test_complete_coverage_passes(self):
        self.assertEqual(coverage_issues(passing_coverage(), EXPECTED), [])

    def test_every_health_requirement_is_preserved(self):
        for key, value, message in (
            ("Spans", 0, "No current-run"), ("Failures", 4, "4 failed spans"),
            ("AmbiguousSpans", 1, "ambiguous"), ("Interactions", [], "interaction spans"),
            ("ResponseInteractions", [], "Responses dependencies"), ("GenAiSpans", 0, "GenAI chat"),
            ("PersistenceSpans", 0, "persist_story"), ("PersistenceSpans", 2, "persist_story"),
        ):
            with self.subTest(key=key, value=value):
                coverage = passing_coverage()
                coverage[key] = value
                self.assertIn(message, " ".join(coverage_issues(coverage, EXPECTED)))

    def test_sentinel_is_not_required_if_unconfigured(self):
        coverage = passing_coverage()
        coverage["Interactions"] = coverage["ResponseInteractions"] = ["story", "facts"]
        coverage.update(
            WorkflowRuns=1, WorkflowRootSpans=1, ExecutorSpans=3,
            WorkflowNames=["story-facts"], WorkflowIds=["main-id"],
            WorkflowSteps=["story-facts/story", "story-facts/facts", "story-facts/persistence"],
        )
        self.assertEqual(coverage_issues(coverage, {"story", "facts"}), [])

    def test_shared_trace_stage_coverage_is_valid(self):
        coverage = passing_coverage()
        coverage["Interactions"] = ["story", "facts", "persistence", "sentinel"]
        self.assertEqual(coverage_issues(coverage, EXPECTED), [])

    def test_missing_native_workflows_executors_and_metadata_fail(self):
        for key, value, message in (
            ("WorkflowRuns", 0, "workflow.run"),
            ("WorkflowRootSpans", 0, "notebook.workflow"),
            ("WorkflowNames", [], "story-facts"),
            ("WorkflowNames", ["story-facts"], "sentinel"),
            ("WorkflowIds", [], "workflow IDs"),
            ("WorkflowSteps", [], "story-facts/persistence"),
            ("WorkflowSteps", ["story", "facts", "persistence", "sentinel"], "executor steps"),
            ("ExecutorSpans", 0, "executor roots"),
            ("WorkflowFailures", 1, "failed workflow/executor"),
            ("UncorrelatedWorkflowSpans", 1, "parent correlation"),
        ):
            with self.subTest(key=key, value=value):
                coverage = passing_coverage()
                coverage[key] = value
                self.assertIn(message, " ".join(coverage_issues(coverage, EXPECTED)))

    def test_complete_interaction_coverage_cannot_hide_bad_critical_ancestry(self):
        for key, message in (
            ("UnmappedCriticalSpans", "no correlated executor ancestor"),
            ("AmbiguousCriticalSpans", "ambiguous ancestry"),
        ):
            with self.subTest(key=key):
                coverage = passing_coverage()
                coverage[key] = 1
                self.assertIn(message, " ".join(coverage_issues(coverage, EXPECTED)))

    def test_malformed_query_result_is_not_success(self):
        with self.assertRaises(KeyError):
            coverage_issues({}, EXPECTED)


class ReportTests(unittest.TestCase):
    def render(self, results=None, **kwargs):
        return render_observability_report(
            RUN_ID, "workspace", passing_coverage(), results or report_results(),
            build_observability_queries(RUN_ID), EXPECTED,
            content_recording_enabled=kwargs.pop("content_recording_enabled", True), **kwargs,
        )

    def test_report_distinguishes_spans_content_and_calls(self):
        report = self.render()
        for text in ("64", "34", "30", "AVAILABLE", "not a call count", "not nested", "KQL: coverage", "UTC"):
            self.assertIn(text, report)

    def test_workflow_view_is_not_a_nested_duration_total(self):
        report = self.render()
        for text in (
            "NativeDurationMs", "wall-clock", "story-facts", "sentinel", "ExecutorSteps",
            "app.interaction.root=true", "Sibling story/facts/persistence", "Unmapped / ambiguous critical spans",
        ):
            self.assertIn(text, report)

    def test_workflow_metadata_is_html_escaped(self):
        results = report_results()
        results["workflows"][0]["WorkflowName"] = "<script>workflow</script>"
        report = self.render(results)
        self.assertNotIn("<script>", report)
        self.assertIn("&lt;script&gt;workflow", report)

    def test_display_and_capture_policy_require_explicit_booleans(self):
        for kwargs in ({"show_content": "false"}, {"content_recording_enabled": "true"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(TypeError):
                    self.render(**kwargs)

    def test_empty_content_is_visible_not_a_false_content_pass(self):
        results = report_results()
        results["content_coverage"][0].update(InputInteractions=[], OutputInteractions=[])
        report = self.render(results)
        self.assertIn("WAITING / NOT RECORDED", report)
        self.assertNotIn("AVAILABLE -", report)
        self.assertIn("PASS - span health", report)

    def test_disabled_recording_does_not_require_content(self):
        self.assertIn("Content capture disabled locally", self.render(content_recording_enabled=False))
        with self.assertRaisesRegex(ValueError, "recording policy"):
            self.render(content_recording_enabled=False, show_content=True)

    def test_unmatched_content_is_a_warning(self):
        results = report_results()
        results["content_coverage"][0]["UnmatchedContentRecords"] = 1
        self.assertIn("Some content has no matching dependency span", self.render(results))

    def test_invalid_content_is_reported_explicitly(self):
        results = report_results()
        results["content_coverage"][0]["InvalidMessageRecords"] = 1
        self.assertIn("1 matched content records contain invalid JSON", self.render(results))

    def test_invalid_unmatched_content_is_also_reported(self):
        results = report_results()
        results["content_coverage"][0]["UnmatchedInvalidMessageRecords"] = 1
        self.assertIn("1 unmatched content records contain invalid JSON", self.render(results))

    def test_missing_requested_payload_is_not_silently_rendered_as_empty(self):
        results = report_results()
        results["content"] = [{
            "TimeGenerated": "now", "Interaction": "story", "Operation": "chat",
            "SpanId": "span", "InputCharacters": 12,
        }]
        with self.assertRaises(KeyError):
            self.render(results, show_content=True)

    def test_renderer_also_bounds_preview_size_and_record_count(self):
        results = report_results()
        row = {
            "TimeGenerated": "now", "Interaction": "story", "Operation": "chat", "SpanId": "span",
            "InputCharacters": 2000, "OutputCharacters": 0, "InstructionCharacters": 0,
            "ToolDefinitionsCharacters": 0, "ToolArgumentsCharacters": 0, "ToolResultCharacters": 0,
            "InputPreview": "X" * 1200 + "TOO_LONG",
        }
        results["content"] = [row] * 200 + [dict(row, InputPreview="TOO_MANY")]
        report = self.render(results, show_content=True)
        self.assertNotIn("TOO_LONG", report)
        self.assertNotIn("TOO_MANY", report)

    def test_all_dynamic_html_is_escaped_and_payloads_are_hidden_by_default(self):
        unsafe = '<script>alert("unsafe")</script>'
        results = report_results()
        row = {
            "TimeGenerated": "now", "Interaction": unsafe, "Operation": "chat", "SpanId": "span",
            "InputCharacters": 2000, "OutputCharacters": 0, "InstructionCharacters": 0,
            "ToolDefinitionsCharacters": 0, "ToolArgumentsCharacters": 0, "ToolResultCharacters": 0,
            "InputPreview": unsafe + "PRIVATE_PAYLOAD",
        }
        results["content"] = [row]
        hidden = self.render(results)
        self.assertNotIn("PRIVATE_PAYLOAD", hidden)
        self.assertNotIn("<script>", hidden)
        visible = self.render(results, show_content=True)
        self.assertIn("PRIVATE_PAYLOAD", visible)
        self.assertNotIn("<script>", visible)
        self.assertIn("&lt;script&gt;", visible)
        self.assertIn("first 1200 of 2000 characters; truncated", visible)
        self.assertIn("saved in notebook outputs", visible)

    def test_failure_report_groups_the_cause_without_hiding_failed_attempts(self):
        report = render_failure_report(RUN_ID, ["4 failed spans across 1 operations."], [], [
            {"ExceptionType": "BadRequestError", "Message": "<403 Forbidden>", "OperationId": "trace"},
        ])
        self.assertIn("FAIL", report)
        self.assertIn("earlier", report)
        self.assertIn("&lt;403 Forbidden&gt;", report)
        self.assertNotIn("<403 Forbidden>", report)

    def test_failing_coverage_cannot_render_a_pass(self):
        for field, value in (
            ("Failures", 4), ("WorkflowSteps", []), ("UnmappedCriticalSpans", 1),
            ("AmbiguousCriticalSpans", 1), ("UncorrelatedWorkflowSpans", 1),
        ):
            with self.subTest(field=field):
                coverage = passing_coverage()
                coverage[field] = value
                with self.assertRaisesRegex(ValueError, "Cannot render a passing"):
                    render_observability_report(
                        RUN_ID, "workspace", coverage, report_results(), {}, EXPECTED,
                        content_recording_enabled=True,
                    )


if __name__ == "__main__":
    unittest.main()
