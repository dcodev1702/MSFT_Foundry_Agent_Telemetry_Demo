"""Dependency and installer contracts for the Foundry Windows notebook."""

import ast
import io
import json
import os
import subprocess
import time
import unittest
from contextlib import redirect_stdout
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx2
from azure.ai.projects import AIProjectClient
from azure.ai.projects.telemetry import AIProjectInstrumentor
from azure.core.credentials import AccessToken
from azure.core.settings import settings
from azure.core.tracing.ext.opentelemetry_span import OpenTelemetrySpan
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "zolab-ai-agent-demo-win11.ipynb"
RUNTIME = ROOT / "requirements-notebook.txt"
SHARED = ROOT / "requirements-notebook-shared.txt"
VALIDATION = ROOT / "requirements-notebook-validation.txt"
CONSTRAINTS = ROOT / "constraints-notebook-win11.txt"


def direct_pins(path):
    pins = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-c ", "-r ")):
            continue
        requirement = Requirement(line)
        specifiers = list(requirement.specifier)
        if len(specifiers) != 1 or specifiers[0].operator != "==":
            raise AssertionError(f"{path.name} must use exact pins: {line}")
        pins[canonicalize_name(requirement.name)] = specifiers[0].version
    return pins


def installation_source():
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    return "".join(
        next(cell["source"] for cell in notebook["cells"] if cell["id"] == "a2c70b8c")
    )


class NotebookDependencyTests(unittest.TestCase):
    def test_profiles_and_constraints_agree(self):
        constraints = direct_pins(CONSTRAINTS)
        for profile in (RUNTIME, SHARED, VALIDATION):
            for name, expected in direct_pins(profile).items():
                with self.subTest(profile=profile.name, package=name):
                    self.assertEqual(constraints[name], expected)
        self.assertIn("-c constraints-notebook-win11.txt", RUNTIME.read_text())
        self.assertIn("-r requirements-notebook.txt", SHARED.read_text())
        self.assertIn("-r requirements-notebook.txt", VALIDATION.read_text())

    def test_runtime_and_validation_pins_match_the_selected_environment(self):
        for profile in (RUNTIME, VALIDATION):
            for name, expected in direct_pins(profile).items():
                with self.subTest(profile=profile.name, package=name):
                    self.assertEqual(version(name), expected)

    def test_optional_shared_profile_matches_when_installed(self):
        try:
            version("agent-framework-openai")
        except PackageNotFoundError:
            self.skipTest("The optional shared MAF providers are not installed.")
        for name, expected in direct_pins(SHARED).items():
            with self.subTest(package=name):
                self.assertEqual(version(name), expected)

    def test_foundry_runtime_does_not_install_unrelated_agent_services(self):
        runtime = direct_pins(RUNTIME)
        self.assertEqual(runtime["azure-ai-projects"], "2.6.1")
        self.assertEqual(runtime["openai"], "3.16.1")
        self.assertEqual(runtime["httpx2"], "2.13.0")
        self.assertEqual(runtime["azure-identity"], "1.26.0b2")
        self.assertEqual(runtime["agent-framework-core"], "1.19.0")
        for name in ("agent-framework-openai", "agent-framework-orchestrations", "agent-framework-a2a", "a2a-sdk", "uvicorn"):
            self.assertNotIn(name, runtime)

    def test_standalone_overlap_remains_compatible_without_sharing_environments(self):
        standalone = direct_pins(ROOT / "agent-framework-demo" / "requirements.txt")
        shared = direct_pins(RUNTIME) | direct_pins(SHARED)
        for name in ("agent-framework-core", "agent-framework-openai", "agent-framework-orchestrations"):
            self.assertEqual(shared[name], standalone[name])
        self.assertEqual(direct_pins(RUNTIME)["httpx2"], standalone["httpx2"])
        self.assertNotIn("agent-framework-demo/.venv", installation_source())

    def test_github_source_manifest_pins_official_commits(self):
        manifest = json.loads((ROOT / "notebook-source-releases.json").read_text())
        repositories = {
            "https://github.com/microsoft/agent-framework.git",
            "https://github.com/Azure/azure-sdk-for-python.git",
            "https://github.com/openai/openai-python.git",
            "https://github.com/pydantic/httpx2.git",
            "https://github.com/jupyter/nbformat.git",
        }
        self.assertEqual({release["Repository"] for release in manifest["Releases"]}, repositories)
        for release in manifest["Releases"]:
            self.assertRegex(release["Commit"], r"^[0-9a-f]{40}$")
            self.assertTrue(release["Tag"])
            self.assertTrue(release["Projects"])
        for wheel in (
            "agent_framework_core-1.19.0-py3-none-any.whl",
            "azure_ai_projects-2.6.1-py3-none-any.whl",
            "openai-3.16.1-py3-none-any.whl",
            "httpx2-2.13.0-py3-none-any.whl",
            "nbformat-5.11.1-py3-none-any.whl",
        ):
            self.assertIn(wheel, manifest["ExpectedWheels"])


