"""Tool-content observations use returned MCP items and the existing trace pipeline."""

import ast
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlparse

from openai.types.responses import Response
from openai.types.responses.response_output_item import McpApprovalRequest, McpCall, McpListTools
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode

import test_notebook_validation as validation_tests
from test_notebook_observability import passing_coverage, report_results
from notebook_support.observability import (
    build_observability_queries, render_failure_report, render_observability_report,
)
from notebook_support.response_observability import (
    get_tool_content_recording_policy, record_response_observability,
)


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "zolab-ai-agent-demo-linux.ipynb"
RUN_ID = "00000000-0000-0000-0000-000000000001"


def tool_call(**changes):
    return McpCall.model_validate({
        "id": "call-1", "type": "mcp_call", "name": "query_lake",
        "server_label": "sentinel", "arguments": '{"query":"DemoTable | take 1"}',
        "output": '{"rows":[{"message":"demo result"}]}', "status": "completed",
    } | changes)


def tool_response(items):
    return Response.model_construct(
        id="response-1", model="demo-model", status="completed", output=items, usage=None,
    )


class ToolContentPolicyTests(unittest.TestCase):
    def test_explicit_opt_in_and_master_content_policy(self):
        for raw, requested in (("1", True), (" TRUE ", True), ("0", False), ("false", False)):
            for master in (True, False):
                with self.subTest(raw=raw, master=master):
                    self.assertEqual(
                        get_tool_content_recording_policy(
                            content_enabled=master, environment={"OTEL_LOG_TOOL_CONTENT": raw},
                        ),
                        requested and master,
                    )
        self.assertFalse(get_tool_content_recording_policy(content_enabled=True, environment={}))

    def test_invalid_policy_values_fail_explicitly(self):
        for value in ("", "yes", "on", "2"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "OTEL_LOG_TOOL_CONTENT"):
                get_tool_content_recording_policy(
                    content_enabled=True, environment={"OTEL_LOG_TOOL_CONTENT": value},
                )
        with self.assertRaises(TypeError):
            get_tool_content_recording_policy(content_enabled="true", environment={})  # pyright: ignore[reportArgumentType]


class LinuxTelemetryPolicyTests(validation_tests.TelemetryPolicyTests):
    def setUp(self):
        with patch.object(validation_tests, "NOTEBOOK", NOTEBOOK):
            super().setUp()
        self.scope["get_tool_content_recording_policy"] = get_tool_content_recording_policy

    def test_tool_policy_change_requires_restart_without_a_second_provider(self):
        os.environ["OTEL_LOG_TOOL_CONTENT"] = "1"
        self.initialize()
        self.assertTrue(self.scope["_notebook_telemetry_state"][3])
        os.environ["OTEL_LOG_TOOL_CONTENT"] = "0"
        with self.assertRaisesRegex(RuntimeError, "Restart the kernel"):
            self.initialize()
        self.configure.assert_called_once()

    def test_invalid_tool_flag_fails_before_instrumentation(self):
        os.environ["OTEL_LOG_TOOL_CONTENT"] = "invalid"
        with self.assertRaisesRegex(ValueError, "OTEL_LOG_TOOL_CONTENT"):
            self.initialize()
        self.configure.assert_not_called()
        self.enable_maf.assert_not_called()

    def test_master_opt_out_disables_tool_content(self):
        os.environ["OTEL_LOG_TOOL_CONTENT"] = "1"
        os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "false"
        self.assertFalse(self.initialize())
        self.assertFalse(self.scope["_notebook_telemetry_state"][3])

    def test_linux_notebook_enables_the_demo_flag_without_overwriting_an_opt_out(self):
        notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        source = "".join(next(cell["source"] for cell in notebook["cells"] if cell["id"] == "3c78effc"))
        statement = next(
            node for node in ast.parse(source).body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "setdefault"
            and node.value.args and isinstance(node.value.args[0], ast.Constant)
            and node.value.args[0].value == "OTEL_LOG_TOOL_CONTENT"
        )
        code = compile(ast.Module(body=[statement], type_ignores=[]), str(NOTEBOOK), "exec")
        exec(code, {"os": os})
        self.assertEqual(os.environ["OTEL_LOG_TOOL_CONTENT"], "1")
        os.environ["OTEL_LOG_TOOL_CONTENT"] = "0"
        exec(code, {"os": os})
        self.assertEqual(os.environ["OTEL_LOG_TOOL_CONTENT"], "0")


