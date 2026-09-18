import ast
import asyncio
import importlib.util
import io
import json
import os
import subprocess
import sys
import sysconfig
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryFile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agent_framework import Content, MCPStdioTool
from mcp import StdioServerParameters, types
from mcp.server.lowlevel import Server
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "agent-framework-demo"
NOTEBOOK_PATH = DEMO_DIR / "zolab-agent-framework-sdk-win11.ipynb"
HELPER_PATH = DEMO_DIR / "agent_framework_menu_mcp_server.py"
VERIFICATION_CELL_ID = "2c86be91"

helper_spec = importlib.util.spec_from_file_location("menu_mcp_test_helper", HELPER_PATH)
if helper_spec is None or helper_spec.loader is None:
    raise RuntimeError(f"Cannot load the MCP helper: {HELPER_PATH}")
helper = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(helper)


def notebook_cells():
    notebook = json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))
    return {cell["id"]: "".join(cell["source"]) for cell in notebook["cells"]}


class McpTelemetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.addCleanup(self.provider.shutdown)
        self.tracer = self.provider.get_tracer("mcp-test")

    async def invoke_handler(self, handler):
        server = Server("menu-test")
        server.request_handlers[types.CallToolRequest] = handler
        request = types.CallToolRequest.model_validate(
            {
                "method": "tools/call",
                "params": {
                    "name": "RestaurantAgent",
                    "arguments": {"task": "List the specials."},
                    "_meta": {
                        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
                    },
                },
            }
        )
        with (
            patch.object(helper.trace, "get_tracer", return_value=self.tracer),
            patch.object(helper, "finish_telemetry") as finish,
        ):
            helper.instrument_mcp_server(server)
            result = await server.request_handlers[types.CallToolRequest](request)
            finish.assert_called_once_with()
        return result

    async def test_server_joins_incoming_trace_and_flushes_after_span_finishes(self):
        response = types.ServerResult(types.CallToolResult(content=[]))
        active_spans = []

        async def handler(request):
            active_spans.append(trace.get_current_span())
            return response

        result = await self.invoke_handler(handler)

        self.assertIs(result, response)
        span, = self.exporter.get_finished_spans()
        self.assertEqual(span.context.trace_id, int("0123456789abcdef" * 2, 16))
        self.assertEqual(span.parent.span_id, int("0123456789abcdef", 16))
        self.assertEqual(span.kind, trace.SpanKind.SERVER)
        self.assertEqual(span.attributes["gen_ai.tool.name"], "RestaurantAgent")
        self.assertFalse(active_spans[0].is_recording())
        self.assertFalse(trace.get_current_span().get_span_context().is_valid)

    async def test_mcp_error_result_marks_server_span_failed(self):
        response = types.ServerResult(
            types.CallToolResult(
                content=[types.TextContent(type="text", text="Menu lookup failed.")],
                isError=True,
            )
        )

        async def handler(request):
            return response

        self.assertIs(await self.invoke_handler(handler), response)
        span, = self.exporter.get_finished_spans()
        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)

    async def test_handler_exception_is_not_hidden(self):
        async def handler(request):
            raise ValueError("menu failure")

        with self.assertRaisesRegex(ValueError, "menu failure"):
            await self.invoke_handler(handler)
        span, = self.exporter.get_finished_spans()
        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)

    async def test_no_destination_mode_does_not_flush_noop_providers(self):
        server = Server("offline-test")
        response = types.ServerResult(types.CallToolResult(content=[]))

        async def handler(request):
            return response

        server.request_handlers[types.CallToolRequest] = handler
        request = types.CallToolRequest.model_validate(
            {"method": "tools/call", "params": {"name": "RestaurantAgent", "arguments": {}}}
        )
        with (
            patch.object(
                helper.trace, "get_tracer",
                return_value=trace.NoOpTracerProvider().get_tracer("offline"),
            ),
            patch.object(helper, "finish_telemetry") as finish,
        ):
            helper.instrument_mcp_server(server, export_enabled=False)
            result = await server.request_handlers[types.CallToolRequest](request)
        self.assertIs(result, response)
        finish.assert_not_called()

    def test_flush_attempts_all_signals_and_reports_failure(self):
        providers = [Mock(), Mock(), Mock()]
        providers[0].force_flush.return_value = False
        providers[1].force_flush.side_effect = RuntimeError("trace flush failed")
        providers[2].force_flush.return_value = True
        with (
            patch.object(helper.metrics, "get_meter_provider", return_value=providers[0]),
            patch.object(helper.trace, "get_tracer_provider", return_value=providers[1]),
            patch.object(helper, "get_logger_provider", return_value=providers[2]),
        ):
            with self.assertRaisesRegex(RuntimeError, "metrics.*traces"):
                helper.finish_telemetry()
        for provider in providers:
            provider.force_flush.assert_called_once_with(timeout_millis=10000)

    def test_shutdown_attempts_all_signals_and_reports_failure(self):
        providers = [Mock(), Mock(), Mock()]
        providers[0].shutdown.side_effect = RuntimeError("metric shutdown failed")
        with (
            patch.object(helper.metrics, "get_meter_provider", return_value=providers[0]),
            patch.object(helper.trace, "get_tracer_provider", return_value=providers[1]),
            patch.object(helper, "get_logger_provider", return_value=providers[2]),
        ):
            with self.assertRaisesRegex(RuntimeError, "metrics"):
                helper.finish_telemetry(shutdown=True)
        for provider in providers:
            provider.shutdown.assert_called_once_with()


class McpVerificationCellTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = notebook_cells()[VERIFICATION_CELL_ID]
        self.code = compile(
            self.source, str(NOTEBOOK_PATH), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        )
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.addCleanup(self.provider.shutdown)
        self.answer = "Clam Chowder; Cobb Salad; Chai Tea. Clam Chowder: $9.99."
        self.client = SimpleNamespace(
            is_connected=True,
            call_tool=AsyncMock(return_value=[Content.from_text(self.answer)]),
        )
        self.panels = []
        self.scope = {
            "mcp_client": self.client,
            "tracer": self.provider.get_tracer("notebook-mcp-test"),
            "telemetry_session_id": "test-session",
            "capture_prompt_content": False,
            "restaurant_agent_revision": "test-revision",
            "menu_specials_text": (
                "Special Soup: Clam Chowder\nSpecial Salad: Cobb Salad\nSpecial Drink: Chai Tea"
            ),
            "restaurant_agent_spec": {"fixed_price": "$9.99"},
            "_agent_framework_otel_initialized": True,
            "_agent_framework_otel_shutdown": False,
            "demo_text": lambda value, *args, **kwargs: str(value),
            "demo_status": lambda value, **kwargs: str(value),
            "display_demo_panel": lambda *args: self.panels.append(args),
        }

    async def run_cell(self):
        with patch("opentelemetry.trace.get_tracer_provider", return_value=self.provider):
            await eval(self.code, self.scope)

    async def test_real_mcp_tool_is_called_and_response_is_published(self):
        await self.run_cell()
        self.client.call_tool.assert_awaited_once_with(
            "RestaurantAgent", task=self.scope["mcp_verification_prompt"]
        )
        self.assertEqual(self.scope["mcp_verification_response"], self.answer)
        self.assertEqual(len(self.scope["mcp_verification_trace_id"]), 32)
        self.assertTrue(self.panels)
        span, = self.exporter.get_finished_spans()
        self.assertEqual(span.name, "agent_framework.mcp_verification")
        self.assertNotIn("demo.mcp.prompt", span.attributes)
        self.assertNotIn("demo.mcp.response", span.attributes)
        self.assertIn("demo.mcp.prompt.sha256", span.attributes)

    async def test_prompt_and_response_recording_respects_opt_in(self):
        self.scope["capture_prompt_content"] = True
        await self.run_cell()
        span, = self.exporter.get_finished_spans()
        self.assertEqual(span.attributes["demo.mcp.prompt"], self.scope["mcp_verification_prompt"])
        self.assertEqual(span.attributes["demo.mcp.response"], self.answer)

    async def test_missing_facts_fail_and_clear_stale_success(self):
        self.scope["mcp_verification_response"] = "stale answer"
        self.scope["mcp_verification_trace_id"] = "stale trace"
        self.client.call_tool.return_value = [Content.from_text("No menu information.")]
        with self.assertRaisesRegex(RuntimeError, "missing expected menu facts"):
            await self.run_cell()
        self.assertNotIn("mcp_verification_response", self.scope)
        self.assertNotIn("mcp_verification_trace_id", self.scope)
        self.assertFalse(self.panels)
        span, = self.exporter.get_finished_spans()
        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)

    async def test_disconnected_client_fails_before_invocation(self):
        self.client.is_connected = False
        with self.assertRaisesRegex(RuntimeError, "5.1"):
            await self.run_cell()
        self.client.call_tool.assert_not_awaited()

    async def test_tool_errors_are_propagated_without_success_panel(self):
        self.client.call_tool.side_effect = RuntimeError("MCP call failed")
        with self.assertRaisesRegex(RuntimeError, "MCP call failed"):
            await self.run_cell()
        self.assertNotIn("mcp_verification_response", self.scope)
        self.assertFalse(self.panels)

    async def test_slow_tool_is_bounded_by_the_call_timeout(self):
        real_timeout = asyncio.timeout

        async def slow_tool(*args, **kwargs):
            await asyncio.sleep(1)
            return [Content.from_text(self.answer)]

        self.client.call_tool.side_effect = slow_tool
        with patch("asyncio.timeout", side_effect=lambda seconds: real_timeout(0.01)):
            with self.assertRaises(TimeoutError):
                await self.run_cell()
        self.assertNotIn("mcp_verification_response", self.scope)
        self.assertFalse(self.panels)

    async def test_flush_failure_does_not_publish_success(self):
        failing_provider = Mock()
        failing_provider.force_flush.return_value = False
        with patch(
            "opentelemetry.trace.get_tracer_provider", return_value=failing_provider
        ):
            with self.assertRaisesRegex(RuntimeError, "did not flush"):
                await eval(self.code, self.scope)
        self.assertNotIn("mcp_verification_response", self.scope)
        self.assertFalse(self.panels)


class McpClientLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cells = notebook_cells()
        self.start_code = compile(
            cells["91a844a6"], str(NOTEBOOK_PATH), "exec",
            flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        )
        self.stop_code = compile(
            cells["5d0a3908"], str(NOTEBOOK_PATH), "exec",
            flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        )
        self.client = SimpleNamespace(
            is_connected=False,
            connect=AsyncMock(),
            close=AsyncMock(),
            functions=[SimpleNamespace(name="RestaurantAgent")],
        )

        async def connect():
            self.client.is_connected = True

        self.client.connect.side_effect = connect
        self.factory = Mock(return_value=self.client)
        factory = self.factory

        class FakeStdioTool:
            def __new__(cls, *args, **kwargs):
                return factory(*args, **kwargs)

        self.factory_type = FakeStdioTool
        self.scope = {
            "mcp_server_script": HELPER_PATH,
            "telemetry_session_id": "test-session",
            "_agent_framework_otel_initialized": True,
            "_agent_framework_otel_shutdown": False,
            "tracer": trace.NoOpTracerProvider().get_tracer("mcp-test"),
            "demo_text": lambda value, *args, **kwargs: str(value),
            "demo_status": lambda value, **kwargs: str(value),
            "display_demo_panel": Mock(),
        }

    async def test_connect_discovers_tool_and_reuses_same_client(self):
        with (
            patch("agent_framework.MCPStdioTool", self.factory_type),
            redirect_stdout(io.StringIO()),
        ):
            await eval(self.start_code, self.scope)
            await eval(self.start_code, self.scope)
        self.factory.assert_called_once()
        self.client.connect.assert_awaited_once()
        self.assertIs(self.scope["mcp_client"], self.client)
        self.assertEqual(self.scope["mcp_start_status"], "Reused")
        self.assertEqual(self.factory.call_args.kwargs["env"]["OTEL_SERVICE_INSTANCE_ID"], "test-session")
        self.assertFalse(self.factory.call_args.kwargs["load_prompts"])

    async def test_legacy_process_requires_cleanup_before_starting_another(self):
        self.scope["mcp_server_process"] = SimpleNamespace(poll=lambda: None)
        with patch("agent_framework.MCPStdioTool", self.factory_type):
            with self.assertRaisesRegex(RuntimeError, "cleanup step 7.1"):
                await eval(self.start_code, self.scope)
        self.factory.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows-only MCP transport")
    async def test_newly_installed_windows_package_paths_are_refreshed(self):
        with (
            patch("importlib.util.find_spec", side_effect=[None, Mock()]),
            patch("site.addsitedir") as refresh,
            patch("agent_framework.MCPStdioTool", self.factory_type),
            redirect_stdout(io.StringIO()),
        ):
            await eval(self.start_code, self.scope)
        refresh.assert_called_once_with(sysconfig.get_path("purelib"))
        self.client.connect.assert_awaited_once()

    @unittest.skipUnless(os.name == "nt", "Windows-only MCP transport")
    async def test_missing_windows_dependency_reports_setup_error(self):
        with (
            patch("importlib.util.find_spec", return_value=None),
            patch("site.addsitedir"),
            patch("agent_framework.MCPStdioTool", self.factory_type),
        ):
            with self.assertRaisesRegex(RuntimeError, "requires pywin32"):
                await eval(self.start_code, self.scope)
        self.factory.assert_not_called()

    async def test_missing_discovered_tool_closes_connection(self):
        self.client.functions = []
        with patch("agent_framework.MCPStdioTool", self.factory_type):
            with self.assertRaisesRegex(RuntimeError, "did not expose RestaurantAgent"):
                await eval(self.start_code, self.scope)
        self.client.close.assert_awaited_once()
        self.scope["display_demo_panel"].assert_not_called()

    async def test_managed_cleanup_is_idempotent(self):
        self.scope["mcp_client"] = self.client
        await eval(self.stop_code, self.scope)
        await eval(self.stop_code, self.scope)
        self.client.close.assert_awaited_once()
        self.assertIsNone(self.scope["mcp_client"])
        self.assertEqual(self.scope["mcp_cleanup_status"], "Already stopped")

    async def test_legacy_cleanup_waits_after_terminating_only_the_tracked_process(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("mcp", 5), 0]
        self.scope["mcp_server_process"] = process
        await eval(self.stop_code, self.scope)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close.assert_called_once()
        self.assertIsNone(self.scope["mcp_server_process"])