class NotebookInstallCellTests(unittest.TestCase):
    def setUp(self):
        self.code = compile(installation_source(), str(NOTEBOOK), "exec")

    def interpreter(self, root):
        return root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
            "python.exe" if os.name == "nt" else "python"
        )

    def execute(self, root, python, runner):
        with (
            patch("pathlib.Path.cwd", return_value=root),
            patch("sys.executable", str(python)),
            patch("subprocess.check_call", side_effect=runner),
            redirect_stdout(io.StringIO()),
        ):
            exec(self.code, {})

    def test_local_release_wheels_are_used_when_present(self):
        for with_wheels in (False, True):
            with self.subTest(with_wheels=with_wheels), TemporaryDirectory() as temp:
                root = Path(temp)
                requirements = root / "requirements-notebook.txt"
                requirements.write_text("# test fixture\n", encoding="utf-8")
                wheelhouse = root / ".wheels"
                if with_wheels:
                    wheelhouse.mkdir()
                commands = []
                python = self.interpreter(root)
                self.execute(root, python, commands.append)
                self.assertEqual(len(commands), 3)
                install = commands[1]
                self.assertEqual(install[0], str(python))
                self.assertIn(str(requirements), install)
                self.assertEqual("--find-links" in install, with_wheels)
                if with_wheels:
                    self.assertIn(str(wheelhouse), install)
                self.assertEqual(commands[2], [str(python), "-m", "pip", "check"])
                self.assertNotIn("--pre", install)
                self.assertNotIn("--index-url", install)
                self.assertNotIn("--trusted-host", install)

    def test_wrong_kernel_cannot_modify_another_environment(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "requirements-notebook.txt").touch()
            calls = []
            with self.assertRaisesRegex(RuntimeError, "kernel verification"):
                self.execute(root, root / "unrelated" / "python.exe", calls.append)
            self.assertEqual(calls, [])

    def test_missing_manifest_fails_before_installing(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            calls = []
            with self.assertRaisesRegex(FileNotFoundError, "repository root"):
                self.execute(root, self.interpreter(root), calls.append)
            self.assertEqual(calls, [])

    def test_install_failure_preserves_original_error_and_explains_recovery(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "requirements-notebook.txt").touch()
            commands = []

            def fail_install(command):
                commands.append(command)
                if "--requirement" in command:
                    raise subprocess.CalledProcessError(1, command)

            with self.assertRaisesRegex(RuntimeError, "build_notebook_wheels") as error:
                self.execute(root, self.interpreter(root), fail_install)
            self.assertIsInstance(error.exception.__cause__, subprocess.CalledProcessError)
            self.assertEqual(len(commands), 2)

    def test_all_notebook_cells_remain_compilable(self):
        notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                compile(
                    "".join(cell["source"]), cell["id"], "exec",
                    flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
                )


class FoundrySdkCompatibilityTests(unittest.TestCase):
    """Use real SDKs with an in-memory HTTP transport, never a cloud deployment."""

    def setUp(self):
        class TestCredential:
            def get_token(self, *scopes, **kwargs):
                return AccessToken("test-only-token", int(time.time()) + 3600)

        self.credential = TestCredential()
        self.endpoint = "https://example.services.ai.azure.com/api/projects/sdk-test"
        self.requests = []
        self.provider = TracerProvider()
        self.exporter = InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.addCleanup(self.provider.shutdown)

    def response_transport(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/conversations"):
            data = {"id": "conv_test", "object": "conversation", "created_at": 1}
        else:
            data = {
                "id": "resp_test", "object": "response", "created_at": 1,
                "status": "completed", "model": "sdk-test-model",
                "output": [{
                    "id": "msg_test", "type": "message", "role": "assistant",
                    "status": "completed",
                    "content": [{
                        "type": "output_text", "text": "SDK roundtrip passed",
                        "annotations": [],
                    }],
                }],
                "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            }
        return httpx2.Response(200, json=data, request=request)

    def test_responses_project_and_agent_routes_and_trace_propagation(self):
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
                instrumentor.instrument(
                    enable_content_recording=False, enable_trace_context_propagation=True
                )
                for agent_name in (None, "sdk-test-agent"):
                    with self.subTest(agent_name=agent_name):
                        transport = httpx2.Client(
                            transport=httpx2.MockTransport(self.response_transport)
                        )
                        with (
                            AIProjectClient(
                                endpoint=self.endpoint, credential=self.credential,
                                allow_preview=True,
                            ) as project,
                            project.get_openai_client(
                                agent_name=agent_name, http_client=transport
                            ) as client,
                        ):
                            with self.provider.get_tracer("sdk-test").start_as_current_span(
                                "notebook-request"
                            ) as root_span:
                                response = client.responses.create(
                                    model="sdk-test-model", input="Test requirement"
                                )
                            self.assertEqual(response.output_text, "SDK roundtrip passed")
                            expected_suffix = (
                                "/openai/v1/responses" if agent_name is None
                                else f"/agents/{agent_name}/endpoint/protocols/openai/responses"
                            )
                            request = self.requests[-1]
                            self.assertEqual(request.url.path, "/api/projects/sdk-test" + expected_suffix)
                            self.assertIn("traceparent", request.headers)
                            self.assertIn(
                                format(root_span.get_span_context().trace_id, "032x"),
                                request.headers["traceparent"],
                            )
                            self.assertEqual(json.loads(request.content)["input"], "Test requirement")
                spans = self.exporter.get_finished_spans()
                self.assertTrue(
                    any(span.attributes.get("gen_ai.operation.name") for span in spans),
                    "The installed Azure AI Projects/OpenAI pair must emit GenAI spans.",
                )
                for span in spans:
                    self.assertNotIn("Test requirement", str(span.attributes))
        finally:
            instrumentor.uninstrument()
            settings.tracing_implementation = previous_implementation
            settings.tracing_enabled = previous_enabled

    def test_conversations_api_works_through_updated_projects_client(self):
        with (
            AIProjectClient(
                endpoint=self.endpoint, credential=self.credential, allow_preview=True
            ) as project,
            project.get_openai_client(
                http_client=httpx2.Client(
                    transport=httpx2.MockTransport(self.response_transport)
                )
            ) as client,
        ):
            conversation = client.conversations.create()
        self.assertEqual(conversation.id, "conv_test")
        self.assertEqual(
            self.requests[-1].url.path, "/api/projects/sdk-test/openai/v1/conversations"
        )


if __name__ == "__main__":
    unittest.main()