class ToolContentCaptureTests(unittest.TestCase):
    def setUp(self):
        self.provider = TracerProvider()
        self.exporter = InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("tool-content-test")
        self.addCleanup(self.provider.shutdown)
        patcher = patch("notebook_support.response_observability.trace.get_tracer", return_value=self.tracer)
        patcher.start()
        self.addCleanup(patcher.stop)

    def capture(self, items, *, tool_policy: bool | None = True, master: bool = True):
        response = tool_response(items)
        before = response.model_dump(mode="json")
        with self.tracer.start_as_current_span("existing Responses request") as parent:
            record_response_observability(
                parent, response, deployment="demo-deployment", capture_content=master,
                capture_tool_content=tool_policy, conversation_id="conversation-1",
            )
        self.assertEqual(response.model_dump(mode="json"), before)
        spans = self.exporter.get_finished_spans()
        return spans[-1], [span for span in spans if span.name.startswith("notebook.mcp.observe ")]

    def test_returned_arguments_and_result_have_exact_parent_and_identifiers(self):
        item = tool_call()
        parent, observations = self.capture([item])
        self.assertEqual(len(observations), 1)
        observation = observations[0]
        assert observation.parent is not None
        assert observation.context is not None
        assert parent.context is not None
        assert parent.attributes is not None
        self.assertEqual(observation.parent.span_id, parent.context.span_id)
        self.assertEqual(observation.context.trace_id, parent.context.trace_id)
        self.assertEqual(observation.kind, SpanKind.INTERNAL)
        attributes = observation.attributes
        assert attributes is not None
        self.assertEqual(attributes["gen_ai.operation.name"], "observe_tool")
        self.assertEqual(attributes["gen_ai.tool.call.arguments"], item.arguments)
        self.assertEqual(attributes["gen_ai.tool.call.result"], item.output)
        self.assertEqual(attributes["gen_ai.tool.call.id"], "call-1")
        self.assertEqual(attributes["gen_ai.conversation.id"], "conversation-1")
        self.assertEqual(attributes["gen_ai.response.id"], "response-1")
        self.assertEqual(attributes["app.mcp.server"], "sentinel")
        self.assertTrue(attributes["app.tool.observation"])
        self.assertEqual(attributes["app.tool.source"], "responses.output")
        self.assertEqual(attributes["app.tool.timing"], "client observation; remote duration unavailable")
        self.assertNotIn("app.usage.source", attributes)
        self.assertNotIn("gen_ai.usage.input_tokens", attributes)
        self.assertEqual(parent.attributes["app.tool.content.items"], 1)
        self.assertEqual(parent.attributes["app.mcp.tool_calls"], 1)

    def test_explicit_parent_is_used_even_if_another_span_is_current(self):
        with self.tracer.start_as_current_span("response parent") as parent:
            with self.tracer.start_as_current_span("unrelated child") as other:
                record_response_observability(
                    parent, tool_response([tool_call()]), deployment="demo", capture_content=True,
                    capture_tool_content=True,
                )
                self.assertEqual(trace.get_current_span().get_span_context(), other.get_span_context())
        observation = self.exporter.get_finished_spans()[0]
        assert observation.parent is not None
        self.assertEqual(observation.parent.span_id, parent.get_span_context().span_id)

    def test_approval_and_discovery_are_not_claimed_as_tool_executions(self):
        approval = McpApprovalRequest(
            id="approval-1", type="mcp_approval_request", name="search", server_label="learn",
            arguments='{"query":"Azure docs"}',
        )
        discovery = McpListTools.model_validate({
            "id": "list-1", "type": "mcp_list_tools", "server_label": "learn",
            "tools": [{"name": "search", "input_schema": {"type": "object"}, "description": "Search docs"}],
        })
        _, observations = self.capture([approval, discovery])
        self.assertEqual(len(observations), 2)
        first, second = [span.attributes for span in observations]
        assert first is not None and second is not None
        self.assertEqual(first["app.mcp.approval.id"], "approval-1")
        self.assertNotIn("gen_ai.tool.call.id", first)
        self.assertFalse(first["app.tool.result.present"])
        self.assertNotIn("gen_ai.tool.call.result", first)
        definitions = second["gen_ai.tool.definitions"]
        assert isinstance(definitions, str)
        self.assertEqual(json.loads(definitions)[0]["name"], "search")
        self.assertFalse(second["app.tool.arguments.present"])
        self.assertNotIn("gen_ai.tool.name", second)

    def test_missing_and_empty_results_remain_distinct(self):
        _, observations = self.capture([
            tool_call(id="missing", output=None), tool_call(id="empty", output=""),
        ])
        missing, empty = [span.attributes for span in observations]
        assert missing is not None and empty is not None
        self.assertFalse(missing["app.tool.result.present"])
        self.assertNotIn("gen_ai.tool.call.result", missing)
        self.assertTrue(empty["app.tool.result.present"])
        self.assertEqual(empty["gen_ai.tool.call.result"], "")
        self.assertEqual(empty["app.tool.result.characters"], 0)

    def test_remote_failure_is_recorded_without_changing_local_span_outcome(self):
        parent, observations = self.capture([tool_call(output=None, status="failed")])
        observation = observations[0]
        assert observation.attributes is not None
        assert parent.attributes is not None
        self.assertEqual(observation.attributes["app.mcp.tool.status"], "failed")
        self.assertTrue(observation.attributes["app.tool.returned_error"])
        self.assertEqual(parent.attributes["app.mcp.tool_errors"], 1)
        self.assertEqual(parent.status.status_code, StatusCode.UNSET)
        self.assertEqual(observation.status.status_code, StatusCode.UNSET)
        self.assertEqual(parent.events[0].name, "mcp.tool.error")

    def test_disabled_or_unconfigured_capture_adds_no_tool_payload_spans(self):
        for tool_policy, master in ((False, True), (True, False), (None, True)):
            self.exporter.clear()
            with self.subTest(tool_policy=tool_policy, master=master):
                parent, observations = self.capture([tool_call()], tool_policy=tool_policy, master=master)
                assert parent.attributes is not None
                self.assertEqual(observations, [])
                self.assertNotIn("gen_ai.tool.call.arguments", parent.attributes)
                if tool_policy is None:
                    self.assertNotIn("app.tool.content.enabled", parent.attributes)
                else:
                    self.assertFalse(parent.attributes["app.tool.content.enabled"])
                    self.assertEqual(parent.attributes["app.tool.content.state"], "disabled")

    def test_missing_output_and_no_returned_tools_have_explicit_different_states(self):
        for items, expected in ((None, "response output not reported"), ([], "no MCP items returned")):
            self.exporter.clear()
            with self.subTest(items=items):
                parent, observations = self.capture(items)
                assert parent.attributes is not None
                self.assertEqual(observations, [])
                self.assertEqual(parent.attributes["app.tool.content.state"], expected)

    def test_invalid_capture_flag_fails_before_writing_metadata(self):
        with self.tracer.start_as_current_span("parent") as parent:
            with self.assertRaisesRegex(TypeError, "capture_tool_content"):
                record_response_observability(
                    parent, tool_response([]), deployment="demo", capture_content=True,
                    capture_tool_content="1",  # pyright: ignore[reportArgumentType]
                )
        attributes = self.exporter.get_finished_spans()[-1].attributes
        assert attributes is not None
        self.assertEqual(dict(attributes), {})

    def test_installed_exporter_preserves_large_tool_strings_up_to_its_genai_limit(self):
        from azure.monitor.opentelemetry.exporter._utils import _filter_custom_properties
        from azure.monitor.opentelemetry.exporter._generated.exporter.models import MessageData
        from azure.monitor.opentelemetry.exporter.export.trace._exporter import _convert_span_events_to_envelopes

        result = "tool-result-" + "x" * (256 * 1024)
        _, observations = self.capture([tool_call(output=result)])
        observation = observations[0]
        assert observation.attributes is not None
        assert observation.context is not None
        self.assertEqual(observation.attributes["gen_ai.tool.call.result"], result)
        self.assertEqual(observation.attributes["app.tool.result.characters"], len(result))
        properties = _filter_custom_properties(observation.attributes)
        self.assertEqual(properties["gen_ai.tool.call.result"], result[:256 * 1024])
        self.assertGreater(len(properties["gen_ai.tool.call.result"]), 8192)
        envelopes = _convert_span_events_to_envelopes(observation)
        self.assertEqual(len(envelopes), 1)
        envelope = envelopes[0]
        assert envelope.data is not None and envelope.tags is not None
        data = envelope.data.base_data
        assert isinstance(data, MessageData)
        assert data.properties is not None
        self.assertEqual(data.message, "mcp.tool.content.observed")
        self.assertEqual(envelope.tags["ai.operation.parentId"], f"{observation.context.span_id:016x}")
        self.assertNotIn("tool-result-", str(data.properties))

    def test_both_notebook_response_paths_capture_every_approval_continuation(self):
        for cell_id in ("2692d274", "ef551c01"):
            with self.subTest(cell=cell_id):
                self.exporter.clear()
                approval = McpApprovalRequest(
                    id="approval-1", type="mcp_approval_request", name="query_lake",
                    server_label="sentinel", arguments="{}",
                )
                client = SimpleNamespace(responses=SimpleNamespace(
                    create=Mock(side_effect=[tool_response([approval]), tool_response([tool_call(approval_request_id="approval-1")])]),
                ))
                namespace = {
                    "tracer": self.tracer, "SpanKind": SpanKind, "Status": Status, "StatusCode": StatusCode,
                    "otel_context": context, "make_baggage_context": lambda _values: context.Context(),
                    "baggage_values": {}, "build_responses_url": lambda _client: "https://example.invalid/openai/v1/responses",
                    "urlparse": urlparse, "conversation": SimpleNamespace(id="conversation-1"),
                    "openai_client": client, "model_name": "demo-model",
                    "main_agent_display_name": "main", "sentinel_agent_display_name": "sentinel",
                    "agent_runtime": None, "agent_reference_payload": {}, "sentinel_agent_reference_payload": {},
                    "response_options": lambda *_args: {}, "content_recording_enabled": True,
                    "tool_content_recording_enabled": True, "record_response_observability": record_response_observability,
                }
                with patch.object(validation_tests, "NOTEBOOK", NOTEBOOK):
                    scope = validation_tests.load_functions(cell_id, ["create_agent_response"], namespace)
                with self.tracer.start_as_current_span("stage"):
                    scope["create_agent_response"]("demo prompt")
                    scope["create_agent_response"]([{"type": "mcp_approval_response", "approval_request_id": "approval-1", "approve": True}])
                spans = self.exporter.get_finished_spans()
                observations = [
                    span for span in spans
                    if span.attributes is not None and span.attributes.get("app.tool.observation")
                ]
                requests = [span for span in spans if span.name == "POST /openai/v1/responses"]
                self.assertEqual(len(observations), 2)
                self.assertEqual(len(requests), 2)
                parents, request_ids, conversations = set(), set(), set()
                for span in observations:
                    assert span.parent is not None and span.attributes is not None
                    parents.add(span.parent.span_id)
                    conversation_id = span.attributes["gen_ai.conversation.id"]
                    assert isinstance(conversation_id, str)
                    conversations.add(conversation_id)
                for span in requests:
                    assert span.context is not None
                    request_ids.add(span.context.span_id)
                self.assertEqual(parents, request_ids)
                self.assertEqual(conversations, {"conversation-1"})
                self.assertEqual(client.responses.create.call_count, 2)
                client.responses.create.side_effect = RuntimeError("test SDK failure")
                with self.assertRaisesRegex(RuntimeError, "test SDK failure"):
                    scope["create_agent_response"]("failing request")
                failed = self.exporter.get_finished_spans()[-1]
                assert failed.attributes is not None
                self.assertEqual(failed.name, "POST /openai/v1/responses")
                self.assertTrue(failed.attributes["app.tool.content.enabled"])
                self.assertEqual(failed.attributes["app.tool.content.state"], "request failed; no response output")
                self.assertEqual(failed.status.status_code, StatusCode.ERROR)
                self.assertNotIn("app.tool.content.items", failed.attributes)


