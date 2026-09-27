"""Opt-in LiteLLM gateway routing for the Linux notebook."""

import base64
import hashlib
import importlib.util
import io
import json
import os
import sys
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
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
    COLLECTOR_STATUS_RULES, LITELLM_TRACE_SETTINGS, SERVICE_CONFIG_FILES, GatewayConfig, check_gateway_ready,
    check_gateway_telemetry, collector_normalizes_litellm_status, configure_agent_gateway,
    gateway_infrastructure_status, gateway_mode, load_gateway_config,
)
from notebook_support.observability import build_observability_queries, render_observability_report
from test_notebook_observability import RUN_ID, passing_coverage, report_results
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


def write_gateway_dir(directory, *, normalize_status=True):
    gateway_dir = Path(directory) / "gateway"
    gateway_dir.mkdir(exist_ok=True)
    (gateway_dir / "config.yaml").write_text("general_settings: {}\n", encoding="utf-8")
    collector = (ROOT / "gateway" / "otel-collector.yaml").read_text(encoding="utf-8")
    if not normalize_status:
        collector = collector.replace("processors: [transform/litellm_status, batch]", "processors: [batch]")
    (gateway_dir / "otel-collector.yaml").write_text(collector, encoding="utf-8")
    return gateway_dir


LITELLM_ENV = [f"{key}={value}" for key, value in LITELLM_TRACE_SETTINGS.items()] + ["LITELLM_LOG=INFO"]


def fake_docker(containers, *, started_at=None, logs=None, error=None, litellm_env=None):
    """Answer the gateway's docker ps/inspect/logs calls; containers maps service -> "image\\tstatus"."""
    started_at = started_at or f"{datetime.now(timezone.utc) + timedelta(minutes=1):%Y-%m-%dT%H:%M:%S}.123456789Z"
    environments = {"id-litellm": LITELLM_ENV if litellm_env is None else litellm_env}
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if error is not None:
            raise error
        if command[:2] == ["docker", "ps"]:
            service = next(item.rsplit("=", 1)[1] for item in command if "compose.service=" in item)
            listed = containers.get(service)
            return SimpleNamespace(returncode=0, stdout=f"id-{service}\t{listed}\n" if listed else "", stderr="")
        if command[:2] == ["docker", "inspect"]:
            environment = json.dumps(environments.get(command[-1], []))
            return SimpleNamespace(returncode=0, stdout=f"{started_at} {environment}\n", stderr="")
        if command[:2] == ["docker", "logs"]:
            return SimpleNamespace(returncode=0, stdout="", stderr=(logs or {}).get(command[-1], ""))
        raise AssertionError(f"unexpected command: {command}")

    run.calls = calls
    return run


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
        self.assertEqual((gateway.virtual_key, gateway.default_headers()), (False, {}))

    def test_provisioned_virtual_key_and_end_user_are_used_without_exposing_the_key(self):
        with TemporaryDirectory() as directory:
            path = write_env(
                directory, LITELLM_NOTEBOOK_KEY="sk-test-virtual", LITELLM_END_USER_ID="analyst@example.com",
            )
            gateway = load_gateway_config({"FOUNDRY_AGENT_GATEWAY_ENV_FILE": str(path)})
        self.assertEqual((gateway.api_key, gateway.virtual_key), ("sk-test-virtual", True))
        self.assertEqual(gateway.default_headers(), {"x-litellm-end-user-id": "analyst@example.com"})
        self.assertNotIn("sk-test-virtual", repr(gateway))
        with TemporaryDirectory() as directory:
            path = write_env(directory, LITELLM_NOTEBOOK_KEY="sk-REPLACE_WITH_THE_PROVISIONED_VIRTUAL_KEY")
            placeholder = load_gateway_config({"FOUNDRY_AGENT_GATEWAY_ENV_FILE": str(path)})
        self.assertEqual((placeholder.api_key, placeholder.virtual_key), ("sk-test-master", False))

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
                    api_key="sk-test-master", default_headers={},
                )
        with self.assertRaises(ValueError):
            get_agent_openai_client(self.client, self.runtime, "unconfigured-agent")

    def test_virtual_key_requests_name_the_end_user_for_litellm(self):
        gateway = replace(GATEWAY, api_key="sk-test-virtual", virtual_key=True, end_user_id="analyst@example.com")
        get_agent_openai_client(self.client, replace(ENDPOINT_RUNTIME, gateway=gateway), "main-backend")
        self.client.get_openai_client.assert_called_once_with(
            agent_name="main-backend", base_url="http://127.0.0.1:4000/foundry-agent/main",
            api_key="sk-test-virtual", default_headers={"x-litellm-end-user-id": "analyst@example.com"},
        )

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


