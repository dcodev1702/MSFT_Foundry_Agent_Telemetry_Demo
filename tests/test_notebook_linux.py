"""Ubuntu setup, isolation, and dependency contracts for the Linux notebook."""

import ast
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import nbformat

from test_notebook_dependencies import direct_pins


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "zolab-ai-agent-demo-linux.ipynb"
REQUIREMENTS = ROOT / "requirements"


def notebook_cells():
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    return {cell["id"]: cell for cell in notebook["cells"]}


def cell_source(cell_id):
    return "".join(notebook_cells()[cell_id]["source"])


class LinuxNotebookTests(unittest.TestCase):
    def test_notebook_schema_and_code_cells_are_valid_and_unexecuted(self):
        notebook = nbformat.read(NOTEBOOK, as_version=4)
        nbformat.validate(notebook)
        self.assertEqual(notebook.metadata.kernelspec.name, "ai-agent-demo-linux")
        self.assertEqual(
            notebook.metadata.kernelspec.display_name,
            "AI Agent Demo (Linux, Python 3.14.7)",
        )
        for cell in notebook.cells:
            if cell.cell_type == "code":
                with self.subTest(cell=cell.id):
                    compile(cell.source, cell.id, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                    self.assertEqual(cell.outputs, [])
                    self.assertIsNone(cell.execution_count)

    def test_foundry_workflows_and_telemetry_keep_the_original_behavior(self):
        original = json.loads((ROOT / "zolab-ai-agent-demo-win11.ipynb").read_text(encoding="utf-8"))
        linux = notebook_cells()
        platform_cells = {"fcc00444", "04fb2ced", "a2c70b8c", "8b1659dd", "8330c10b"}
        self.assertEqual(set(linux), {cell["id"] for cell in original["cells"]})
        for cell in original["cells"]:
            if cell["cell_type"] == "code" and cell["id"] not in platform_cells:
                with self.subTest(cell=cell["id"]):
                    if cell["id"] == "586f0511":
                        # Prompts are editable independently; preserve agent preparation calls.
                        sources = ("".join(cell["source"]), "".join(linux[cell["id"]]["source"]))
                        calls = [
                            [ast.dump(node) for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)]
                            for source in sources
                        ]
                        self.assertEqual(calls[0], calls[1])
                    else:
                        self.assertEqual(linux[cell["id"]]["source"], cell["source"])

    def test_profiles_are_exact_and_use_linux_constraints_only(self):
        constraints = direct_pins(REQUIREMENTS / "constraints-notebook-linux.txt")
        runtime = REQUIREMENTS / "requirements-notebook-linux.txt"
        validation = REQUIREMENTS / "requirements-notebook-linux-validation.txt"
        self.assertIn("-c constraints-notebook-linux.txt", runtime.read_text(encoding="utf-8"))
        self.assertIn("-r requirements-notebook-linux.txt", validation.read_text(encoding="utf-8"))
        for profile in (runtime, validation):
            self.assertNotIn("win11", profile.read_text(encoding="utf-8"))
            for name, expected in direct_pins(profile).items():
                with self.subTest(profile=profile.name, package=name):
                    self.assertEqual(constraints[name], expected)
        self.assertIn("pexpect", constraints)
        self.assertIn("ptyprocess", constraints)
        for name in ("pywin32", "colorama", "appnope"):
            self.assertNotIn(name, constraints)
        for name in ("agent-framework-openai", "agent-framework-a2a", "a2a-sdk"):
            self.assertNotIn(name, direct_pins(runtime))

    @unittest.skipUnless(sys.platform == "linux", "Linux dependency snapshot")
    def test_installed_packages_match_every_linux_constraint(self):
        for name, expected in direct_pins(REQUIREMENTS / "constraints-notebook-linux.txt").items():
            with self.subTest(package=name):
                self.assertEqual(version(name), expected)

    def test_setup_has_no_windows_installer_or_global_python_override(self):
        source = "\n".join(cell_source(cell_id) for cell_id in ("fcc00444", "04fb2ced", "a2c70b8c", "8330c10b"))
        for forbidden in ("winget", "az.cmd", "Scripts/python.exe", "build_notebook_wheels.ps1", "--break-system-packages", "--trusted-host"):
            self.assertNotIn(forbidden, source)

    def test_cli_extensions_are_documented_separately_from_python_dependencies(self):
        sources = (cell_source("0b2957a9"), (ROOT / "README.md").read_text(encoding="utf-8"))
        for source in sources:
            for extension, release, preview in (
                ("log-analytics", "1.0.0b2", "true"),
                ("azure-devops", "1.0.8", "false"),
            ):
                self.assertIn(
                    f"az extension add --name {extension} --version {release} --allow-preview {preview} --yes",
                    source,
                )
            self.assertIn("az monitor log-analytics query --help", source)
            self.assertIn("az devops project list --help", source)
            self.assertNotIn("az extension add --name kql", source)
        for name in ("requirements-notebook-linux.txt", "constraints-notebook-linux.txt"):
            pins = direct_pins(REQUIREMENTS / name)
            for extension in ("azure-cli", "log-analytics", "azure-devops"):
                self.assertNotIn(extension, pins)