class ToolContentReportTests(unittest.TestCase):
    def results(self):
        result = report_results()
        result["tool_content_coverage"] = [{"Interaction": "facts", "ExpectedItems": 1, "ObservedItems": 1}]
        result["tool_content"] = [{
            "ItemType": "mcp_call", "ToolName": "<script>demo-tool</script>",
            "ArgumentsPreview": "demo-arguments", "ResultPreview": "x" * 2000,
            "DefinitionsPreview": "", "PayloadState": "available",
            "ArgumentsReturned": True, "ResultReturned": True, "DefinitionsReturned": False,
            "ArgumentsCharacters": 14, "ResultCharacters": 2000, "DefinitionsCharacters": None,
        }]
        return result

    def render(self, results=None, *, show_content=False):
        return render_observability_report(
            RUN_ID, "workspace", passing_coverage(), self.results() if results is None else results,
            build_observability_queries(RUN_ID, include_content=show_content, include_tool_content=True),
            {"story", "facts", "sentinel"}, content_recording_enabled=True, show_content=show_content,
        )

    def test_tool_queries_are_opt_in_scoped_and_do_not_count_observations_as_execution(self):
        base = build_observability_queries(RUN_ID)
        queries = build_observability_queries(RUN_ID, include_tool_content=True)
        self.assertEqual(set(queries) - set(base), {"tool_content", "tool_content_coverage"})
        for name in ("tool_content", "tool_content_coverage"):
            self.assertIn(f'let run_id = "{RUN_ID}"', queries[name])
            self.assertIn("ago(6h)", queries[name])
            self.assertIn("where IsToolObservation", queries[name])
            self.assertIn("on _ResourceId, $left.OperationId == $right.TraceId, $left.Id == $right.SpanId", queries[name])
            self.assertIn("arg_max(TimeGenerated, *) by _ResourceId, TraceId, SpanId", queries[name])
            self.assertNotIn("ArgumentsPreview", queries[name])
        self.assertIn("| take 200", queries["tool_content"])
        self.assertNotIn("| take ", queries["tool_content_coverage"])
        self.assertIn('IsToolObservation, "tool observation"', queries["end_to_end"])
        self.assertNotIn("DurationMs", queries["tool_content"].split("tool_observations\n| project")[-1])
        self.assertIn('"empty payload returned"', queries["tool_content"])
        self.assertIn('"incomplete payload"', queries["tool_content"])
        self.assertIn("OutputNotReported=countif(CaptureEnabled == true and isnull(ExpectedItems))", queries["tool_content_coverage"])
        self.assertIn('or isnotnull(tobool(Properties["app.tool.content.enabled"]))', queries["tool_content_coverage"])

    def test_query_and_html_previews_are_opt_in_escaped_and_bounded(self):
        queries = build_observability_queries(RUN_ID, include_content=True, include_tool_content=True)
        self.assertIn("ResultPreview=substring(ToolCallResult, 0, 1200)", queries["tool_content"])
        html = self.render(show_content=True)
        self.assertIn("demo-arguments", html)
        self.assertIn("x" * 1200, html)
        self.assertNotIn("x" * 1201, html)
        self.assertIn("&lt;script&gt;demo-tool&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)
        hidden = self.render()
        self.assertNotIn("demo-arguments", hidden)
        self.assertNotIn("x" * 100, hidden)

    def test_tool_details_are_capped_at_200_rows_but_coverage_is_separate(self):
        results = self.results()
        results["tool_content"] = [
            dict(results["tool_content"][0], ToolName=f"tool-row-{index:03d}")
            for index in range(205)
        ]
        html = self.render(results)
        self.assertIn("tool-row-199", html)
        self.assertNotIn("tool-row-200", html)
        self.assertIn("Coverage uses all observation spans", html)

    def test_empty_and_unavailable_results_have_different_display_labels(self):
        results = self.results()
        row = results["tool_content"][0]
        results["tool_content"] = [
            dict(row, ResultPreview="", ResultReturned=True, ResultCharacters=0),
            dict(row, ResultPreview="", ResultReturned=False, ResultCharacters=None),
        ]
        html = self.render(results, show_content=True)
        self.assertIn("<td>empty returned string</td>", html)
        self.assertIn("<td>not returned</td>", html)

    def test_failure_report_has_the_same_tool_evidence(self):
        html = render_failure_report(
            RUN_ID, ["test failure"], [], [], diagnostics=self.results(),
            content_recording_enabled=True, show_content=True,
        )
        self.assertIn("Tool-content observations", html)
        self.assertIn("demo-arguments", html)
        self.assertIn("not additional tool invocations", html)

    def test_missing_requested_tool_evidence_is_not_a_success_fallback(self):
        results = self.results()
        del results["tool_content"]
        with self.assertRaises(KeyError):
            self.render(results)
        with self.assertRaises(TypeError):
            build_observability_queries(RUN_ID, include_tool_content="1")  # pyright: ignore[reportArgumentType]


if __name__ == "__main__":
    unittest.main()
