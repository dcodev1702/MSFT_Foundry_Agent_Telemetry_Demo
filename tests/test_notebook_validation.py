import ast
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import Mock, patch
from copy import deepcopy
from tempfile import TemporaryDirectory

from azure.ai.projects.models import AgentIdentity, MCPTool, PromptAgentDefinition
from azure.monitor.opentelemetry._utils.configurations import _get_configurations
from opentelemetry import context, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode
from notebook_agent_endpoints import (
    AgentRuntimeConfig, AgentTarget, get_agent_openai_client, get_pinned_agent,
    prepare_backend_agent, response_options, responses_url, sync_agent_version,
)


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
            "agent_runtime": AgentRuntimeConfig(mode="project"), "response_options": response_options,
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

    def test_content_is_not_recorded_when_disabled(self):
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
            "agent_runtime": AgentRuntimeConfig(mode="project"), "response_options": response_options,
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


class SentinelTableRoutingTests(unittest.TestCase):
    def setUp(self):
        notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        self.cells = {cell["id"]: "".join(cell["source"]) for cell in notebook["cells"]}
        self.agent_tree = ast.parse(self.cells["586f0511"])
        assignment = next(
            node for node in self.agent_tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "sentinel_signin_instructions"
                    for target in node.targets)
        )
        self.instructions = ast.literal_eval(assignment.value)

    def test_known_table_interactive_filter_and_latest_event_are_explicit(self):
        self.assertIn("query_lake directly on SigninLogs only", self.instructions)
        self.assertIn("IsInteractive == true", self.instructions)
        self.assertIn("UserPrincipalName case-insensitively", self.instructions)
        self.assertIn("top 1 by TimeGenerated desc", self.instructions)

    def test_interactive_filter_uses_the_signinlogs_boolean(self):
        self.assertIn("IsInteractive (bool)", self.instructions)
        self.assertIn("IsInteractive == true", self.instructions)
        self.assertNotIn("IsInteractive == 'true'", self.instructions)
        self.assertNotIn("LogonType", self.instructions)
        self.assertNotIn("ResultType ==", self.instructions)

    def test_canonical_query_uses_only_signinlogs_columns(self):
        self.assertIn(
            "SigninLogs | where IsInteractive == true "
            "| where UserPrincipalName =~ '<signed-in UPN>' | top 1 by TimeGenerated desc "
            "| project TimeGenerated, UserPrincipalName, IsInteractive, IPAddress, "
            "Location, LocationDetails, AppDisplayName, ResourceDisplayName",
            self.instructions,
        )

    def test_ah_route_is_removed_without_changing_the_mcp_connection(self):
        self.assertNotIn("EntraIdSignInEvents", self.instructions)
        self.assertNotIn("runHuntingQuery", self.instructions)
        self.assertIn(
            'sentinel_mcp_url = "https://sentinel.microsoft.com/mcp/data-exploration"',
            self.cells["377478c3"],
        )
        self.assertIn("project_connection_id=sentinel_project_connection_id", self.cells["377478c3"])

    def test_discovery_and_fallback_table_search_are_prohibited(self):
        self.assertIn("Do not call search_tables", self.instructions)
        self.assertRegex(self.instructions, r"without searching for or substituting another (?:SDL\s*)?table")
        self.assertNotIn("requires it after schema discovery", self.cells["586f0511"])
        self.assertNotIn("column names returned by schema discovery", self.cells["586f0511"])

    def test_schema_and_output_mappings_use_signinlogs_columns(self):
        for name in ("TimeGenerated", "UserPrincipalName", "IsInteractive", "IPAddress",
                     "Location", "LocationDetails", "AppDisplayName", "ResourceDisplayName"):
            with self.subTest(column=name):
                self.assertIn(name, self.instructions)
        for name in ("Timestamp", "AccountUpn", "LogonType"):
            self.assertNotIn(name, self.instructions)
        self.assertIn("LocationDetails.city", self.instructions)
        self.assertIn("LocationDetails.state", self.instructions)
        self.assertIn("LocationDetails.countryOrRegion", self.instructions)
        self.assertIn("do not invent top-level City, State or Country columns", self.instructions)
        self.assertIn("TimeGenerated to Date, UserPrincipalName to UPN", self.instructions)

    def test_system_and_user_prompts_share_the_same_routing_instructions(self):
        for cell_id, target_name in (
            ("586f0511", "sentinel_agent_instructions"),
            ("ef551c01", "sentinel_prompt"),
        ):
            with self.subTest(prompt=target_name):
                tree = ast.parse(self.cells[cell_id])
                assignment = next(
                    node for node in tree.body
                    if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == target_name
                            for target in node.targets)
                )
                self.assertTrue(any(
                    isinstance(node, ast.Name) and node.id == "sentinel_signin_instructions"
                    for node in ast.walk(assignment.value)
                ))