class LinuxImportOutputTests(unittest.TestCase):
    def test_reports_distro_release_and_architecture_without_changing_existing_output(self):
        source = compile(cell_source("8b1659dd"), str(NOTEBOOK), "exec")
        for name, release, machine, expected_architecture in (
            ("Ubuntu", "26.04", "x86_64", "x64"),
            ("Debian GNU/Linux", "13", "aarch64", "arm64"),
            ("Fedora Linux", "44", "ppc64le", "ppc64le"),
        ):
            with self.subTest(distro=name, machine=machine):
                output = io.StringIO()
                with (
                    patch("platform.freedesktop_os_release", return_value={"NAME": name, "VERSION_ID": release}),
                    patch("platform.machine", return_value=machine),
                    patch("platform.system", return_value="Linux"),
                    patch("platform.release", return_value="test-kernel"),
                    redirect_stdout(output),
                ):
                    exec(source, {})
                text = output.getvalue()
                distro_lines = [line.split("Distro:", 1)[1].strip() for line in text.splitlines() if "Distro:" in line]
                self.assertEqual(distro_lines, [f"{name} {release} - {expected_architecture}"])
                self.assertIn("All libraries imported successfully", text)
                self.assertIn("Runtime:  MSFT Agent Framework workflows + Azure AI Projects + Responses API", text)
                self.assertIn("Platform: Linux test-kernel", text)
                self.assertIn(f"Python:   {sys.version.split()[0]}", text)

    def test_imports_remain_unchanged(self):
        original = json.loads((ROOT / "zolab-ai-agent-demo-win11.ipynb").read_text(encoding="utf-8"))
        original_source = next("".join(cell["source"]) for cell in original["cells"] if cell["id"] == "8b1659dd")
        imports = [
            [ast.dump(node) for node in ast.parse(source).body if isinstance(node, (ast.Import, ast.ImportFrom))]
            for source in (original_source, cell_source("8b1659dd"))
        ]
        self.assertEqual(imports[0], imports[1])


