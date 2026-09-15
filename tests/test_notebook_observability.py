import json
import unittest
from pathlib import Path

from notebook_observability import (
    build_observability_queries, coverage_issues, render_failure_report,
    render_observability_report,
)


RUN_ID = "00000000-0000-0000-0000-000000000001"
EXPECTED = {"story", "facts", "sentinel"}


def passing_coverage():
    return {
        "Spans": 64, "Failures": 0, "FailedOperations": 0, "AmbiguousSpans": 0,
        "Interactions": list(EXPECTED), "ResponseInteractions": list(EXPECTED),
        "ResponseDependencies": 8, "GenAiSpans": 13, "PersistenceSpans": 1,
        "Roles": ["client", "responsesapi"], "Versions": ["2026.09.15"],
    }


def report_results():
    return {
        "content_coverage": [{
            "Spans": 64, "SpansWithContent": 34, "SpansWithoutContent": 30,
            "ContentRecords": 34, "ScopedContentRecords": 34, "UnmatchedContentRecords": 0,
            "RecordsWithInput": 29, "RecordsWithOutput": 29, "RecordsWithInstructions": 18,
            "InvalidMessageRecords": 0,
            "InputInteractions": list(EXPECTED), "OutputInteractions": list(EXPECTED),
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
            "coverage", "interactions", "runs_trend", "end_to_end",
            "content_coverage", "content", "failures", "exceptions",
        })
        for query in self.queries.values():
            self.assertIn(f'let run_id = "{RUN_ID}";', query)
            self.assertIn('Properties["demo.run_id"]', query)
            self.assertIn("ago(6h)", query)

    def test_health_and_trends_do_not_depend_on_content(self):
        for name in ("coverage", "interactions", "runs_trend", "failures", "exceptions"):
            self.assertNotIn("AppGenAIContent", self.queries[name])
        self.assertIn('where IsNotebookRoot and RootInteraction in ("story", "facts", "sentinel")', self.queries["runs_trend"])

    def test_join_cannot_multiply_metrics_or_drop_spans_without_content(self):
        query = self.queries["end_to_end"]
        self.assertIn("arg_max(TimeGenerated, *) by _ResourceId, OperationId, Id", query)
        self.assertIn("arg_max(TimeGenerated, *) by _ResourceId, Id", query)
        self.assertIn("by _ResourceId, TraceId, SpanId;", query)
        self.assertIn("join kind=leftouter (content_by_span)", query)
        self.assertIn("on _ResourceId, $left.OperationId == $right.TraceId, $left.Id == $right.SpanId", query)
        self.assertIn("AmbiguousSpans=countif", self.queries["coverage"])

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
        self.assertIn("SHOW_GENAI_CONTENT = False", source)
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
        self.assertEqual(coverage_issues(coverage, {"story", "facts"}), [])

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

    def test_missing_requested_payload_is_not_silently_rendered_as_empty(self):
        results = report_results()
        results["content"] = [{
            "TimeGenerated": "now", "Interaction": "story", "Operation": "chat",
            "SpanId": "span", "InputCharacters": 12,
        }]
        with self.assertRaises(KeyError):
            self.render(results, show_content=True)

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
        coverage = passing_coverage()
        coverage["Failures"] = 4
        with self.assertRaisesRegex(ValueError, "Cannot render a passing"):
            render_observability_report(
                RUN_ID, "workspace", coverage, report_results(), {}, EXPECTED,
                content_recording_enabled=True,
            )


if __name__ == "__main__":
    unittest.main()
