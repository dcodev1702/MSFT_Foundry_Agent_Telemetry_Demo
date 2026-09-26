"""Opt-in LiteLLM gateway routing for the Linux notebook."""

import io
import json
import os
import time
import unittest
import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import httpx2
import yaml
from azure.ai.projects import AIProjectClient
from azure.ai.projects.telemetry import AIProjectInstrumentor
from azure.core.credentials import AccessToken
from azure.core.settings import settings
from azure.core.tracing.ext.opentelemetry_span import OpenTelemetrySpan
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode

from notebook_support.agent_endpoints import (
    AgentRuntimeConfig, AgentTarget, get_agent_openai_client, responses_url,
)
from notebook_support.gateway import (
    GatewayConfig, check_gateway_ready, configure_agent_gateway, gateway_mode, load_gateway_config,
)
import test_notebook_validation as validation_tests


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "zolab-ai-agent-demo-linux.ipynb"
NOW = datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc)
ENDPOINT_RUNTIME = AgentRuntimeConfig(
    mode="agent_endpoint", main=AgentTarget("main-backend", "1"),
    sentinel=AgentTarget("sentinel-backend", "1"), version_policy="sync",
)
GATEWAY = GatewayConfig(
    name="litellm", base_url="http://127.0.0.1:4000", api_key="sk-test-master",
    token_expires_at=NOW + timedelta(hours=1),
)


def write_env(directory, **overrides):
    values = {
        "LITELLM_MASTER_KEY": "sk-test-master", "LITELLM_BIND_ADDRESS": "127.0.0.1",
        "LITELLM_PORT": "4000", "AZURE_AD_TOKEN": "upstream-test-token",
        "AZURE_AD_TOKEN_EXPIRES_ON": (NOW + timedelta(hours=1)).isoformat(),
    }
    values.update(overrides)
    path = Path(directory) / "gateway.env"
    path.write_text(
        "# test fixture\n" + "".join(f"{key}='{value}'\n" for key, value in values.items()),
        encoding="utf-8",
    )
    return path