class GatewayTelemetryCheckTests(unittest.TestCase):
    RUNNING = {
        "litellm": "ghcr.io/berriai/litellm:main-stable@sha256:abc\tUp 2 hours (healthy)",
        "otel-collector": "otel/opentelemetry-collector-contrib:0.161.0@sha256:abc\tUp 2 hours",
    }

    def test_running_current_normalized_stack_counts_recent_export_failures(self):
        logs = {
            "id-otel-collector": "2026-09-27T08:00:00Z\terror\tqueue_sender.go:1\tExporting failed. Dropping data.\n",
            "id-litellm": "Failed to export span batch code: 503\nPOST /foundry-agent/main/responses 200\n",
        }
        with TemporaryDirectory() as directory:
            run = fake_docker(self.RUNNING, logs=logs)
            status = check_gateway_telemetry(GATEWAY, gateway_dir=write_gateway_dir(directory), run=run)
        self.assertEqual(status, {"config": "current", "status_normalized": True, "export_failures": 2})
        self.assertIn(["docker", "logs", "--since", "30m", "id-otel-collector"], run.calls)

    def test_missing_status_rule_fails_before_docker_is_queried(self):
        with TemporaryDirectory() as directory:
            run = fake_docker(self.RUNNING)
            with self.assertRaisesRegex(RuntimeError, "lacks the transform/litellm_status rules"):
                check_gateway_telemetry(
                    GATEWAY, gateway_dir=write_gateway_dir(directory, normalize_status=False), run=run,
                )
        self.assertEqual(run.calls, [])

    def test_litellm_container_without_otel_v2_settings_must_be_recreated(self):
        stale = [entry for entry in LITELLM_ENV if not entry.startswith("LITELLM_OTEL_V2=")]
        with TemporaryDirectory() as directory:
            gateway_dir = write_gateway_dir(directory)
            for environment, missing in (
                (stale, "LITELLM_OTEL_V2=true"),
                (LITELLM_ENV[1:] + ["LITELLM_OTEL_V2=false"], "LITELLM_OTEL_V2=true"),
                ([], "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=span_only"),
            ):
                with self.subTest(missing=missing):
                    run = fake_docker(self.RUNNING, litellm_env=environment)
                    with self.assertRaisesRegex(RuntimeError, missing) as raised:
                        check_gateway_telemetry(GATEWAY, gateway_dir=gateway_dir, run=run)
                    self.assertIn("Run gateway/start.sh to recreate it", str(raised.exception))
        # The stack in compose.yaml sets exactly these values.
        environment = yaml.safe_load((ROOT / "gateway" / "compose.yaml").read_text(encoding="utf-8"))[
            "services"]["litellm"]["environment"]
        self.assertEqual({key: environment[key] for key in LITELLM_TRACE_SETTINGS}, LITELLM_TRACE_SETTINGS)

    def test_stopped_stale_or_unavailable_containers_fail_with_start_guidance(self):
        cases = {
            "otel-collector container is not running": ({"litellm": self.RUNNING["litellm"]}, {}),
            "config.yaml changed after the litellm container started": (
                self.RUNNING, {"started_at": "2000-01-01T00:00:00.000000001Z"},
            ),
            "Docker is unavailable": (self.RUNNING, {"error": FileNotFoundError("docker")}),
        }
        with TemporaryDirectory() as directory:
            gateway_dir = write_gateway_dir(directory)
            for message, (containers, options) in cases.items():
                with self.subTest(message):
                    with self.assertRaisesRegex(RuntimeError, message) as raised:
                        check_gateway_telemetry(GATEWAY, gateway_dir=gateway_dir, run=fake_docker(containers, **options))
                    self.assertIn("gateway/start.sh", str(raised.exception))


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

    def test_gateway_containers_have_memory_caps(self):
        services = yaml.safe_load((ROOT / "gateway" / "compose.yaml").read_text(encoding="utf-8"))["services"]
        self.assertEqual(services["litellm"]["mem_limit"], "5g")
        self.assertEqual(services["otel-collector"]["mem_limit"], "2560m")

    def test_litellm_exports_content_and_enriched_spans_through_the_collector(self):
        config = yaml.safe_load((ROOT / "gateway" / "config.yaml").read_text(encoding="utf-8"))
        # The Foundry hook fills the logging payload before LiteLLM's otel callback reads it.
        self.assertEqual(
            config["litellm_settings"]["callbacks"], ["litellm_callbacks.foundry_agent_telemetry", "otel"],
        )
        self.assertFalse(config["litellm_settings"]["turn_off_message_logging"])
        services = yaml.safe_load((ROOT / "gateway" / "compose.yaml").read_text(encoding="utf-8"))["services"]
        litellm, collector = services["litellm"], services["otel-collector"]
        environment = litellm["environment"]
        self.assertNotIn("env_file", litellm)
        self.assertIn("./litellm_callbacks.py:/app/litellm_callbacks.py:ro", litellm["volumes"])
        self.assertEqual(environment["OTEL_EXPORTER"], "otlp_http")
        self.assertEqual(environment["OTEL_ENDPOINT"], "http://otel-collector:4318/v1/traces")
        self.assertEqual(environment["OTEL_SERVICE_NAME"], "litellm-gateway")
        self.assertEqual(environment["OTEL_RESOURCE_ATTRIBUTES"], "service.namespace=foundry-agent-demo")
        self.assertEqual(environment["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"], "span_only")
        self.assertEqual(environment["OTEL_ENVIRONMENT_NAME"], "${OTEL_ENVIRONMENT_NAME:-demo}")
        # OpenTelemetry v2 tracing, and stable HTTP attributes on its FastAPI server span.
        self.assertEqual(environment["LITELLM_OTEL_V2"], "true")
        self.assertEqual(environment["OTEL_SEMCONV_STABILITY_OPT_IN"], "http")
        # v1-only setting; v2 always joins the inbound traceparent.
        self.assertNotIn("OTEL_IGNORE_CONTEXT_PROPAGATION", environment)
        self.assertTrue(environment["FOUNDRY_MODEL_DEPLOYMENT"].startswith("${FOUNDRY_MODEL_DEPLOYMENT:?"))
        self.assertNotIn("APPLICATIONINSIGHTS_CONNECTION_STRING", environment)
        self.assertIn("otel-collector", litellm["depends_on"])
        self.assertRegex(collector["image"], r"^otel/opentelemetry-collector-contrib:[0-9.]+@sha256:[0-9a-f]{64}$")
        self.assertNotIn("ports", collector)
        self.assertEqual(list(collector["environment"]), ["APPLICATIONINSIGHTS_CONNECTION_STRING", "GOMEMLIMIT"])
        self.assertEqual(collector["environment"]["GOMEMLIMIT"], "2048MiB")
        pipeline = yaml.safe_load((ROOT / "gateway" / "otel-collector.yaml").read_text(encoding="utf-8"))
        traces = pipeline["service"]["pipelines"]["traces"]
        self.assertEqual((traces["receivers"], traces["exporters"]), (["otlp"], ["azure_monitor"]))
        self.assertEqual(
            pipeline["exporters"]["azure_monitor"]["connection_string"],
            "${env:APPLICATIONINSIGHTS_CONNECTION_STRING}",
        )

    def test_collector_reports_litellm_ok_status_without_error_result_codes(self):
        config_file = ROOT / "gateway" / "otel-collector.yaml"
        pipeline = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        statements = pipeline["processors"]["transform/litellm_status"]["trace_statements"]
        for rule in COLLECTOR_STATUS_RULES:
            self.assertTrue(any(statement.startswith(rule) for statement in statements), rule)
        # Only the POST-only agent routes are renamed; database spans get no RPC hint and so no OK status.
        self.assertIn('"^/foundry-agent/(main|sentinel)/(conversations|responses)$"', statements[0])
        self.assertIn('span.attributes["db.system.name"] == nil', statements[2])
        self.assertIn('span.attributes["http.response.status_code"] < 400', statements[3])
        # Nothing sets an error status to OK or resets an error.
        self.assertFalse(any("STATUS_CODE_ERROR" in statement for statement in statements))
        self.assertTrue(all(
            "span.status.code == STATUS_CODE_UNSET" in statement
            for statement in statements if "span.status.code," in statement
        ))
        self.assertEqual(
            pipeline["service"]["pipelines"]["traces"]["processors"], ["transform/litellm_status", "batch"],
        )
        self.assertTrue(collector_normalizes_litellm_status(config_file))

    def test_start_script_restarts_services_whose_mounted_config_changed(self):
        script = (ROOT / "gateway" / "start.sh").read_text(encoding="utf-8")
        for service, config_names in SERVICE_CONFIG_FILES.items():
            for config_name in config_names:
                self.assertIn(f"{service}:{config_name}", script)
        self.assertLess(script.index(" up -d"), script.index('restart "$service"'))
        self.assertLess(script.index("Trace export: OpenTelemetry Collector running"), script.index("provision_identity.py"))


class GatewayObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.queries = build_observability_queries(RUN_ID)

    def test_gateway_server_spans_join_the_graph_without_widening_the_run_scope(self):
        for name, query in self.queries.items():
            with self.subTest(query=name):
                self.assertIn("let run_operations = AppDependencies", query)
                self.assertIn("let spans = materialize(union AppDependencies, AppRequests", query)
                self.assertIn('IsGatewaySpan=AppRoleName endswith "litellm-gateway"', query)
        self.assertIn('IsGatewaySpan, "llm-gateway"', self.queries["end_to_end"])
        self.assertNotIn('IsGatewaySpan, "gateway"', self.queries["end_to_end"])

    def test_gateway_view_joins_client_gateway_upstream_and_foundry_timings(self):
        query = self.queries["gateway"]
        self.assertIn('where IsGatewaySpan and Type == "AppRequests"', query)
        # LiteLLM's OpenTelemetry v2 upstream-call span is chat <model>, found by its GenAI operation.
        self.assertIn('where IsGatewaySpan and GenAiOperation == "chat"', query)
        self.assertIn('where AppRoleName == "responsesapi" and Name startswith "invoke_agent"', query)
        self.assertIn("GatewayOverheadMs=round(DurationMs - UpstreamMs, 1)", query)

    def render(self, results):
        return render_observability_report(
            RUN_ID, "workspace", passing_coverage(), results, self.queries,
            {"story", "facts", "sentinel"}, content_recording_enabled=True,
        )

    def test_report_shows_gateway_hops_only_when_the_view_was_read(self):
        self.assertNotIn("LiteLLM gateway hops", self.render(report_results()))
        results = report_results()
        results["gateway"] = [{
            "TimeGenerated": "2026-09-26T21:00:00Z", "Interaction": "story", "Route": "/foundry-agent/main/responses",
            "Status": "200", "Success": True, "ClientMs": 1205.4, "GatewayMs": 1190.9, "UpstreamMs": 1189.1,
            "GatewayOverheadMs": 1.8, "FoundryAgentMs": 670.4, "OperationId": "trace-1", "SpanId": "span-1",
        }]
        report = self.render(results)
        for text in ("LiteLLM gateway hops (1 requests", "/foundry-agent/main/responses", "GatewayOverheadMs", "1.8"):
            self.assertIn(text, report)