class McpNotebookTransportTests(unittest.IsolatedAsyncioTestCase):
    def client_class(self, transport_factory):
        tree = ast.parse(notebook_cells()["91a844a6"])
        definition = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "NotebookMCPStdioTool"
        )
        scope = {
            "MCPStdioTool": MCPStdioTool,
            "AsyncIterator": AsyncIterator,
            "asynccontextmanager": asynccontextmanager,
            "StdioServerParameters": StdioServerParameters,
            "stdio_client": transport_factory,
            "TemporaryFile": TemporaryFile,
            "sys": sys,
        }
        exec(compile(ast.Module(body=[definition], type_ignores=[]), str(NOTEBOOK_PATH), "exec"), scope)
        return scope["NotebookMCPStdioTool"]

    async def test_transport_uses_real_stderr_handle_and_replays_diagnostics(self):
        files = []

        @asynccontextmanager
        async def transport(parameters, *, errlog):
            self.assertGreaterEqual(errlog.fileno(), 0)
            self.assertEqual(parameters.command, sys.executable)
            files.append(errlog)
            errlog.write("server diagnostic")
            yield ("reader", "writer")

        client = self.client_class(transport)(name="test", command=sys.executable)
        with redirect_stderr(io.StringIO()) as output:
            async with client.get_mcp_client() as streams:
                self.assertEqual(streams, ("reader", "writer"))
        self.assertIn("server diagnostic", output.getvalue())
        self.assertTrue(files[0].closed)

    async def test_transport_failure_keeps_diagnostics_and_closes_file(self):
        files = []

        @asynccontextmanager
        async def transport(parameters, *, errlog):
            files.append(errlog)
            errlog.write("startup failed")
            raise OSError("spawn failed")
            yield

        client = self.client_class(transport)(name="test", command=sys.executable)
        with redirect_stderr(io.StringIO()) as output:
            with self.assertRaisesRegex(OSError, "spawn failed"):
                async with client.get_mcp_client():
                    self.fail("A failed transport cannot be used")
        self.assertIn("startup failed", output.getvalue())
        self.assertTrue(files[0].closed)