class GatewayConfigurationTests(unittest.TestCase):
    def test_direct_is_the_default_and_environment_overrides_build_metadata(self):
        self.assertEqual(gateway_mode({}, {}), "direct")
        self.assertEqual(gateway_mode({"agent_gateway": "litellm"}, {}), "litellm")
        self.assertEqual(
            gateway_mode({"agent_gateway": "litellm"}, {"FOUNDRY_AGENT_GATEWAY": "direct"}), "direct",
        )
        for value in ("LiteLLM", "proxy", 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gateway_mode({"agent_gateway": value}, {})

    def test_direct_mode_leaves_the_runtime_unchanged(self):
        self.assertIs(configure_agent_gateway(ENDPOINT_RUNTIME, {}, {}), ENDPOINT_RUNTIME)
        self.assertIsNone(ENDPOINT_RUNTIME.gateway)
        self.assertEqual(ENDPOINT_RUNTIME.label, "responses.create + agent endpoint")

    def test_gateway_requires_agent_endpoint_mode(self):
        with self.assertRaisesRegex(ValueError, "agent_endpoint"):
            configure_agent_gateway(AgentRuntimeConfig(mode="project"), {"agent_gateway": "litellm"}, {})

    def test_settings_are_loaded_without_exposing_the_master_key(self):
        with TemporaryDirectory() as directory:
            path = write_env(directory, LITELLM_BIND_ADDRESS="0.0.0.0", LITELLM_PORT="4100")
            runtime = configure_agent_gateway(
                ENDPOINT_RUNTIME, {"agent_gateway": "litellm"},
                {"FOUNDRY_AGENT_GATEWAY_ENV_FILE": str(path)},
            )
        gateway = runtime.gateway
        assert gateway is not None
        self.assertEqual(gateway.base_url, "http://127.0.0.1:4100")
        self.assertEqual(gateway.api_key, "sk-test-master")
        self.assertEqual(gateway.token_expires_at, NOW + timedelta(hours=1))
        self.assertNotIn("sk-test-master", repr(gateway))
        self.assertNotIn("sk-test-master", repr(runtime))
        self.assertEqual(runtime.label, "responses.create + agent endpoint via LiteLLM gateway")
        self.assertEqual((runtime.main, runtime.sentinel), (ENDPOINT_RUNTIME.main, ENDPOINT_RUNTIME.sentinel))

    def test_missing_or_placeholder_settings_fail_with_start_guidance(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(FileNotFoundError, "gateway/start.sh"):
                load_gateway_config({"FOUNDRY_AGENT_GATEWAY_ENV_FILE": str(Path(directory) / "missing.env")})
            for master_key in ("sk-REPLACE_WITH_A_LONG_RANDOM_VALUE", ""):
                with self.subTest(master_key=master_key), self.assertRaisesRegex(ValueError, "gateway/start.sh"):
                    load_gateway_config({
                        "FOUNDRY_AGENT_GATEWAY_ENV_FILE": str(write_env(directory, LITELLM_MASTER_KEY=master_key)),
                    })
            with self.assertRaisesRegex(ValueError, "LITELLM_PORT"):
                load_gateway_config({
                    "FOUNDRY_AGENT_GATEWAY_ENV_FILE": str(write_env(directory, LITELLM_PORT="http")),
                })

    def test_runtime_replacement_during_version_sync_keeps_the_gateway(self):
        runtime = replace(ENDPOINT_RUNTIME, gateway=GATEWAY)
        updated = replace(runtime, main=AgentTarget("main-backend", "2"))
        self.assertIs(updated.gateway, GATEWAY)


class GatewayRoutingTests(unittest.TestCase):
    def setUp(self):
        self.runtime = replace(ENDPOINT_RUNTIME, gateway=GATEWAY)
        self.client = Mock()

    def test_each_agent_uses_its_own_gateway_route_and_the_proxy_key(self):
        for name, role in (("main-backend", "main"), ("sentinel-backend", "sentinel")):
            with self.subTest(role=role):
                self.client.reset_mock()
                get_agent_openai_client(self.client, self.runtime, name)
                self.client.get_openai_client.assert_called_once_with(
                    agent_name=name, base_url=f"http://127.0.0.1:4000/foundry-agent/{role}",
                    api_key="sk-test-master",
                )
        with self.assertRaises(ValueError):
            get_agent_openai_client(self.client, self.runtime, "unconfigured-agent")

    def test_gateway_is_rejected_without_agent_endpoints(self):
        with self.assertRaisesRegex(ValueError, "agent endpoint"):
            get_agent_openai_client(self.client, AgentRuntimeConfig(mode="project", gateway=GATEWAY), "main")
        self.client.get_openai_client.assert_not_called()

    def test_responses_url_accepts_only_the_configured_gateway_routes(self):
        for role in ("main", "sentinel"):
            client = SimpleNamespace(base_url=f"http://127.0.0.1:4000/foundry-agent/{role}/")
            self.assertEqual(responses_url(client), f"http://127.0.0.1:4000/foundry-agent/{role}/responses")
        with self.assertRaises(ValueError):
            responses_url(SimpleNamespace(base_url="http://127.0.0.1:4000/foundry-agent/other/"))

    def test_span_attributes_name_the_gateway_and_the_upstream_foundry_host(self):
        self.assertEqual(
            GATEWAY.span_attributes("https://demo.services.ai.azure.com/api/projects/demo"),
            {"app.gateway.name": "litellm", "app.upstream.server.address": "demo.services.ai.azure.com"},
        )


class GatewaySdkTransportTests(unittest.TestCase):
    """Use the real Foundry/OpenAI SDKs with an in-memory transport, never a live gateway."""

    def setUp(self):
        self.requests = []
        self.provider = TracerProvider()
        self.exporter = InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.addCleanup(self.provider.shutdown)

    def transport(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/conversations"):
            data = {"id": "conv_test", "object": "conversation", "created_at": 1}
        else:
            data = {
                "id": "resp_test", "object": "response", "created_at": 1, "status": "completed",
                "model": "sdk-test-model", "output": [], "usage": {
                    "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                },
            }
        return httpx2.Response(200, json=data, request=request)

    def test_sdk_sends_notebook_calls_to_the_gateway_with_proxy_auth_and_trace_context(self):
        class TestCredential:
            def get_token(self, *scopes, **kwargs):
                return AccessToken("entra-token-must-not-reach-gateway", int(time.time()) + 3600)

        previous_implementation = settings.tracing_implementation()
        previous_enabled = settings.tracing_enabled()
        instrumentor = AIProjectInstrumentor()
        try:
            with (
                patch.dict(os.environ, {"AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING": "true"}),
                patch.object(trace, "get_tracer_provider", return_value=self.provider),
            ):
                settings.tracing_implementation = OpenTelemetrySpan
                settings.tracing_enabled = True
                instrumentor.instrument(enable_content_recording=False, enable_trace_context_propagation=True)
                with (
                    AIProjectClient(
                        endpoint="https://example.services.ai.azure.com/api/projects/sdk-test",
                        credential=TestCredential(), allow_preview=True,
                    ) as project,
                    project.get_openai_client(
                        agent_name="main-backend", base_url=GATEWAY.agent_base_url("main"),
                        api_key=GATEWAY.api_key,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(self.transport)),
                    ) as client,
                ):
                    self.assertEqual(responses_url(client), "http://127.0.0.1:4000/foundry-agent/main/responses")
                    with self.provider.get_tracer("gateway-test").start_as_current_span("notebook-request") as root:
                        conversation = client.conversations.create()
                        client.responses.create(conversation=conversation.id, input="Gateway test")
        finally:
            instrumentor.uninstrument()
            settings.tracing_implementation = previous_implementation
            settings.tracing_enabled = previous_enabled
        self.assertEqual(
            [request.url.path for request in self.requests],
            ["/foundry-agent/main/conversations", "/foundry-agent/main/responses"],
        )
        trace_id = format(root.get_span_context().trace_id, "032x")
        for request in self.requests:
            with self.subTest(path=request.url.path):
                self.assertEqual(request.url.params.get("api-version"), "v1")
                self.assertEqual(request.headers["authorization"], "Bearer sk-test-master")
                self.assertIn("foundry-features", request.headers)
                self.assertIn(trace_id, request.headers["traceparent"])


class GatewayReadinessTests(unittest.TestCase):
    @staticmethod
    def gateway(expires):
        return replace(GATEWAY, token_expires_at=expires)

    @staticmethod
    def opener(payload=None, error=None):
        calls = []

        def open_url(url, timeout):
            calls.append((url, timeout))
            if error is not None:
                raise error
            return io.BytesIO(json.dumps(payload).encode())

        return open_url, calls

    def test_ready_gateway_reports_database_and_remaining_token_lifetime(self):
        opener, calls = self.opener({"status": "healthy", "db": "connected"})
        status = check_gateway_ready(self.gateway(NOW + timedelta(minutes=45)), now=NOW, opener=opener)
        self.assertEqual(status, {"db": "connected", "token_minutes_remaining": 45})
        self.assertEqual(calls, [("http://127.0.0.1:4000/health/readiness", 10)])

    def test_short_lived_or_unknown_token_fails_before_contacting_the_gateway(self):
        for expires in (None, NOW + timedelta(minutes=9), NOW - timedelta(minutes=1)):
            opener, calls = self.opener({"db": "connected"})
            with self.subTest(expires=expires), self.assertRaisesRegex(RuntimeError, "gateway/start.sh"):
                check_gateway_ready(self.gateway(expires), now=NOW, opener=opener)
            self.assertEqual(calls, [])

    def test_unready_unreachable_or_disconnected_gateway_fails_with_guidance(self):
        for error in (
            urllib.error.HTTPError("http://127.0.0.1:4000/health/readiness", 503, "Unavailable", {}, None),
            urllib.error.URLError("connection refused"),
        ):
            opener, _ = self.opener(error=error)
            with self.subTest(error=type(error).__name__), self.assertRaisesRegex(RuntimeError, "gateway/start.sh"):
                check_gateway_ready(self.gateway(NOW + timedelta(hours=1)), now=NOW, opener=opener)
        opener, _ = self.opener({"status": "healthy", "db": "disconnected"})
        with self.assertRaisesRegex(RuntimeError, "Neon"):
            check_gateway_ready(self.gateway(NOW + timedelta(hours=1)), now=NOW, opener=opener)


class GatewayConfigFileTests(unittest.TestCase):
    def test_each_agent_route_forwards_trace_context_and_replaces_client_authorization(self):
        settings_block = yaml.safe_load((ROOT / "gateway" / "config.yaml").read_text(encoding="utf-8"))["general_settings"]
        routes = {route["path"]: route for route in settings_block["pass_through_endpoints"]}
        self.assertEqual(set(routes), {"/foundry-agent/main", "/foundry-agent/sentinel"})
        for role in ("main", "sentinel"):
            route = routes[f"/foundry-agent/{role}"]
            with self.subTest(role=role):
                self.assertEqual(route["target"], f"os.environ/FOUNDRY_{role.upper()}_AGENT_BASE_URL")
                self.assertTrue(route["auth"])
                self.assertTrue(route["include_subpath"])
                self.assertTrue(route["forward_headers"])
                self.assertEqual(route["methods"], ["POST"])
                self.assertEqual(route["headers"], {"Authorization": "Bearer os.environ/AZURE_AD_TOKEN"})
                self.assertEqual(route["default_query_params"], {"api-version": "v1"})
        self.assertTrue(settings_block["disable_spend_logs"])
        self.assertEqual(settings_block["pass_through_request_timeout"], 600)
        service = yaml.safe_load((ROOT / "gateway" / "compose.yaml").read_text(encoding="utf-8"))["services"]["litellm"]
        for key in ("FOUNDRY_MAIN_AGENT_BASE_URL", "FOUNDRY_SENTINEL_AGENT_BASE_URL", "DATABASE_URL"):
            self.assertIn(key, service["environment"])
        self.assertEqual(service["ports"], ["${LITELLM_BIND_ADDRESS:-127.0.0.1}:${LITELLM_PORT:-4000}:4000"])


class LinuxNotebookGatewayWiringTests(unittest.TestCase):
    def cells(self):
        notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        return {cell["id"]: "".join(cell["source"]) for cell in notebook["cells"]}

    def test_setup_binds_the_optional_gateway_and_checks_it_before_agent_calls(self):
        source = self.cells()["8330c10b"]
        self.assertIn(
            "configure_agent_gateway(AgentRuntimeConfig.from_build_info(build_info), build_info)", source,
        )
        self.assertIn("check_gateway_ready(agent_runtime.gateway)", source)
        self.assertIn("agent_gateway_span_attributes = ", source)
        self.assertNotIn("gateway.api_key", source)

    def test_both_response_paths_tag_gateway_requests_and_leave_direct_requests_unchanged(self):
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        self.addCleanup(provider.shutdown)
        tracer = provider.get_tracer("gateway-wiring-test")
        attributes = GATEWAY.span_attributes("https://demo.services.ai.azure.com/api/projects/demo")
        for cell_id in ("2692d274", "ef551c01"):
            for gateway_attributes in (attributes, None):
                with self.subTest(cell=cell_id, gateway=bool(gateway_attributes)):
                    exporter.clear()
                    namespace = {
                        "tracer": tracer, "SpanKind": SpanKind, "Status": Status, "StatusCode": StatusCode,
                        "otel_context": context, "make_baggage_context": lambda _values: context.Context(),
                        "baggage_values": {}, "urlparse": urlparse,
                        "build_responses_url": lambda _client: "http://127.0.0.1:4000/foundry-agent/main/responses",
                        "conversation": SimpleNamespace(id="conversation-1"),
                        "openai_client": SimpleNamespace(responses=SimpleNamespace(
                            create=Mock(return_value=SimpleNamespace(id="response-1")),
                        )),
                        "model_name": "demo-model", "main_agent_display_name": "main",
                        "sentinel_agent_display_name": "sentinel", "agent_runtime": None,
                        "agent_reference_payload": {}, "sentinel_agent_reference_payload": {},
                        "response_options": lambda *_args: {}, "content_recording_enabled": False,
                        "tool_content_recording_enabled": False,
                        "record_response_observability": lambda *_args, **_kwargs: None,
                    }
                    if gateway_attributes is not None:
                        namespace["agent_gateway_span_attributes"] = gateway_attributes
                    with patch.object(validation_tests, "NOTEBOOK", NOTEBOOK):
                        scope = validation_tests.load_functions(cell_id, ["create_agent_response"], namespace)
                    scope["create_agent_response"]("demo prompt")
                    span = next(
                        span for span in exporter.get_finished_spans() if span.name == "POST /openai/v1/responses"
                    )
                    assert span.attributes is not None
                    self.assertEqual(span.attributes["server.address"], "127.0.0.1")
                    for key, value in attributes.items():
                        if gateway_attributes is None:
                            self.assertNotIn(key, span.attributes)
                        else:
                            self.assertEqual(span.attributes[key], value)


if __name__ == "__main__":
    unittest.main()