class GatewayInfrastructureStatusTests(unittest.TestCase):
    FRANKFURT_URL = (
        "postgresql://neondb_owner:test-password@ep-test-00000000.c-6.eu-central-1.aws.neon.tech/neondb"
        "?sslmode=require&channel_binding=require"
    )

    def status(
        self, directory, *, readiness=None, error=None, docker=None, docker_error=None, expires_in=45,
        started_at=None, gateway_dir=ROOT / "gateway",
    ):
        env_file = write_env(
            directory, DATABASE_URL=self.FRANKFURT_URL,
            AZURE_AD_TOKEN_EXPIRES_ON=(NOW + timedelta(minutes=expires_in)).isoformat(),
        )

        def opener(url, timeout):
            if error is not None:
                raise error
            return io.BytesIO(json.dumps(readiness or {}).encode())

        run = fake_docker({"otel-collector": docker} if docker else {}, started_at=started_at, error=docker_error)
        return gateway_infrastructure_status(
            {"agent_gateway": "litellm"}, {"FOUNDRY_AGENT_GATEWAY_ENV_FILE": str(env_file)},
            now=NOW, opener=opener, run=run, gateway_dir=gateway_dir,
        )

    def assert_no_secrets(self, status):
        text = str(status)
        for secret in ("sk-test-master", "test-password", "ep-test-00000000", "upstream-test-token"):
            self.assertNotIn(secret, text)

    def test_direct_mode_marks_the_gateway_stack_as_not_used(self):
        status = gateway_infrastructure_status({}, {})
        self.assertEqual(set(status), {"litellm", "otel_collector", "neon"})
        self.assertTrue(all(value == "➖ Not used (agent_gateway=direct)" for value in status.values()))

    def test_healthy_stack_reports_address_token_collector_version_and_neon_region(self):
        with TemporaryDirectory() as directory:
            status = self.status(
                directory, readiness={"status": "healthy", "db": "connected"},
                docker="otel/opentelemetry-collector-contrib:0.161.0@sha256:abc\tUp 2 hours",
            )
        self.assertEqual(status["litellm"], "✅ Ready at http://127.0.0.1:4000, Foundry token valid 45 min")
        self.assertEqual(status["otel_collector"], "✅ otelcol-contrib 0.161.0, Up 2 hours → App Insights")
        self.assertEqual(status["neon"], "✅ Connected, neondb in aws-eu-central-1 (Frankfurt)")
        self.assert_no_secrets(status)

    def test_collector_with_stale_or_unnormalized_config_is_flagged(self):
        image = "otel/opentelemetry-collector-contrib:0.161.0@sha256:abc\tUp 2 hours"
        with TemporaryDirectory() as directory:
            stale = self.status(directory, readiness={"db": "connected"}, docker=image, started_at="2000-01-01T00:00:00Z")
            gateway_dir = write_gateway_dir(directory, normalize_status=False)
            unnormalized = self.status(directory, readiness={"db": "connected"}, docker=image, gateway_dir=gateway_dir)
        self.assertEqual(
            stale["otel_collector"], "⚠️ otelcol-contrib 0.161.0, Up 2 hours; config changed, run gateway/start.sh",
        )
        self.assertEqual(
            unnormalized["otel_collector"],
            "⚠️ otelcol-contrib 0.161.0, Up 2 hours; LiteLLM status rules missing, "
            "run gateway/start.sh after restoring them",
        )

    def test_unreachable_gateway_and_stopped_collector_are_marked(self):
        with TemporaryDirectory() as directory:
            status = self.status(directory, error=urllib.error.URLError("connection refused"), docker="")
        self.assertTrue(status["litellm"].startswith("❌ Unreachable at http://127.0.0.1:4000"))
        self.assertEqual(status["otel_collector"], "❌ Not running; run gateway/start.sh")
        self.assertEqual(status["neon"], "⚠️ Status unknown, neondb in aws-eu-central-1 (Frankfurt)")
        self.assert_no_secrets(status)

    def test_disconnected_database_short_token_and_missing_docker_are_marked(self):
        body = io.BytesIO(json.dumps({"status": "unhealthy", "db": "disconnected"}).encode())
        error = urllib.error.HTTPError("http://127.0.0.1:4000/health/readiness", 503, "Unavailable", {}, body)
        with TemporaryDirectory() as directory:
            status = self.status(directory, error=error, docker_error=FileNotFoundError("docker"))
            short = self.status(directory, readiness={"db": "connected"}, docker="", expires_in=5)
        self.assertEqual(status["litellm"], "⚠️ Not ready at http://127.0.0.1:4000 (HTTP 503)")
        self.assertEqual(status["otel_collector"], "⚠️ Docker status unavailable")
        self.assertTrue(status["neon"].startswith("❌ Disconnected, neondb in aws-eu-central-1"))
        self.assertIn("Foundry token 5 min left; run gateway/start.sh", short["litellm"])
        self.assert_no_secrets(status)

    def test_missing_gateway_settings_are_reported_without_raising(self):
        status = gateway_infrastructure_status(
            {"agent_gateway": "litellm"}, {"FOUNDRY_AGENT_GATEWAY_ENV_FILE": "/nonexistent/gateway.env"},
        )
        self.assertEqual(status["litellm"], "❌ Not configured; run gateway/start.sh")
        self.assertEqual(status["neon"], "➖ Not checked")


class RuntimeEnvRefreshTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("refresh_runtime_env", ROOT / "gateway" / "refresh_runtime_env.py")
        assert spec is not None and spec.loader is not None
        self.refresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.refresh)

    def test_hand_added_keys_survive_a_refresh_and_managed_keys_are_replaced(self):
        current = {"DATABASE_URL": "old-url", "NEON_API_KEY": "napi_test", "AZURE_AD_TOKEN": "old-token"}
        managed = {"DATABASE_URL": "new-url", "AZURE_AD_TOKEN": "new-token"}
        merged = self.refresh.merge_unmanaged(current, managed)
        self.assertEqual(merged, {"DATABASE_URL": "new-url", "AZURE_AD_TOKEN": "new-token", "NEON_API_KEY": "napi_test"})
        with TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            self.refresh.write_env(env_file, merged)
            self.assertEqual(self.refresh.parse_env(env_file), merged)
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_signed_in_user_is_read_from_the_token_claims(self):
        def token(claims):
            return "header." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".signature"

        self.assertEqual(
            self.refresh.signed_in_user(token({"upn": "analyst@example.com", "oid": "oid-1"})),
            ("analyst@example.com", "analyst@example.com"),
        )
        self.assertEqual(self.refresh.signed_in_user(token({"oid": "oid-1"})), ("oid-1", None))
        self.assertEqual(self.refresh.signed_in_user("not-a-jwt"), (None, None))


