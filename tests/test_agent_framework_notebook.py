import ast
import json
import unittest
from html import escape
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK_PATH = ROOT / "agent-framework-demo" / "zolab-agent-framework-sdk-win11.ipynb"
REQUIREMENTS_PATH = ROOT / "agent-framework-demo" / "requirements.txt"
MCP_HELPER_PATH = ROOT / "agent-framework-demo" / "agent_framework_menu_mcp_server.py"

EXPECTED_PINS = {
    "agent-framework-core": "1.18.0",
    "agent-framework-openai": "1.14.3",
    "agent-framework-orchestrations": "1.1.1",
    "anyio": "4.15.1",
    "azure-identity": "1.25.3",
    "httpx2": "2.12.0",
    "ipykernel": "7.3.0",
    "mcp": "1.30.0",
    "openai": "3.13.0",
    "opentelemetry-api": "1.44.0",
    "opentelemetry-exporter-otlp-proto-grpc": "1.44.0",
    "opentelemetry-sdk": "1.44.0",
    "pydantic": "2.13.5",
}


def load_notebook():
    return json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))


def cells_by_id(notebook):
    return {cell["id"]: "".join(cell.get("source", [])) for cell in notebook["cells"]}


class DependencyTests(unittest.TestCase):
    def test_requirements_are_exact_current_pins(self):
        pins = {}
        for raw_line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith(("-c ", "--constraint ")):
                continue
            self.assertEqual(line.count("=="), 1, line)
            name, pinned_version = line.split("==", 1)
            pins[name] = pinned_version
        self.assertEqual(pins, EXPECTED_PINS)

    def test_selected_environment_matches_inventory_pins(self):
        self.assertEqual(
            {name: version(name) for name in EXPECTED_PINS},
            EXPECTED_PINS,
        )