class MarpModelMetadataTests(unittest.TestCase):
    def setUp(self):
        self.scope = load_functions("586f0511", [
            "resolve_model_metadata", "capture_model_metadata", "format_model_footer",
        ])
        self.metadata = {
            "type": "OpenAI", "name": "gpt-5.6-terra",
            "version": "2026-07-09", "deployment": "demo-model-alias",
        }
        self.deployment = SimpleNamespace(
            name="demo-model-alias", model_publisher="OpenAI",
            model_name="gpt-5.6-terra", model_version="2026-07-09",
        )
        self.client = SimpleNamespace(deployments=Mock())
        self.client.deployments.get.return_value = self.deployment

    def test_metadata_uses_underlying_model_not_deployment_alias(self):
        metadata = self.scope["resolve_model_metadata"](self.client, "demo-model-alias")
        self.assertEqual(metadata, self.metadata)
        self.client.deployments.get.assert_called_once_with(name="demo-model-alias")

    def test_missing_metadata_is_not_invented(self):
        for value in (None, "", " ", 37):
            with self.subTest(version=value):
                self.deployment.model_version = value
                with self.assertRaisesRegex(ValueError, "version"):
                    self.scope["resolve_model_metadata"](self.client, "demo-model-alias")

    def test_metadata_lookup_errors_propagate(self):
        self.client.deployments.get.side_effect = RuntimeError("lookup failed")
        with self.assertRaisesRegex(RuntimeError, "lookup failed"):
            self.scope["resolve_model_metadata"](self.client, "demo-model-alias")

    def test_response_alias_name_and_version_are_supported(self):
        for model in ("demo-model-alias", "gpt-5.6-terra", "gpt-5.6-terra-2026-07-09"):
            with self.subTest(response_model=model):
                result = self.scope["capture_model_metadata"](
                    self.metadata, SimpleNamespace(model=model),
                )
                self.assertEqual(result["response_model"], model)
                self.assertEqual(result["version"], "2026-07-09")
                self.assertNotIn("response_model", self.metadata)

    def test_unexpected_response_model_cannot_be_mislabeled(self):
        for model in (None, "", "gpt-5.4", "gpt-5.6-terra-2026-08-01"):
            with self.subTest(response_model=model):
                with self.assertRaisesRegex(ValueError, "model"):
                    self.scope["capture_model_metadata"](self.metadata, SimpleNamespace(model=model))

    def test_footer_escapes_html(self):
        metadata = {**self.metadata, "name": "<model>&"}
        footer = self.scope["format_model_footer"](metadata)
        self.assertIn("&lt;model&gt;&amp;", footer)
        self.assertNotIn("<model>", footer)

    def test_model_metadata_is_persisted_and_passed_to_both_builders(self):
        notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        cells = {cell["id"]: "".join(cell["source"]) for cell in notebook["cells"]}
        for cell_id, metadata_name in (
            ("2692d274", "main_model_metadata"), ("ef551c01", "sentinel_model_metadata"),
        ):
            with self.subTest(cell=cell_id):
                tree = ast.parse(cells[cell_id])
                record = next(
                    node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "story_record"
                            for target in node.targets)
                )
                metadata_value = next(
                    value for key, value in zip(record.keys, record.values)
                    if isinstance(key, ast.Constant) and key.value == "model_metadata"
                )
                self.assertEqual(metadata_value.id, metadata_name)
                call = next(
                    node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == "build_marp_deck"
                )
                argument = next(kw.value for kw in call.keywords if kw.arg == "model_metadata")
                self.assertEqual(argument.id, metadata_name)

    def test_both_decks_show_metadata_without_changing_slide_counts(self):
        metadata = {**self.metadata, "response_model": "gpt-5.6-terra-2026-07-09"}
        common = {
            "Path": Path, "format_model_footer": self.scope["format_model_footer"],
            "generated_at_display": "2026-09-15 03:12:00Z",
            "agent_runtime_label": "responses.create + agent_reference",
            "main_agent_display_name": "main-agent",
            "sentinel_agent_display_name": "sentinel-agent",
            "sentinel_workspace_name": "test-workspace",
            "sentinel_subscription_name": "test-subscription",
            "sentinel_target_upn": "user@example.invalid",
        }
        for cell_id, names, kwargs, slide_count in (
            ("2692d274", ["strip_heading", "normalize_marp_text", "build_marp_deck"],
             {"story_text": "Story.", "facts_text": "MSFT Learn Insights\nFacts.",
              "conversation_map": {"story": "one", "facts": "two"},
              "model_metadata": {"story": metadata, "facts": metadata}}, 4),
            ("ef551c01", ["normalize_marp_text", "build_marp_deck"],
             {"response_text": "Sentinel result.", "conversation_id": "three",
              "model_metadata": metadata}, 3),
        ):
            with self.subTest(cell=cell_id):
                builder = load_functions(cell_id, names, common)["build_marp_deck"]
                deck = builder(
                    **kwargs, story_id=1, marp_path=Path("marp") / "test.md",
                    stories_path=Path("stories.json"),
                )
                footer = json.loads(next(
                    line.removeprefix("footer: ") for line in deck.splitlines()
                    if line.startswith("footer: ")
                ))
                self.assertIn("LLM type/provider: OpenAI", footer)
                self.assertIn("Model name: gpt-5.6-terra", footer)
                self.assertIn("Model version: 2026-07-09", footer)
                self.assertIn("Model deployment: `demo-model-alias`", deck)
                self.assertIn("gpt-5.6-terra-2026-07-09", deck)
                self.assertEqual(deck.count("\n---\n"), slide_count)
                self.assertIn("footer {", deck)


class CreationSpanEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.provider = TracerProvider()
        self.exporter = InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.addCleanup(self.provider.shutdown)
        self.tracer = self.provider.get_tracer("creation-enrichment-test")
        self.metadata = {
            "type": "OpenAI", "name": "gpt-5.6-terra",
            "version": "2026-07-09", "deployment": "deployment-alias",
        }
        self.definition = PromptAgentDefinition(
            model="deployment-alias", instructions="private instructions",
            tools=[MCPTool(server_label="test-tool", server_url="https://example.invalid/mcp")],
        )
        self.scope = load_functions("586f0511", [
            "add_agent_creation_metadata", "record_creation_request_id", "make_creation_response_hook",
        ])

    def fingerprint(self, definition=None, metadata=None):
        with self.tracer.start_as_current_span("create") as span:
            self.scope["add_agent_creation_metadata"](
                span, definition or self.definition, metadata or self.metadata,
            )
        return self.exporter.get_finished_spans()[-1].attributes

    def test_resolved_model_fields_and_digest_are_recorded_without_payload(self):
        attributes = self.fingerprint()
        self.assertEqual(attributes["app.model.name"], "gpt-5.6-terra")
        self.assertEqual(attributes["app.model.version"], "2026-07-09")
        self.assertEqual(attributes["app.model.publisher"], "OpenAI")
        self.assertEqual(attributes["app.model.deployment"], "deployment-alias")
        self.assertRegex(attributes["app.agent.config.sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(attributes["app.azure.request_id_available"])
        self.assertNotIn("private instructions", str(dict(attributes)))

    def test_fingerprint_is_stable_across_object_key_order(self):
        content = self.definition.as_dict()
        reordered = SimpleNamespace(as_dict=lambda: dict(reversed(list(content.items()))))
        self.assertEqual(
            self.fingerprint()["app.agent.config.sha256"],
            self.fingerprint(reordered, dict(reversed(list(self.metadata.items()))))["app.agent.config.sha256"],
        )

    def test_instruction_tool_and_resolved_version_changes_change_digest(self):
        baseline = self.fingerprint()["app.agent.config.sha256"]
        for field, value in (
            ("instructions", "changed instructions"),
            ("tools", [MCPTool(server_label="other", server_url="https://example.invalid/other").as_dict()]),
        ):
            with self.subTest(field=field):
                data = {**self.definition.as_dict(), field: value}
                changed = self.fingerprint(SimpleNamespace(as_dict=lambda: data))
                self.assertNotEqual(changed["app.agent.config.sha256"], baseline)
        changed = self.fingerprint(metadata={**self.metadata, "version": "2026-08-01"})
        self.assertNotEqual(changed["app.agent.config.sha256"], baseline)

    def test_request_id_and_gateway_id_are_allowlisted(self):
        with self.tracer.start_as_current_span("create") as span:
            response = SimpleNamespace(http_response=SimpleNamespace(headers={
                "X-Request-ID": " service-request ",
                "APIM-Request-ID": "gateway-request",
                "Authorization": "must-not-be-exported",
            }))
            self.assertIs(self.scope["make_creation_response_hook"](span)(response), response)
        attributes = self.exporter.get_finished_spans()[-1].attributes
        self.assertEqual(attributes["app.azure.request_id"], "service-request")
        self.assertEqual(attributes["app.azure.apim_request_id"], "gateway-request")
        self.assertEqual(attributes["app.azure.request_id_header"], "x-request-id")
        self.assertTrue(attributes["app.azure.request_id_available"])
        self.assertNotIn("must-not-be-exported", str(dict(attributes)))

    def test_request_id_header_fallbacks(self):
        for header in ("x-ms-request-id", "apim-request-id", "request-id"):
            with self.subTest(header=header):
                with self.tracer.start_as_current_span("create") as span:
                    self.scope["record_creation_request_id"](span, SimpleNamespace(headers={header: "id"}))
                attributes = self.exporter.get_finished_spans()[-1].attributes
                self.assertEqual(attributes["app.azure.request_id"], "id")
                self.assertEqual(attributes["app.azure.request_id_header"], header)

    def test_missing_request_id_is_explicit_not_fabricated(self):
        with self.tracer.start_as_current_span("create") as span:
            self.scope["add_agent_creation_metadata"](span, self.definition, self.metadata)
            self.scope["record_creation_request_id"](span, SimpleNamespace(headers={}))
        exported = self.exporter.get_finished_spans()[-1]
        self.assertFalse(exported.attributes["app.azure.request_id_available"])
        self.assertNotIn("app.azure.request_id", exported.attributes)
        self.assertIn("create_agent.request_id_unavailable", [event.name for event in exported.events])

    def run_creation_block(self, agent_variable, fail=False):
        notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        source = "".join(next(cell["source"] for cell in notebook["cells"] if cell["id"] == "586f0511"))
        block = next(
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.With) and any(
                isinstance(name, ast.Name) and name.id == agent_variable
                for name in ast.walk(node.items[0].context_expr.args[0])
            )
        )
        error = RuntimeError("creation rejected")
        error.response = SimpleNamespace(headers={"x-request-id": "failed-request"})

        def create_version(*, agent_name, definition, raw_response_hook):
            self.assertEqual(definition.model, "deployment-alias")
            if fail:
                raise error
            raw_response_hook(SimpleNamespace(http_response=SimpleNamespace(headers={
                "x-request-id": "successful-request",
            })))
            return SimpleNamespace(id=f"{agent_name}:9", version="9")

        scope = {
            **self.scope, "tracer": self.tracer, "SpanKind": SpanKind,
            "Status": Status, "StatusCode": StatusCode, "PromptAgentDefinition": PromptAgentDefinition,
            "project_client": SimpleNamespace(agents=SimpleNamespace(create_version=create_version)),
            "main_agent_name": "main", "sentinel_agent_name": "sentinel",
            "main_agent_creation_context": None, "sentinel_creation_context": None,
            "main_agent_instructions": "private instructions", "sentinel_agent_instructions": "private instructions",
            "model_name": "deployment-alias", "model_deployment_metadata": self.metadata,
            "main_tool_labels": ["test-tool"], "sentinel_tool_labels": ["test-tool"],
            "mcp_tool_spec": self.definition.tools[0], "sentinel_tool": self.definition.tools[0],
            "content_recording_enabled": False, "demo_run_id": "test-run", "telemetry_session_id": "test-session",
            "agent_runtime": AgentRuntimeConfig(mode="project"), "agent_setup_operation": "create_agent",
        }
        compiled = compile(ast.Module(body=[block], type_ignores=[]), str(NOTEBOOK), "exec")
        if fail:
            with self.assertRaisesRegex(RuntimeError, "creation rejected"):
                exec(compiled, scope)
        else:
            exec(compiled, scope)
        return self.exporter.get_finished_spans()[-1]

    def test_both_existing_creation_spans_receive_metadata_and_hook(self):
        for variable in ("main_agent_name", "sentinel_agent_name"):
            with self.subTest(agent=variable):
                span = self.run_creation_block(variable)
                self.assertEqual(span.kind, SpanKind.CLIENT)
                self.assertEqual(span.attributes["demo.run_id"], "test-run")
                self.assertEqual(span.attributes["app.azure.request_id"], "successful-request")
                self.assertEqual(span.attributes["app.model.version"], "2026-07-09")
                self.assertRegex(span.attributes["app.agent.config.sha256"], r"^[0-9a-f]{64}$")
                self.assertNotIn("private instructions", str(dict(span.attributes)))

    def test_both_creation_failures_keep_request_id_and_error_status(self):
        for variable in ("main_agent_name", "sentinel_agent_name"):
            with self.subTest(agent=variable):
                span = self.run_creation_block(variable, fail=True)
                self.assertEqual(span.status.status_code, StatusCode.ERROR)
                self.assertEqual(span.attributes["app.azure.request_id"], "failed-request")
                self.assertEqual(span.attributes["error.type"], "RuntimeError")


class PersistenceSpanTests(unittest.TestCase):
    def run_persistence(self, fail=False):
        notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        source = "".join(next(cell["source"] for cell in notebook["cells"] if cell["id"] == "2692d274"))
        block = next(
            node for node in ast.parse(source).body
            if isinstance(node, ast.With)
            and node.items[0].context_expr.args
            and isinstance(node.items[0].context_expr.args[0], ast.Constant)
            and node.items[0].context_expr.args[0].value == "persist_story"
        )
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        append = Mock(side_effect=OSError("disk full")) if fail else Mock(return_value=140)
        scope = {
            "tracer": provider.get_tracer("persist-test"), "persist_context": None,
            "agent_runtime": AgentRuntimeConfig(mode="project"),
            "run_id": "test-run", "session_id": "test-session", "main_agent_display_name": "main",
            "main_agent_id": "main:9", "main_agent_version": "9", "model_name": "alias",
            "generated_at_iso": "2026-09-15T00:00:00Z", "main_model_metadata": {},
            "story_prompt": "prompt", "story_text": "story", "facts_text": "facts", "assistant_text": "answer",
            "main_tool_labels": ["learn"], "conversation_id_map": {"story": "a", "facts": "b"},
            "build_info": {"foundry_project_endpoint": "https://example.invalid", "rg": "group"},
            "marp_output_path": Path("marp") / "test.md", "stories_file": Path("stories.json"),
            "append_story": append,
        }
        compiled = compile(ast.Module(body=[block], type_ignores=[]), str(NOTEBOOK), "exec")
        try:
            if fail:
                with self.assertRaisesRegex(OSError, "disk full"):
                    exec(compiled, scope)
            else:
                exec(compiled, scope)
            append.assert_called_once()
            return exporter.get_finished_spans()[0]
        finally:
            provider.shutdown()

    def test_persistence_is_run_queryable_with_its_own_interaction(self):
        span = self.run_persistence()
        self.assertEqual(span.kind, SpanKind.INTERNAL)
        self.assertEqual(span.attributes["demo.run_id"], "test-run")
        self.assertEqual(span.attributes["app.session.id"], "test-session")
        self.assertEqual(span.attributes["app.interaction"], "persistence")
        self.assertEqual(span.attributes["gen_ai.agent.id"], "main:9")
        self.assertEqual(span.attributes["app.story.id"], 140)

    def test_persistence_failure_is_recorded_and_propagated(self):
        span = self.run_persistence(fail=True)
        self.assertEqual(span.status.status_code, StatusCode.ERROR)
        self.assertEqual(span.attributes["demo.run_id"], "test-run")
        self.assertEqual(span.attributes["error.type"], "OSError")
        self.assertIn("exception", [event.name for event in span.events])

    def test_ingestion_gate_waits_for_persistence(self):
        notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        source = "".join(next(cell["source"] for cell in notebook["cells"] if cell["id"] == "6e3dcab6"))
        self.assertIn('PersistenceSpans=countif(Name == "persist_story" and RootInteraction == "persistence")', source)
        self.assertIn('coverage["PersistenceSpans"] == 1', source)
        self.assertNotIn('expected_interactions.add("persistence")', source)


class AgentEndpointRoutingTests(unittest.TestCase):
    def setUp(self):
        self.build = {
            "agent_invocation_mode": "agent_endpoint",
            "backend_agents": {
                "main": {"name": "main-backend", "version": "1"},
                "sentinel": {"name": "sentinel-backend", "version": "1"},
            },
        }
        self.runtime = AgentRuntimeConfig.from_build_info(self.build, {})
        self.definition = PromptAgentDefinition(model="model-alias", instructions="instructions", tools=[])
        self.endpoint_config = {
            "version_selector": {"version_selection_rules": [
                {"type": "FixedRatio", "agent_version": "1", "traffic_percentage": 100}
            ]},
            "protocol_configuration": {"responses": {}},
            "authorization_schemes": [{"type": "Entra"}],
        }
        self.details = SimpleNamespace(
            state="enabled", instance_identity=SimpleNamespace(principal_id="principal", client_id="client"),
            agent_endpoint=SimpleNamespace(as_dict=lambda: self.endpoint_config),
        )
        self.version = SimpleNamespace(version="1", definition=self.definition)
        self.client = Mock()
        self.client.agents.get.return_value = self.details
        self.client.agents.get_version.return_value = self.version

    def test_project_default_and_explicit_rollback_need_no_endpoint_configuration(self):
        self.assertEqual(AgentRuntimeConfig.from_build_info({}, {}).mode, "project")
        self.assertEqual(
            AgentRuntimeConfig.from_build_info(self.build, {"FOUNDRY_AGENT_INVOCATION_MODE": "project"}).mode,
            "project",
        )

    def test_invalid_mode_or_missing_endpoint_targets_fail_explicitly(self):
        for mode in ("", "typo", None, {}):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                AgentRuntimeConfig.from_build_info({"agent_invocation_mode": mode}, {})
        with self.assertRaisesRegex(ValueError, "backend_agents"):
            AgentRuntimeConfig.from_build_info({"agent_invocation_mode": "agent_endpoint"}, {})

    def test_dynamic_versions_invalid_names_and_shared_agents_are_rejected(self):
        for version in ("@latest", "", "0", 1):
            with self.subTest(version=version), self.assertRaises(ValueError):
                AgentRuntimeConfig.from_build_info({
                    **self.build, "backend_agents": {
                        **self.build["backend_agents"], "main": {"name": "main-backend", "version": version},
                    },
                }, {})
        for name in ("../agent", "name/other", "a" * 64, "sentinel-backend"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                AgentRuntimeConfig.from_build_info({
                    **self.build, "backend_agents": {
                        **self.build["backend_agents"], "main": {"name": name, "version": "1"},
                    },
                }, {})

    def test_pinned_agent_is_read_only_and_preserves_response_hook(self):
        hook = Mock()
        result = get_pinned_agent(self.client, self.runtime.target("main"), self.definition, raw_response_hook=hook)
        self.assertIs(result, self.version)
        self.client.agents.get.assert_called_once_with(agent_name="main-backend", raw_response_hook=hook)
        self.client.agents.get_version.assert_called_once_with(
            agent_name="main-backend", agent_version="1", raw_response_hook=hook,
        )
        self.client.agents.create_version.assert_not_called()
        self.client.agents.update_details.assert_not_called()

    def test_missing_identity_or_disabled_agent_blocks_cutover(self):
        self.details.instance_identity = None
        with self.assertRaisesRegex(RuntimeError, "identity"):
            get_pinned_agent(self.client, self.runtime.target("main"), self.definition)
        self.details.state = "disabled"
        with self.assertRaisesRegex(RuntimeError, "enabled"):
            get_pinned_agent(self.client, self.runtime.target("main"), self.definition)

    def test_pin_drift_does_not_silently_promote(self):
        self.endpoint_config["version_selector"]["version_selection_rules"][0]["agent_version"] = "@latest"
        with self.assertRaisesRegex(RuntimeError, "not pinned"):
            get_pinned_agent(self.client, self.runtime.target("main"), self.definition)
        self.client.agents.update_details.assert_not_called()
        self.client.agents.create_version.assert_not_called()

    def test_notebook_edits_require_an_explicit_release(self):
        changed = PromptAgentDefinition(model="model-alias", instructions="changed", tools=[])
        with self.assertRaisesRegex(RuntimeError, "differs from pinned"):
            get_pinned_agent(self.client, self.runtime.target("main"), changed)
        self.client.agents.create_version.assert_not_called()

    def test_missing_responses_or_entra_authorization_is_rejected(self):
        for field in ("protocol_configuration", "authorization_schemes"):
            with self.subTest(field=field):
                original = self.endpoint_config[field]
                self.endpoint_config[field] = {} if field == "protocol_configuration" else []
                with self.assertRaisesRegex(RuntimeError, "Responses protocol"):
                    get_pinned_agent(self.client, self.runtime.target("main"), self.definition)
                self.endpoint_config[field] = original

    def test_client_is_bound_to_configured_agent_only_in_endpoint_mode(self):
        get_agent_openai_client(self.client, self.runtime, "main-backend")
        self.client.get_openai_client.assert_called_once_with(agent_name="main-backend")
        self.client.reset_mock()
        get_agent_openai_client(self.client, AgentRuntimeConfig(mode="project"), "main")
        self.client.get_openai_client.assert_called_once_with()
        with self.assertRaises(ValueError):
            get_agent_openai_client(self.client, self.runtime, "unconfigured-agent")

    def test_endpoint_requests_do_not_send_project_agent_references(self):
        payload = {"agent_reference": {"name": "main"}}
        self.assertEqual(response_options(self.runtime, payload), {})
        self.assertEqual(response_options(AgentRuntimeConfig(mode="project"), payload), {"extra_body": payload})

    def test_both_response_url_shapes_avoid_duplicate_path_segments(self):
        for path in (
            "/api/projects/demo/openai/v1/",
            "/api/projects/demo/agents/main-backend/endpoint/protocols/openai/",
        ):
            with self.subTest(path=path):
                client = SimpleNamespace(base_url="https://example.invalid" + path)
                self.assertEqual(responses_url(client), "https://example.invalid" + path + "responses")
        with self.assertRaises(ValueError):
            responses_url(SimpleNamespace(base_url="https://example.invalid/unrecognized/"))

    def test_both_notebook_cells_and_gate_use_runtime_aware_routing(self):
        notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        cells = {cell["id"]: "".join(cell["source"]) for cell in notebook["cells"]}
        for cell_id in ("2692d274", "ef551c01"):
            self.assertIn("get_agent_openai_client(project_client, agent_runtime,", cells[cell_id])
            self.assertIn("**response_options(agent_runtime,", cells[cell_id])
            self.assertIn("return responses_url(openai_client)", cells[cell_id])
            self.assertIn('"agent_invocation_mode": agent_runtime.mode', cells[cell_id])
        self.assertIn('Name endswith "/responses"', cells["6e3dcab6"])
        self.assertIn('tostring(Properties["gen_ai.operation.name"]) == "responses.create"', cells["6e3dcab6"])


class AgentVersionSyncTests(unittest.TestCase):
    def setUp(self):
        self.definition = PromptAgentDefinition(model="alias", instructions="original", tools=[])
        self.changed = PromptAgentDefinition(model="alias", instructions="updated", tools=[])
        self.versions = {"1": SimpleNamespace(version="1", definition=self.definition)}
        self.latest = "1"
        self.configuration = {
            "version_selector": {"version_selection_rules": [
                {"type": "FixedRatio", "agent_version": "1", "traffic_percentage": 100}
            ]},
            "protocol_configuration": {"responses": {}},
            "authorization_schemes": [{"type": "Entra"}],
        }
        self.identity = AgentIdentity({"principal_id": "principal", "client_id": "client"})
        self.client = Mock()
        self.client.agents.get.side_effect = self.get_agent
        self.client.agents.get_version.side_effect = lambda *, agent_name, agent_version, **kw: self.versions[agent_version]
        self.client.agents.create_version.side_effect = self.create_version
        self.client.agents.update_details.side_effect = self.activate
        self.target = AgentTarget("main-backend", "1")
        self.build = {
            "agent_invocation_mode": "agent_endpoint", "backend_version_policy": "sync",
            "foundry_project_endpoint": "https://example.invalid/project", "untouched": {"value": 42},
            "backend_agents": {
                "main": {"name": "main-backend", "version": "1"},
                "sentinel": {"name": "sentinel-backend", "version": "1"},
            },
        }
        self.runtime = AgentRuntimeConfig.from_build_info(self.build, {})
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "build_info-test.json"
        self.path.write_text(json.dumps(self.build), encoding="utf-8")

    def get_agent(self, **kwargs):
        configuration = deepcopy(self.configuration)
        return SimpleNamespace(
            state="enabled", instance_identity=self.identity,
            agent_endpoint=SimpleNamespace(as_dict=lambda: configuration),
            versions=SimpleNamespace(latest=self.versions[self.latest]),
        )

    def create_version(self, *, agent_name, definition, **kwargs):
        version = str(max(map(int, self.versions)) + 1)
        self.versions[version] = SimpleNamespace(version=version, definition=definition)
        self.latest = version
        return self.versions[version]

    def activate(self, *, agent_name, agent_endpoint, **kwargs):
        update = agent_endpoint.as_dict()
        self.assertEqual(set(update), {"version_selector"})
        self.configuration.update(update)
        return self.get_agent()

    def prepare(self, definition=None):
        return prepare_backend_agent(
            self.client, self.runtime, "main", definition or self.changed,
            build_info=self.build, build_info_path=self.path,
        )

    def test_sync_is_explicit_and_pinned_remains_the_compatible_default(self):
        self.assertEqual(self.runtime.version_policy, "sync")
        old = {key: value for key, value in self.build.items() if key != "backend_version_policy"}
        self.assertEqual(AgentRuntimeConfig.from_build_info(old, {}).version_policy, "pinned")
        for policy in ("", "unknown", None, {}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                AgentRuntimeConfig.from_build_info({**self.build, "backend_version_policy": policy}, {})

    def test_changed_definition_creates_activates_and_saves_version(self):
        selected, updated = self.prepare()
        self.assertEqual(selected.version, "2")
        self.assertEqual(updated.target("main").version, "2")
        self.assertEqual(self.configuration["version_selector"]["version_selection_rules"][0]["agent_version"], "2")
        self.assertEqual(self.configuration["authorization_schemes"], [{"type": "Entra"}])
        self.assertEqual(self.configuration["protocol_configuration"], {"responses": {}})
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["backend_agents"]["main"]["version"], "2")
        self.assertEqual(saved["backend_agents"]["sentinel"]["version"], "1")
        self.assertEqual(saved["untouched"], {"value": 42})
        self.assertEqual(self.build["backend_agents"]["main"]["version"], "2")
        self.assertIn("1", self.versions)

    def test_unchanged_rerun_does_not_create_or_activate_again(self):
        _, self.runtime = self.prepare()
        self.client.reset_mock()
        before = self.path.read_bytes()
        selected, _ = self.prepare()
        self.assertEqual(selected.version, "2")
        self.client.agents.create_version.assert_not_called()
        self.client.agents.update_details.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_matching_latest_candidate_is_reused(self):
        self.versions["2"] = SimpleNamespace(version="2", definition=self.changed)
        self.latest = "2"
        selected, _ = self.prepare()
        self.assertEqual(selected.version, "2")
        self.client.agents.create_version.assert_not_called()
        self.client.agents.update_details.assert_called_once()

    def test_stale_local_version_is_reconciled_to_matching_active_version(self):
        self.versions["2"] = SimpleNamespace(version="2", definition=self.changed)
        self.latest = "2"
        self.configuration["version_selector"]["version_selection_rules"][0]["agent_version"] = "2"
        selected, updated = self.prepare()
        self.assertEqual(selected.version, updated.target("main").version)
        self.assertEqual(updated.target("main").version, "2")
        self.client.agents.create_version.assert_not_called()
        self.client.agents.update_details.assert_not_called()

    def test_create_failure_does_not_change_routing_or_local_selection(self):
        self.client.agents.create_version.side_effect = RuntimeError("create denied")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "create denied"):
            self.prepare()
        self.client.agents.update_details.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_activation_failure_is_not_reported_as_success(self):
        self.client.agents.update_details.side_effect = RuntimeError("activation denied")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "activation denied"):
            self.prepare()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.configuration["version_selector"]["version_selection_rules"][0]["agent_version"], "1")

    def test_mismatching_created_version_is_never_activated(self):
        self.client.agents.create_version.side_effect = None
        self.client.agents.create_version.return_value = SimpleNamespace(version="2", definition=self.definition)
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            self.prepare()
        self.client.agents.update_details.assert_not_called()

    def test_unverified_activation_does_not_update_local_file(self):
        self.client.agents.update_details.side_effect = None
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "Could not verify"):
            self.prepare()
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_routing_change_is_not_overwritten(self):
        original_create = self.create_version

        def concurrent_create(**kwargs):
            result = original_create(**kwargs)
            self.configuration["authorization_schemes"].append({"type": "BotServiceRbac"})
            return result

        self.client.agents.create_version.side_effect = concurrent_create
        with self.assertRaisesRegex(RuntimeError, "changed during synchronization"):
            self.prepare()
        self.client.agents.update_details.assert_not_called()

    def test_split_or_latest_routing_requires_a_deliberate_policy_change(self):
        self.configuration["version_selector"]["version_selection_rules"][0]["agent_version"] = "@latest"
        with self.assertRaisesRegex(RuntimeError, "single fixed version"):
            self.prepare()
        self.client.agents.create_version.assert_not_called()
        self.client.agents.update_details.assert_not_called()

    def test_local_save_failure_is_explicit_and_next_run_repairs_selection(self):
        original = self.path.read_bytes()
        with patch("notebook_agent_endpoints.os.replace", side_effect=OSError("read only")):
            with self.assertRaisesRegex(RuntimeError, "active on version 2.*could not be saved"):
                self.prepare()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))
        self.client.reset_mock()
        selected, _ = self.prepare()
        self.assertEqual(selected.version, "2")
        self.assertEqual(json.loads(self.path.read_text())["backend_agents"]["main"]["version"], "2")
        self.client.agents.create_version.assert_not_called()
        self.client.agents.update_details.assert_not_called()

    def test_invalid_build_file_stops_before_any_cloud_changes(self):
        self.path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "JSON object"):
            self.prepare()
        self.client.agents.get.assert_not_called()
        self.client.agents.create_version.assert_not_called()

    def test_concurrent_local_edits_are_not_overwritten_after_activation(self):
        def activate_and_edit(**kwargs):
            result = self.activate(**kwargs)
            edited = json.loads(self.path.read_text())
            edited["user_note"] = "preserve this edit"
            self.path.write_text(json.dumps(edited), encoding="utf-8")
            return result

        self.client.agents.update_details.side_effect = activate_and_edit
        with self.assertRaisesRegex(RuntimeError, "active on version 2.*could not be saved"):
            self.prepare()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["user_note"], "preserve this edit")
        self.assertEqual(saved["backend_agents"]["main"]["version"], "1")
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_notebook_wires_both_roles_and_keeps_updated_runtime(self):
        notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        source = "".join(next(cell["source"] for cell in notebook["cells"] if cell["id"] == "586f0511"))
        self.assertIn("main_agent, agent_runtime = prepare_backend_agent(", source)
        self.assertIn("sentinel_project_agent, agent_runtime = prepare_backend_agent(", source)
        self.assertEqual(source.count("build_info=build_info, build_info_path=build_info_path"), 2)


class TelemetryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "OTEL_SERVICE_NAME": "foundry-agent-framework-demo",
            "OTEL_SERVICE_VERSION": "2026.09.14",
            "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS": "otel",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.instrumentor = Mock()
        self.instrumentor.is_instrumented.return_value = True
        self.instrumentor.is_content_recording_enabled.side_effect = (
            lambda: self.instrumentor.instrument.call_args.kwargs["enable_content_recording"]
        )
        self.httpx2 = SimpleNamespace(is_instrumented_by_opentelemetry=True)
        self.configurations = []

        def capture_configuration(**kwargs):
            self.configurations.append(_get_configurations(**kwargs))

        self.configure = Mock(side_effect=capture_configuration)
        self.scope = load_functions("3c78effc", [
            "get_content_recording_policy", "configure_notebook_telemetry",
        ], {
            "os": os, "Resource": Resource, "settings": SimpleNamespace(),
            "configure_azure_monitor": self.configure,
            "AIProjectInstrumentor": lambda: self.instrumentor,
            "HTTPX2ClientInstrumentor": lambda: self.httpx2,
        })

    def initialize(self, **overrides):
        arguments = {"connection_string": "test-only", "project": "test-project", "session": "test-session"}
        return self.scope["configure_notebook_telemetry"](**(arguments | overrides))

    def test_actual_distro_configuration_is_trace_only(self):
        self.assertTrue(self.initialize())
        config = self.configurations[0]
        self.assertFalse(config["disable_tracing"])
        self.assertTrue(config["disable_logging"])
        self.assertTrue(config["disable_metrics"])
        self.assertFalse(config["enable_live_metrics"])
        self.assertFalse(config["enable_performance_counters"])

    def test_inherited_sampler_cannot_reduce_demo_coverage(self):
        os.environ["OTEL_TRACES_SAMPLER"] = "microsoft.fixed_percentage"
        os.environ["OTEL_TRACES_SAMPLER_ARG"] = "0.1"
        self.initialize()
        self.assertEqual(self.configurations[0]["sampling_ratio"], 1.0)

    def test_content_recording_defaults_to_enabled(self):
        self.assertTrue(self.initialize())
        self.assertEqual(os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"], "true")
        self.instrumentor.instrument.assert_called_once_with(
            enable_content_recording=True,
            enable_trace_context_propagation=True,
            enable_baggage_propagation=True,
        )

    def test_explicit_false_opts_out_of_content_recording(self):
        os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = " FALSE "
        self.assertFalse(self.initialize())
        self.assertEqual(os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"], "false")
        self.assertFalse(self.instrumentor.instrument.call_args.kwargs["enable_content_recording"])

    def test_old_content_flag_does_not_override_explicit_opt_out(self):
        os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "false"
        os.environ["AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED"] = "true"
        self.assertFalse(self.initialize())
        self.instrumentor.instrument.assert_called_once_with(
            enable_content_recording=False,
            enable_trace_context_propagation=True,
            enable_baggage_propagation=True,
        )

    def test_single_content_policy_is_normalized_and_passed_to_sdk(self):
        os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = " TRUE "
        self.assertTrue(self.initialize())
        self.assertEqual(os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"], "true")
        self.assertTrue(self.instrumentor.instrument.call_args.kwargs["enable_content_recording"])

    def test_invalid_content_values_fail_before_provider_setup(self):
        for value in ("1", "0", "", "yes"):
            with self.subTest(value=value):
                os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = value
                with self.assertRaisesRegex(ValueError, "must be"):
                    self.initialize()
        self.configure.assert_not_called()

    def test_resource_identity_is_preserved_without_agent_framework(self):
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "custom.label=retained,service.version=wrong"
        self.initialize()
        attributes = self.configurations[0]["resource"].attributes
        self.assertEqual(attributes["service.name"], "foundry-agent-framework-demo")
        self.assertEqual(attributes["service.version"], "2026.09.14")
        self.assertEqual(attributes["service.namespace"], "foundry-agent-demo")
        self.assertEqual(attributes["service.instance.id"], "test-session")
        self.assertEqual(attributes["foundry.project.name"], "test-project")
        self.assertEqual(attributes["deployment.environment"], "demo")
        self.assertEqual(attributes["deployment.environment.name"], "demo")
        self.assertEqual(attributes["custom.label"], "retained")
        self.assertNotIn("cloud.region", attributes)

    def test_repeated_setup_does_not_add_duplicate_providers(self):
        self.initialize()
        self.initialize()
        self.configure.assert_called_once()
        self.instrumentor.instrument.assert_called_once()

    def test_changed_content_policy_requires_kernel_restart(self):
        self.initialize()
        os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "false"
        with self.assertRaisesRegex(RuntimeError, "Restart the kernel"):
            self.initialize()
        self.configure.assert_called_once()

    def test_changed_project_or_backend_requires_restart(self):
        self.initialize()
        for override in ({"project": "other-project"}, {"connection_string": "other-backend"}):
            with self.subTest(override=override):
                with self.assertRaisesRegex(RuntimeError, "Restart the kernel"):
                    self.initialize(**override)

    def test_old_notebook_initialization_requires_restart(self):
        self.scope["_project_otel_initialized"] = True
        with self.assertRaisesRegex(RuntimeError, "Restart the kernel"):
            self.initialize()
        self.configure.assert_not_called()

    def test_explicit_trace_disable_is_not_silently_ignored(self):
        os.environ["OTEL_TRACES_EXPORTER"] = "none"
        with self.assertRaisesRegex(RuntimeError, "disables this demo"):
            self.initialize()
        self.configure.assert_not_called()

    def test_missing_httpx2_instrumentation_is_reported(self):
        self.httpx2.is_instrumented_by_opentelemetry = False
        with self.assertRaisesRegex(RuntimeError, "HTTPX2 instrumentation is disabled"):
            self.initialize()
        with self.assertRaisesRegex(RuntimeError, "previous telemetry setup failed"):
            self.initialize()
        self.configure.assert_called_once()

    def test_sdk_content_policy_mismatch_is_reported(self):
        self.instrumentor.is_content_recording_enabled.side_effect = None
        self.instrumentor.is_content_recording_enabled.return_value = False
        with self.assertRaisesRegex(RuntimeError, "requested content policy"):
            self.initialize()


if __name__ == "__main__":
    unittest.main()