class McpGeneratedHelperTests(unittest.TestCase):
    def generated_scope(self):
        source = notebook_cells()["0a79b228"]
        tree = ast.parse(source)
        prefix = []
        for node in tree.body:
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "write_text"
            ):
                break
            prefix.append(node)
        else:
            self.fail("Notebook no longer writes its generated MCP helper")

        scope = {
            "model_name": "test-model",
            "capture_prompt_content": helper.CAPTURE_PROMPT_CONTENT,
            "message_events_enabled": helper.MESSAGE_EVENTS_ENABLED,
            "service_version": helper.SERVICE_VERSION,
        }
        exec(compile(ast.Module(body=prefix, type_ignores=[]), str(NOTEBOOK_PATH), "exec"), scope)
        return scope

    def test_notebook_generates_the_checked_in_helper_behavior(self):
        scope = self.generated_scope()
        generated_tree = ast.parse(scope["script_body"])
        checked_in_tree = ast.parse(HELPER_PATH.read_text(encoding="utf-8"))
        for candidate in (generated_tree, checked_in_tree):
            for node in candidate.body:
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "AGENT_SPEC_REVISION"
                ):
                    node.value = ast.Constant("revision-for-selected-model")
        self.assertEqual(ast.dump(generated_tree), ast.dump(checked_in_tree))

    def test_header_and_formatting_survive_notebook_regeneration(self):
        scope = self.generated_scope()
        generated = scope["script_body"].replace(
            f'AGENT_SPEC_REVISION = "{scope["restaurant_agent_revision"]}"',
            f'AGENT_SPEC_REVISION = "{helper.AGENT_SPEC_REVISION}"',
            1,
        )
        checked_in = HELPER_PATH.read_text(encoding="utf-8")
        self.assertEqual(generated, checked_in)
        header = checked_in.split("\nimport os\n", 1)[0]
        for label in (
            "File", "Author", "Purpose", "Description", "Usage",
            "Configuration", "Maintenance",
        ):
            self.assertIn(f"# {label}:", header)
        self.assertRegex(header, r"(?m)^# Updated: \d{4}-\d{2}-\d{2}$")
        for number, line in enumerate(checked_in.splitlines(), start=1):
            self.assertLessEqual(len(line), 88, f"helper line {number}")

    def test_literal_wrapping_preserves_exact_prompt_content(self):
        formatter = self.generated_scope()["format_python_string"]
        for value in (
            "",
            "Short text",
            "First line\n\nSecond line\n",
            "  Leading spaces\tand repeated   spaces\r\n",
            "Quotes: 'single' and \"double\"; path: C:\\demo\\menu",
            "\u03a9 and control characters: \x01",
            "unbroken-" + "x" * 150,
        ):
            with self.subTest(value=value):
                expression = "(\n" + formatter(value, 4) + "\n)"
                self.assertEqual(ast.literal_eval(expression), value)


if __name__ == "__main__":
    unittest.main()