class NotebookStructureTests(unittest.TestCase):
    def setUp(self):
        self.notebook = load_notebook()
        self.cells = cells_by_id(self.notebook)

    def test_notebook_is_valid_json_with_clean_outputs_and_python_cells(self):
        self.assertEqual(self.notebook["nbformat"], 4)
        for index, cell in enumerate(self.notebook["cells"], start=1):
            if cell["cell_type"] != "code":
                continue
            self.assertEqual(cell.get("outputs"), [], f"cell {index}")
            self.assertIsNone(cell.get("execution_count"), f"cell {index}")
            compile(
                "".join(cell["source"]),
                f"{NOTEBOOK_PATH}#cell-{index}",
                "exec",
                flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
            )

    def test_core_demo_sequence_is_preserved(self):
        ordered_ids = [cell["id"] for cell in self.notebook["cells"]]
        core_ids = [
            "40b63ed3",
            "e6f2c7e3",
            "a729c275",
            "8f566d58",
            "cc566261",
            "50f0df38",
            "7fd8a171",
            "8617c67b",
            "0a79b228",
            "91a844a6",
            "0f3f5f40",
            "4dd2f39c",
            "5d0a3908",
            "ac926c91",
            "81348edc",
            "9be200c9",
        ]
        self.assertEqual(
            [ordered_ids.index(cell_id) for cell_id in core_ids],
            sorted(ordered_ids.index(cell_id) for cell_id in core_ids),
        )

    def test_install_and_inventory_cells_use_the_pinned_manifest(self):
        install_source = self.cells["a729c275"]
        inventory_source = self.cells["f9e4a4c7"]
        self.assertIn("'requirements.txt'", install_source)
        self.assertIn("line.startswith(('-c ', '--constraint '))", inventory_source)
        self.assertIn("requirements_path = demo_dir / 'requirements.txt'", inventory_source)
        self.assertIn("'--upgrade-strategy'", install_source)
        self.assertNotIn("--pre", install_source)
        self.assertIn("PackageNotFoundError", inventory_source)
        self.assertIn("version_mismatches", inventory_source)
        self.assertIn("globals()['package_inventory']", inventory_source)
        self.assertIn("Run the package installation cell immediately above", inventory_source)

    def test_inventory_reports_installed_versions_without_prior_cell_state(self):
        source = self.cells["f9e4a4c7"]
        scope = {}
        rendered = []
        with patch("IPython.display.display", side_effect=rendered.append):
            exec(compile(source, str(NOTEBOOK_PATH), "exec"), scope)
        self.assertEqual(scope["package_inventory"]["packages"], EXPECTED_PINS)
        html = "\n".join(item.data for item in rendered)
        self.assertIn("OK", html)
        self.assertIn("#2EA043", html)
        self.assertIn("Pins verified", html)
        self.assertIn("Agent Framework", html)

    def test_inventory_instructs_install_on_mismatch_and_clears_stale_state(self):
        source = self.cells["f9e4a4c7"]
        scope = {"package_inventory": {"stale": True}}
        rendered = []
        with patch("importlib.metadata.version", return_value="0.0.0"):
            with patch("IPython.display.display", side_effect=rendered.append):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "Run the package installation cell immediately above",
                ):
                    exec(compile(source, str(NOTEBOOK_PATH), "exec"), scope)
        self.assertNotIn("package_inventory", scope)
        html = "\n".join(item.data for item in rendered)
        self.assertIn("MISMATCH", html)
        self.assertIn("0.0.0", html)
        self.assertIn("#D83B01", html)

    def test_observability_banner_uses_semantic_status_colors(self):
        source = self.cells["50f0df38"]
        tree = ast.parse(source)
        start = next(
            index
            for index, node in enumerate(tree.body)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "accent_style" for target in node.targets)
        )
        rendered = []
        scope = {
            "demo_palette": {
                "accent": "#C239B3",
                "blue": "#0078D4",
                "enabled": "#2EA043",
                "disabled": "#D83B01",
                "rust": "var(--vscode-debugTokenExpression-string, #A64B2A)",
                "muted": "var(--vscode-descriptionForeground, #666666)",
            },
            "demo_text": lambda value, tone=None, bold=True: (
                escape(str(value))
                if tone is None
                else (
                    f'<span style="color: {scope["demo_palette"][tone]}; '
                    f'font-weight: {"700" if bold else "400"};">{escape(str(value))}</span>'
                )
            ),
            "demo_status": lambda value, enabled=None: (
                f'<span style="color: {scope["demo_palette"]["enabled" if enabled else "disabled"]}; '
                f'font-weight: 700;">{escape(str(value))}</span>'
            ),
            "escape": escape,
            "display": rendered.append,
            "HTML": lambda value: value,
            "service_name": "service",
            "service_version": "2026.09.17",
            "otel_endpoint": "http://localhost:4317",
            "telemetry_session_id": "session-id",
            "otel_provider_state": "Initialized",
            "capture_prompt_content": True,
            "console_exporters_enabled": False,
            "message_events_enabled": False,
            "root_debug_enabled": False,
            "package_inventory": {
                "packages": {
                    **EXPECTED_PINS,
                    "agent-framework-core": "1.18.0",
                },
            },
        }
        exec(
            compile(ast.Module(body=tree.body[start:], type_ignores=[]), str(NOTEBOOK_PATH), "exec"),
            scope,
        )
        self.assertEqual(len(rendered), 1)
        html = rendered[0]
        self.assertIn('color: #2EA043; font-weight: 700;">Enabled', html)
        self.assertIn('color: #D83B01; font-weight: 700;">Disabled', html)
        self.assertIn(
            'color: var(--vscode-debugTokenExpression-string, #A64B2A); '
            'font-weight: 700;">session-id',
            html,
        )
        self.assertIn('color: #2EA043; font-weight: 700;">13</span> pins verified', html)
        self.assertIn('Agent Framework <span style="color: #2EA043; font-weight: 700;">1.18.0', html)

    def test_major_runtime_summaries_use_shared_color_renderer(self):
        for cell_id in (
            "8f566d58",
            "cc566261",
            "7fd8a171",
            "8617c67b",
            "0a79b228",
            "91a844a6",
            "0f3f5f40",
            "4dd2f39c",
            "aa785fd2",
            "5d0a3908",
            "ac926c91",
            "81348edc",
            "9be200c9",
        ):
            with self.subTest(cell=cell_id):
                self.assertIn("display_demo_panel(", self.cells[cell_id])

    def test_markdown_documents_the_notebook_color_semantics(self):
        markdown = "\n".join(
            "".join(cell.get("source", []))
            for cell in self.notebook["cells"]
            if cell["cell_type"] == "markdown"
        )
        for expected in (
            "#2EA043",
            "#D83B01",
            "#0078D4",
            "#C239B3",
            "--vscode-debugTokenExpression-string",
            "enabled / ready / successful",
            "disabled / missing / action required",
        ):
            self.assertIn(expected, markdown)

    def test_notebook_uses_an_independent_virtual_environment(self):
        bootstrap_source = self.cells["40b63ed3"]
        kernel_check_source = self.cells["e6f2c7e3"]
        install_source = self.cells["a729c275"]
        for source in (bootstrap_source, kernel_check_source):
            self.assertIn("venv_dir = demo_dir / '.venv'", source)
            self.assertNotIn("venv_dir = repo_root / '.venv'", source)
        self.assertIn("if not venv_python.is_file():", bootstrap_source)
        self.assertIn("Select Another Kernel", bootstrap_source)
        self.assertIn("Select Another Kernel", kernel_check_source)
        self.assertIn("requirements_path = demo_dir / 'requirements.txt'", install_source)
        self.assertEqual(
            self.notebook["metadata"]["kernelspec"],
            {
                "display_name": "Agent Framework SDK Demo (.venv)",
                "language": "python",
                "name": "agent-framework-sdk-demo",
            },
        )

    def test_observability_controls_are_explicit_and_safe_to_rerun(self):
        source = self.cells["50f0df38"]
        self.assertIn("read_bool_env('AGENT_DEMO_CAPTURE_CONTENT', True)", source)
        self.assertIn("read_bool_env('AGENT_DEMO_ROOT_DEBUG', False)", source)
        self.assertIn("read_bool_env('AGENT_DEMO_MESSAGE_EVENTS', False)", source)
        self.assertIn("not bool(otel_endpoint)", source)
        self.assertIn("service_version = '2026.09.17'", source)
        self.assertIn("otlp_protocol='grpc' if otel_endpoint else None", source)
        self.assertIn("otel_semconv_stability_opt_in='gen_ai_latest_experimental'", source)
        self.assertIn("Restart the kernel", source)
        self.assertNotIn("logging.NOTSET", source)

    def test_group_chat_uses_the_maf_118_output_api(self):
        source = self.cells["0f3f5f40"]
        self.assertIn("output_from='all'", source)
        self.assertNotIn("intermediate_outputs=", source)

    def test_prompt_content_is_recorded_only_when_enabled(self):
        for cell_id, attribute in (
            ("8617c67b", "demo.prompt"),
            ("4dd2f39c", "demo.workflow.task"),
        ):
            source = self.cells[cell_id]
            self.assertIn("if capture_prompt_content:", source)
            self.assertIn(f"span.set_attribute('{attribute}'", source)
            self.assertIn(f"'{attribute}.sha256'", source)

    def test_mcp_stdio_and_production_recommendations_are_illuminated(self):
        generated_source = self.cells["0a79b228"]
        markdown = "\n".join(
            "".join(cell.get("source", []))
            for cell in self.notebook["cells"]
            if cell["cell_type"] == "markdown"
        )
        self.assertIn("configure_otel_providers(", generated_source)
        self.assertIn("file=sys.stderr", generated_source)
        self.assertIn("stdout remains reserved for the protocol", generated_source)
        for recommendation in (
            "Separate demo from runtime",
            "Use workload identity in Azure",
            "OpenTelemetry Collector gateway",
            "Make workflow execution durable",
            "Govern captured content",
        ):
            self.assertIn(recommendation, markdown)

    def test_flush_gate_is_present(self):
        source = self.cells["aa785fd2"]
        self.assertIn("force_flush(timeout_millis=30000)", source)
        self.assertIn("service.instance.id", source)

    def test_cleanup_stops_telemetry_before_removing_aspire(self):
        ordered_ids = [cell["id"] for cell in self.notebook["cells"]]
        cleanup_ids = ["5d0a3908", "ac926c91", "81348edc", "9be200c9"]
        self.assertEqual(
            [ordered_ids.index(cell_id) for cell_id in cleanup_ids],
            sorted(ordered_ids.index(cell_id) for cell_id in cleanup_ids),
        )
        shutdown_source = self.cells["ac926c91"]
        for expected in (
            "metrics.get_meter_provider()",
            "trace.get_tracer_provider()",
            "get_logger_provider()",
            "local_otlp_receiver_available",
            "force_flush(timeout_millis=10000)",
            "shutdown()",
            "['_agent_framework_otel_shutdown'] = True",
        ):
            self.assertIn(expected, shutdown_source)
        self.assertIn("Run the OpenTelemetry shutdown cell", self.cells["81348edc"])
        self.assertIn("Run the OpenTelemetry shutdown cell", self.cells["9be200c9"])


class McpHelperTests(unittest.TestCase):
    def test_checked_in_helper_matches_observability_contract(self):
        source = MCP_HELPER_PATH.read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn("configure_otel_providers(", source)
        self.assertIn("SERVICE_VERSION = '2026.09.17'", source)
        self.assertIn("file=sys.stderr", source)
        self.assertIn("enable_console_exporters=False", source)
        self.assertIn('os.environ.get("AZURE_OPENAI_ENDPOINT"', source)
        self.assertNotIn("cognitiveservices.azure.com", source)
        self.assertNotIn('print(f"Starting MCP agent revision: {AGENT_SPEC_REVISION}")', source)


if __name__ == "__main__":
    unittest.main()