class IdentityProvisioningTests(unittest.TestCase):
    def setUp(self):
        with patch.object(sys, "path", [str(ROOT / "gateway"), *sys.path]):
            spec = importlib.util.spec_from_file_location("provision_identity", ROOT / "gateway" / "provision_identity.py")
            assert spec is not None and spec.loader is not None
            self.provision = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.provision)

    def admin(self, responses):
        calls = []

        class FakeAdmin:
            def call(self, method, path, body=None):
                calls.append((method, path.split("?")[0], body))
                return responses.get((method, path.split("?")[0]))

        return FakeAdmin(), calls

    def assert_list_lookups_only(self, calls):
        # LiteLLM's /info endpoints answer a missing team, user or key with 404, a failed request in App Insights.
        self.assertTrue(all(path in {"/team/list", "/user/list", "/key/list"} for method, path, _ in calls if method == "GET"))

    def key(self, current_key, **overrides):
        key = {
            "token": hashlib.sha256(current_key.encode()).hexdigest(), "key_alias": self.provision.KEY_ALIAS,
            "user_id": "analyst@example.com", "team_id": self.provision.TEAM_ID,
            "metadata": {"allowed_passthrough_routes": ["/foundry-agent/main", "/foundry-agent/sentinel"]},
        }
        return {**key, **overrides}

    def test_first_run_creates_team_user_and_a_route_scoped_key_without_failed_lookups(self):
        admin, calls = self.admin({
            ("GET", "/team/list"): [], ("GET", "/user/list"): {"users": []}, ("GET", "/key/list"): {"keys": []},
            ("POST", "/key/generate"): {"key": "sk-test-virtual"},
        })
        key, created = self.provision.ensure_identity(
            admin, user_id="analyst@example.com", user_email="analyst@example.com", current_key=None,
        )
        self.assertEqual((key, created), ("sk-test-virtual", True))
        self.assert_list_lookups_only(calls)
        bodies = {path: body for method, path, body in calls if method == "POST"}
        self.assertEqual(list(bodies), ["/team/new", "/user/new", "/key/generate"])
        self.assertEqual(bodies["/team/new"]["team_id"], "foundry-agent-demo")
        self.assertEqual(
            bodies["/user/new"],
            {"user_id": "analyst@example.com", "user_role": "internal_user", "auto_create_key": False,
             "teams": ["foundry-agent-demo"], "user_email": "analyst@example.com"},
        )
        self.assertEqual(bodies["/key/generate"]["metadata"]["allowed_passthrough_routes"],
                         ["/foundry-agent/main", "/foundry-agent/sentinel"])
        self.assertNotIn("max_budget", bodies["/key/generate"])

    def test_valid_key_is_reused_and_a_user_outside_the_team_is_added(self):
        admin, calls = self.admin({
            ("GET", "/team/list"): [{"team_id": "foundry-agent-demo"}],
            ("GET", "/user/list"): {"users": [{"user_id": "analyst@example.com", "teams": []}]},
            ("GET", "/key/list"): {"keys": [self.key("sk-test-virtual")]},
        })
        key, created = self.provision.ensure_identity(
            admin, user_id="analyst@example.com", user_email=None, current_key="sk-test-virtual",
        )
        self.assertEqual((key, created), ("sk-test-virtual", False))
        self.assert_list_lookups_only(calls)
        self.assertEqual([path for method, path, _ in calls if method == "POST"], ["/team/member_add"])

    def test_key_with_other_routes_is_replaced(self):
        admin, calls = self.admin({
            ("GET", "/team/list"): [{"team_id": "foundry-agent-demo"}],
            ("GET", "/user/list"): {"users": [{"user_id": "analyst@example.com", "teams": ["foundry-agent-demo"]}]},
            ("GET", "/key/list"): {"keys": [self.key("sk-test-old", metadata={})]},
            ("POST", "/key/generate"): {"key": "sk-test-new"},
        })
        key, created = self.provision.ensure_identity(
            admin, user_id="analyst@example.com", user_email=None, current_key="sk-test-old",
        )
        self.assertEqual((key, created), ("sk-test-new", True))
        self.assertEqual([path for method, path, _ in calls if method == "POST"], ["/key/delete", "/key/generate"])

    def test_lookups_retry_transient_failures_and_writes_fail_fast(self):
        responses = [
            urllib.error.HTTPError("http://gateway/key/list", 500, "Server Error", {}, io.BytesIO(b"{}")),
            io.BytesIO(b'{"keys": []}'),
            urllib.error.HTTPError("http://gateway/team/new", 500, "Server Error", {}, io.BytesIO(b'{"error": "db"}')),
        ]

        def opener(request, timeout):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        sleeps = []
        admin = self.provision.LiteLLMAdmin("http://gateway", "sk-test-master", opener=opener, sleep=sleeps.append)
        self.assertEqual(admin.call("GET", "/key/list?key_alias=zolab-notebook-linux"), {"keys": []})
        with self.assertRaisesRegex(RuntimeError, "POST /team/new failed with HTTP 500"):
            admin.call("POST", "/team/new", {"team_id": "foundry-agent-demo"})
        self.assertEqual((sleeps, responses), ([1], []))


class NeonLatencyProbeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("neon_latency", ROOT / "gateway" / "neon-latency.py")
        assert spec is not None and spec.loader is not None
        self.probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.probe)

    def test_region_is_read_from_direct_and_cell_neon_hostnames(self):
        self.assertEqual(self.probe.neon_region("ep-misty-tooth-b5n6zzc8.c-7.us-east-2.aws.neon.tech"), "aws-us-east-2")
        self.assertEqual(self.probe.neon_region("ep-cool-darkness-123456.eu-central-1.aws.neon.tech"), "aws-eu-central-1")
        self.assertIsNone(self.probe.neon_region("db.example.invalid"))

    def test_regions_are_ranked_by_median_latency_and_failures_sort_last(self):
        timings = {"eu-central-1": [9.0, 7.0, 8.0], "us-east-2": [116.0, 115.0, 117.0]}

        def fake_probe(host):
            region = host.split(".")[1]
            if region not in timings:
                raise OSError("unreachable in test")
            return timings[region].pop(0)

        ranked = self.probe.rank_regions(3, fake_probe)
        self.assertEqual(ranked[0], ("aws-eu-central-1", 8.0))
        self.assertEqual(ranked[1], ("aws-us-east-2", 116.0))
        self.assertTrue(all(latency is None for _, latency in ranked[2:]))

    def test_configured_database_host_is_read_without_the_credentials(self):
        with TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "DATABASE_URL='postgresql://role:secret@ep-test-00000000.eu-central-1.aws.neon.tech/neondb?sslmode=require'\n",
                encoding="utf-8",
            )
            self.assertEqual(
                self.probe.configured_database_host(env_file), "ep-test-00000000.eu-central-1.aws.neon.tech",
            )


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
        self.assertIn("if agent_runtime.gateway.virtual_key", source)
        self.assertLess(
            source.index("check_gateway_ready(agent_runtime.gateway)"),
            source.index("check_gateway_telemetry(agent_runtime.gateway)"),
        )
        self.assertIn("agent_gateway_span_attributes = ", source)
        self.assertNotIn("gateway.api_key", source)

    def test_tracing_summary_lists_gateway_components_in_gateway_mode(self):
        source = self.cells()["3c78effc"]
        self.assertIn("from notebook_support.gateway import gateway_infrastructure_status", source)
        self.assertIn("gateway_status = gateway_infrastructure_status(build_info)", source)
        for label in ("LiteLLM gateway tracing", "OTEL Collector", "Neon DB"):
            self.assertIn(label, source)
        self.assertIn("{gateway_trace_html}", source)

    def test_validation_reads_and_reports_the_gateway_view(self):
        source = self.cells()["6e3dcab6"]
        self.assertIn('"end_to_end", "gateway", "runs_trend"', source)
        self.assertIn("if agent_runtime.gateway is not None:", source)
        self.assertIn("observability_results['gateway']", source)

    def test_deployment_table_lists_gateway_rows_and_flags_attention(self):
        source = self.cells()["e1b420fd"]
        self.assertIn("from notebook_support.gateway import gateway_infrastructure_status", source)
        self.assertIn("gateway_status = gateway_infrastructure_status(build_info)", source)
        for label, key in (
            ("🚦 LiteLLM Gateway", "litellm"), ("🔭 OTEL Collector", "otel_collector"), ("🐘 Neon DB", "neon"),
        ):
            self.assertRegex(source, rf'\("{label}[^"]*", gateway_status\["{key}"\]\),')
        for attention, headline in ((True, "needs attention"), (False, "all green")):
            with self.subTest(attention=attention):
                litellm = "❌ Unreachable at http://127.0.0.1:4000; run gateway/start.sh" if attention else "✅ Ready"
                namespace = {
                    "build_info_path": Path("build_info-test.json"),
                    "gateway_infrastructure_status": lambda _build_info: {
                        "litellm": litellm, "otel_collector": "✅ Running", "neon": "✅ Connected",
                    },
                }
                with patch.object(validation_tests, "NOTEBOOK", NOTEBOOK):
                    scope = validation_tests.load_functions("e1b420fd", ["show_current_build_status"], namespace)
                output = io.StringIO()
                build = {
                    "rg": "rg", "storage_account": "sa", "key_vault": "kv", "appinsights": "ai",
                    "foundry_name": "foundry", "foundry_project_name": "project", "genai_model": "model",
                    "foundry_project_endpoint": "https://example.invalid/project",
                    "azure_openai_endpoint": "https://example.invalid/openai",
                }
                with redirect_stdout(output):
                    scope["show_current_build_status"](build)
                self.assertIn(headline, output.getvalue())
                for label in ("🚦 LiteLLM Gateway", "🔭 OTEL Collector", "🐘 Neon DB"):
                    self.assertIn(label, output.getvalue())

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
