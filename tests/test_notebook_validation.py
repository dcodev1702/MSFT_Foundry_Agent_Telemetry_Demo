import ast
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import Mock, patch

from azure.monitor.opentelemetry._utils.configurations import _get_configurations
from opentelemetry import context, trace
from opentelemetry.sdk.resources import Resource
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
        self.assertIn("without searching for or substituting another table", self.instructions)
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
