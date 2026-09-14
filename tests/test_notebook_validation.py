import ast
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode


NOTEBOOK = Path(__file__).resolve().parents[1] / "zolab-ai-agent-demo-win11.ipynb"


def load_functions(cell_id, names, namespace=None):
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    cell = next(cell for cell in notebook["cells"] if cell["id"] == cell_id)
    tree = ast.parse("".join(cell["source"]))
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if {node.name for node in functions} != set(names):
        raise AssertionError("A notebook helper is missing.")
    scope = {"json": json, **(namespace or {})}
    # Load trusted, selected definitions only; never execute the notebook's setup/API cells.
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(NOTEBOOK), "exec"), scope)  # pylint: disable=exec-used
    return scope


class RecordingSpan:
    def __init__(self):
        self.attributes = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def set_attribute(self, name, value):
        self.attributes[name] = value

    def add_event(self, *args):
        pass

    def record_exception(self, error):
        pass

    def set_status(self, status):
        pass


def response(text="", approvals=(), status="completed"):
    return SimpleNamespace(
        id="response-test",
        status=status,
        output_text=text,
        output=[SimpleNamespace(type="mcp_approval_request", id=value) for value in approvals],
        error=None,
        model="test-model",
    )


class ResponseValidationTests(unittest.TestCase):
    def setUp(self):
        self.span = RecordingSpan()
        namespace = {
            "run_id": "test-run", "session_id": "test-session",
            "main_agent_display_name": "test-agent", "main_agent_id": "agent:1",
            "main_agent_version": "1", "model_name": "test-model",
            "project_name_value": "test-project", "main_tool_labels": ["msft-learn"],
            "main_agent_reference_payload": {}, "conversation_ids": {},
            "content_recording_enabled": False, "MAX_APPROVAL_ROUNDS": 1,
            "make_baggage_context": lambda values: None,
            "build_responses_url": lambda client: "https://example.invalid/openai/v1/responses",
            "urlparse": urlparse,
            "otel_context": SimpleNamespace(attach=lambda context: None, detach=lambda token: None),
            "tracer": SimpleNamespace(start_as_current_span=lambda *args, **kwargs: self.span),
            "SpanKind": SpanKind, "Status": Status, "StatusCode": StatusCode,
        }
        self.scope = load_functions(
            "2692d274",
            ["parse_response", "run_query_with_auto_approval", "run_interaction_with_span"],
            namespace,
        )

    def run_responses(self, responses):
        pending = iter(responses)
        client = SimpleNamespace(
            conversations=SimpleNamespace(create=lambda: SimpleNamespace(id="conversation-test")),
            responses=SimpleNamespace(create=lambda **kwargs: next(pending)),
        )
        return self.scope["run_interaction_with_span"](
            openai_client=client, interaction_name="facts", prompt="private prompt"
        )

    def test_final_allowed_approval_response_is_parsed(self):
        final, text, conversation = self.run_responses([
            response(approvals=["approval-1"]), response("grounded answer"),
        ])
        self.assertEqual(text, "grounded answer")
        self.assertEqual(conversation, "conversation-test")
        self.assertEqual(final.status, "completed")

    def test_exhausted_approvals_raise(self):
        with self.assertRaisesRegex(RuntimeError, "exceeded 1"):
            self.run_responses([response(approvals=["one"]), response(approvals=["two"])])

    def test_empty_response_raises(self):
        with self.assertRaisesRegex(RuntimeError, "no assistant text"):
            self.run_responses([response()])

    def test_failed_response_with_text_still_raises(self):
        with self.assertRaisesRegex(RuntimeError, "did not complete"):
            self.run_responses([response("not a success", status="failed")])

    def test_content_is_not_recorded_by_default(self):
        self.run_responses([response("private completion")])
        self.assertNotIn("app.prompt", self.span.attributes)
        self.assertNotIn("app.completion", self.span.attributes)
        self.assertEqual(self.span.attributes["demo.run_id"], "test-run")

    def test_content_can_be_explicitly_enabled(self):
        self.scope["content_recording_enabled"] = True
        self.run_responses([response("private completion")])
        self.assertEqual(self.span.attributes["app.prompt"], "private prompt")
        self.assertEqual(self.span.attributes["app.completion"], "private completion")


class TelemetryResultTests(unittest.TestCase):
    def setUp(self):
        self.parse = load_functions("6e3dcab6", ["log_query_rows"])["log_query_rows"]

    def test_dynamic_columns_are_decoded(self):
        result = {"tables": [{"columns": [
            {"name": "Interactions", "type": "dynamic"},
            {"name": "Spans", "type": "long"},
        ], "rows": [['["story","facts","sentinel"]', 33]]}]}
        self.assertEqual(self.parse(result), [
            {"Interactions": ["story", "facts", "sentinel"], "Spans": 33}
        ])

    def test_native_dynamic_values_are_preserved(self):
        result = {"tables": [{"columns": [{"name": "Roles", "type": "dynamic"}],
                              "rows": [[["demo"]]]}]}
        self.assertEqual(self.parse(result), [{"Roles": ["demo"]}])

    def test_partial_results_are_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "partial"):
            self.parse({"error": {"code": "PartialError"}, "tables": []})

    def test_missing_table_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "no result table"):
            self.parse({})

    def test_row_shape_mismatch_is_not_silently_truncated(self):
        with self.assertRaises(ValueError):
            self.parse({"tables": [{"columns": [{"name": "Spans", "type": "long"}],
                                    "rows": [[1, 2]]}]})


class SentinelCorrelationTests(unittest.TestCase):
    def test_response_dependency_keeps_sentinel_parent(self):
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("notebook-correlation-test")
        scope = load_functions("ef551c01", ["create_agent_response"], {
            "tracer": tracer, "SpanKind": SpanKind, "Status": Status,
            "StatusCode": StatusCode, "urlparse": urlparse,
            "otel_context": context, "context": context.get_current(),
            "sentinel_agent_display_name": "test-sentinel",
            "sentinel_agent_reference_payload": {},
            "conversation": SimpleNamespace(id="conversation-test"),
            "openai_client": SimpleNamespace(
                responses=SimpleNamespace(create=lambda **kwargs: response("ok")),
            ),
            "build_responses_url": lambda client: "https://example.invalid/openai/v1/responses",
        })
        try:
            with tracer.start_as_current_span("sentinel-agent-query") as parent:
                scope["create_agent_response"]("test prompt")
                self.assertEqual(trace.get_current_span(), parent)
            spans = exporter.get_finished_spans()
            dependency = next(span for span in spans if span.name.endswith("/responses"))
            self.assertIsNotNone(dependency.parent)
            self.assertEqual(dependency.parent.span_id, parent.get_span_context().span_id)
            self.assertEqual(dependency.context.trace_id, parent.get_span_context().trace_id)
        finally:
            provider.shutdown()


if __name__ == "__main__":
    unittest.main()