class LinuxEnvironmentCellTests(unittest.TestCase):
    def execute(
        self, cell_id, root, *, prefix=None, runner=None, probe=None,
        uv="/usr/bin/uv", python_version=(3, 14, 7), platform_name="linux",
    ):
        environment = {"python": [3, 14, 7], "prefix": str(root / ".venv-linux"), "platform": "linux"}
        with (
            patch("pathlib.Path.cwd", return_value=root),
            patch("sys.platform", platform_name),
            patch("sys.version_info", python_version),
            patch("sys.prefix", str(prefix or root / ".venv-linux")),
            patch("sys.executable", str(root / ".venv-linux" / "bin" / "python")),
            patch("shutil.which", return_value=uv),
            patch("subprocess.check_call", side_effect=runner),
            patch("subprocess.check_output", return_value=json.dumps(probe or environment)),
            redirect_stdout(io.StringIO()),
        ):
            exec(compile(cell_source(cell_id), str(NOTEBOOK), "exec"), {})

    def make_root(self, directory, *, existing=False):
        root = Path(directory)
        (root / "requirements").mkdir()
        for name in ("requirements-notebook-linux.txt", "constraints-notebook-linux.txt"):
            (root / "requirements" / name).write_text("# fixture\n", encoding="utf-8")
        if existing:
            (root / ".venv-linux" / "bin").mkdir(parents=True)
            (root / ".venv-linux" / "bin" / "python").touch()
            (root / ".venv-linux" / "pyvenv.cfg").touch()
        return root

    def test_bootstrap_uses_uv_without_system_pip_and_registers_distinct_kernel(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            commands = []
            self.execute("fcc00444", root, runner=commands.append, python_version=(3, 14, 4))
            self.assertEqual(commands[0][:3], ["/usr/bin/uv", "python", "install"])
            self.assertIn("--no-bin", commands[0])
            self.assertEqual(commands[0][-1], "3.14.7")
            self.assertEqual(commands[1][:2], ["/usr/bin/uv", "venv"])
            self.assertIn("--seed", commands[1])
            self.assertIn("--no-python-downloads", commands[1])
            self.assertIn("3.14.7", commands[1])
            self.assertEqual(commands[1][-1], str(root / ".venv-linux"))
            install = commands[2]
            self.assertIn(str(root / "requirements" / "constraints-notebook-linux.txt"), install)
            self.assertEqual(install[-2:], ["pip", "ipykernel"])
            self.assertEqual(commands[3][-2:], ["pip", "check"])
            self.assertIn("ai-agent-demo-linux", commands[4])
            self.assertNotIn("--clear", commands[1])

    def test_bootstrap_reuses_existing_environment_without_uv_or_recreation(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory, existing=True)
            commands = []
            self.execute("fcc00444", root, runner=commands.append, uv=None)
            self.assertEqual(len(commands), 3)
            self.assertTrue(all("venv" not in command for command in commands))

    def test_invalid_or_wrong_version_environment_cannot_be_overwritten(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            (root / ".venv-linux").mkdir()
            commands = []
            with self.assertRaisesRegex(RuntimeError, "not a usable Linux virtual environment"):
                self.execute("fcc00444", root, runner=commands.append)
            self.assertEqual(commands, [])
        with TemporaryDirectory() as directory:
            root = self.make_root(directory, existing=True)
            commands = []
            probe = {"python": [3, 14, 4], "prefix": str(root / ".venv-linux"), "platform": "linux"}
            with self.assertRaisesRegex(RuntimeError, "must be a Linux Python 3.14.7"):
                self.execute("fcc00444", root, runner=commands.append, probe=probe)
            self.assertEqual(commands, [])

    def test_missing_uv_or_repository_fails_before_installation(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            commands = []
            with self.assertRaisesRegex(RuntimeError, "Install uv"):
                self.execute("fcc00444", root, runner=commands.append, uv=None)
            self.assertEqual(commands, [])
        for cell_id in ("fcc00444", "a2c70b8c"):
            with self.subTest(cell=cell_id), TemporaryDirectory() as directory:
                commands = []
                with self.assertRaisesRegex(FileNotFoundError, "repository root"):
                    self.execute(cell_id, Path(directory), runner=commands.append)
                self.assertEqual(commands, [])

    def test_wrong_kernel_is_rejected_even_when_python_symlinks_match(self):
        for cell_id in ("04fb2ced", "a2c70b8c"):
            with self.subTest(cell=cell_id), TemporaryDirectory() as directory:
                root = self.make_root(directory, existing=True)
                commands = []
                for prefix in (Path("/usr"), root / ".venv", root / "agent-framework-demo" / ".venv"):
                    with self.subTest(prefix=prefix), self.assertRaisesRegex(RuntimeError, "kernel"):
                        self.execute(cell_id, root, prefix=prefix, runner=commands.append)
                self.assertEqual(commands, [])

    def test_installation_uses_only_linux_manifest_and_checks_dependencies(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            commands = []
            self.execute("a2c70b8c", root, runner=commands.append)
            self.assertEqual(len(commands), 2)
            self.assertEqual(commands[0][-2:], ["--requirement", str(root / "requirements" / "requirements-notebook-linux.txt")])
            self.assertEqual(commands[1][-2:], ["pip", "check"])
            for forbidden in ("--pre", "--index-url", "--trusted-host", "--find-links"):
                self.assertNotIn(forbidden, commands[0])

    def test_verification_and_installation_require_the_exact_python_patch(self):
        for cell_id in ("04fb2ced", "a2c70b8c"):
            for python_version in ((3, 14, 4), (3, 14, 6), (3, 15, 0)):
                with self.subTest(cell=cell_id, python=python_version), TemporaryDirectory() as directory:
                    root = self.make_root(directory)
                    commands = []
                    with self.assertRaisesRegex(RuntimeError, r"Python 3\.14\.7"):
                        self.execute(cell_id, root, runner=commands.append, python_version=python_version)
                    self.assertEqual(commands, [])

    def test_bootstrap_rejects_non_linux_hosts_before_side_effects(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            commands = []
            with self.assertRaisesRegex(RuntimeError, "notebook on Linux"):
                self.execute("fcc00444", root, runner=commands.append, platform_name="win32")
            self.assertEqual(commands, [])

    def test_installation_failure_is_not_reported_as_success(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            with self.assertRaisesRegex(RuntimeError, "Linux dependency installation failed") as error:
                self.execute("a2c70b8c", root, runner=subprocess.CalledProcessError(1, ["pip"]))
            self.assertIsInstance(error.exception.__cause__, subprocess.CalledProcessError)


class LinuxCliLoginTests(unittest.TestCase):
    def setUp(self):
        import shutil

        source = ast.parse(cell_source("8330c10b"))
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "_ensure_az_login")
        namespace = {"shutil": shutil, "subprocess": subprocess}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(NOTEBOOK), "exec"), namespace)
        self.login = namespace["_ensure_az_login"]

    def test_missing_cli_does_not_try_to_install_system_packages(self):
        output = io.StringIO()
        with patch("shutil.which", return_value=None), patch("subprocess.run") as run, redirect_stdout(output):
            self.assertFalse(self.login())
        run.assert_not_called()
        self.assertIn("install-azure-cli-linux", output.getvalue())

    def test_expired_login_reports_terminal_device_code_instructions(self):
        output = io.StringIO()
        with (
            patch("shutil.which", return_value="/usr/bin/az"),
            patch("subprocess.run", return_value=SimpleNamespace(returncode=1, stderr="Please sign in.")) as run,
            redirect_stdout(output),
        ):
            self.assertFalse(self.login())
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/az", "account", "show", "--output", "none"])
        self.assertIn("az login --use-device-code", output.getvalue())
        self.assertIn("Please sign in.", output.getvalue())

    def test_success_and_timeout_are_distinguished(self):
        for result in (SimpleNamespace(returncode=0), subprocess.TimeoutExpired("az", 30)):
            output = io.StringIO()
            timed_out = isinstance(result, subprocess.TimeoutExpired)
            with (
                patch("shutil.which", return_value="/usr/bin/az"),
                patch("subprocess.run", side_effect=result if timed_out else None, return_value=result),
                redirect_stdout(output),
            ):
                self.assertEqual(self.login(), not timed_out)
            if timed_out:
                self.assertIn("timed out", output.getvalue())


if __name__ == "__main__":
    unittest.main()
