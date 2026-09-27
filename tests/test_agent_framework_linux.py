"""Linux edition of the Agent Framework SDK notebook, and checks shared by both editions."""

# These tests run notebook cell source on purpose.
# pylint: disable=exec-used

import ast
import configparser
import glob
import json
import os
import re
import sys
import sysconfig
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "agent-framework-demo"
LINUX_NOTEBOOK = DEMO_DIR / "zolab-agent-framework-sdk-linux.ipynb"
WINDOWS_NOTEBOOK = DEMO_DIR / "zolab-agent-framework-sdk-win11.ipynb"
NOTEBOOKS = (WINDOWS_NOTEBOOK, LINUX_NOTEBOOK)
KERNEL_NAME = "agent-framework-sdk-demo-linux"
KERNEL_DISPLAY = "Agent Framework SDK Demo (Linux, .venv-linux)"
ASPIRE_CONTAINER = "zolab-agent-framework-aspire"
PINNED_ASPIRE_IMAGE = re.compile(
    r"^mcr\.microsoft\.com/dotnet/aspire-dashboard:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$"
)
# The only cells that differ from the Windows notebook; the Windows notebook's tests
# cover the rest.
LINUX_CELLS = {
    "2ea36b16", "1bc8dbb9", "40b63ed3", "eba4152c", "e6f2c7e3", "beca7704",
    "dc65fb6a", "cc566261", "8ec9f476", "91a844a6", "0f3f5f40", "4dd2f39c",
}
WINDOWS_ONLY = (
    "winget", "PowerShell terminal", "powershell", "Scripts", "python.exe", "Docker Desktop",
    "pywin32", "pywintypes", "py -3", "Windows 11", "os.name == 'nt'",
)
MODULES = (
    "agent_framework_menu_mcp_server.py",
    "agent_framework_reviewer_a2a_server.py",
    "agent_framework_reviewer_a2a_client.py",
)


def load(path):
    """Return a parsed notebook."""
    return json.loads(path.read_text(encoding="utf-8"))


def sources(notebook):
    """Return a notebook's cell sources keyed by cell ID."""
    return {cell["id"]: "".join(cell["source"]) for cell in notebook["cells"]}


def run_aspire_cell(notebook_path, *, engine_ready=True, existing=None):
    """Run a notebook's Aspire cell against a fake Docker CLI.

    existing=(status, image) simulates a container that already exists.
    """
    commands, panels = [], []

    def ok(stdout=""):
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def fake_run(command, **_kwargs):
        commands.append(command)
        action = command[1]
        if action == "info":
            if engine_ready:
                return ok("27.0\n")
            return SimpleNamespace(returncode=1, stdout="", stderr="Cannot connect")
        if action == "ps":
            return ok(f"{ASPIRE_CONTAINER}\t{existing[0]}\n" if existing else "")
        if action == "inspect":
            return ok(f"{existing[1]}\n" if existing else "")
        if action == "port":
            return ok(f"127.0.0.1:{'18888' if command[3].startswith('18888') else '4317'}\n")
        if action in {"run", "logs", "rm", "start"}:
            return ok()
        raise AssertionError(f"unexpected docker command: {command}")

    scope = {
        "demo_status": lambda text, enabled: text,
        "demo_text": lambda text, tone: text,
        "display_demo_panel": lambda title, rows: panels.append((title, dict(rows))),
    }
    source = sources(load(notebook_path))["cc566261"]
    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True),
    ):
        exec(compile(source, f"{notebook_path}#cc566261", "exec"), scope)
    return commands, panels, scope


class LinuxNotebookParityTests(unittest.TestCase):
    """Check the Linux notebook mirrors the Windows notebook except for platform cells."""

    def setUp(self):
        self.linux, self.windows = load(LINUX_NOTEBOOK), load(WINDOWS_NOTEBOOK)

    def test_structure_matches_the_windows_notebook_and_only_platform_cells_differ(self):
        self.assertEqual(
            [(cell["id"], cell["cell_type"]) for cell in self.linux["cells"]],
            [(cell["id"], cell["cell_type"]) for cell in self.windows["cells"]],
        )
        linux, windows = sources(self.linux), sources(self.windows)
        changed = {cell_id for cell_id in linux if linux[cell_id] != windows[cell_id]}
        self.assertEqual(changed, LINUX_CELLS)

    def test_outputs_are_clean_and_the_linux_kernel_is_declared(self):
        for cell in self.linux["cells"]:
            if cell["cell_type"] == "code":
                self.assertEqual((cell["outputs"], cell["execution_count"]), ([], None), cell["id"])
        self.assertEqual(
            self.linux["metadata"]["kernelspec"],
            {"display_name": KERNEL_DISPLAY, "language": "python", "name": KERNEL_NAME},
        )

    def test_platform_cells_have_no_windows_only_steps(self):
        linux = sources(self.linux)
        for cell_id in LINUX_CELLS:
            for marker in WINDOWS_ONLY:
                self.assertNotIn(marker, linux[cell_id], f"{cell_id}: {marker}")

    def test_workflow_plans_a_runbook_for_this_linux_notebook(self):
        linux = sources(self.linux)
        for cell_id in ("0f3f5f40", "4dd2f39c"):
            self.assertIn("Ubuntu Linux", linux[cell_id])
            self.assertNotIn("Windows", linux[cell_id])
        self.assertIn("an independent agent-framework-demo/.venv-linux.", linux["4dd2f39c"])

    def test_linux_package_source_uses_the_cross_platform_wheel_helper(self):
        self.assertIn("pwsh ./build_source_wheels.ps1", sources(self.linux)["beca7704"])


class LinuxEnvironmentCellTests(unittest.TestCase):
    """Run the Linux bootstrap and kernel-check cells with fake installers and interpreters."""

    def setUp(self):
        self.cells = sources(load(LINUX_NOTEBOOK))

    def run_cell(
        self, cell_id, working_dir, *, check_call=None, executable=sys.executable,
        platform="linux",
    ):
        """Run one Linux notebook cell from working_dir and return its rendered HTML."""
        rendered = []
        with (
            patch("pathlib.Path.cwd", return_value=working_dir),
            patch("subprocess.check_call", side_effect=check_call),
            patch("IPython.display.display", side_effect=rendered.append),
            patch.object(sys, "executable", executable),
            patch.object(sys, "platform", platform),
        ):
            exec(compile(self.cells[cell_id], f"{LINUX_NOTEBOOK}#{cell_id}", "exec"), {})
        return "\n".join(getattr(item, "data", "") for item in rendered)

    def test_bootstrap_creates_and_registers_the_linux_environment(self):
        with TemporaryDirectory() as directory:
            repository = Path(directory)
            demo_dir = repository / "agent-framework-demo"
            demo_dir.mkdir()
            python = demo_dir / ".venv-linux" / "bin" / "python"
            commands = []

            def check_call(command):
                commands.append(command)
                if command[1:3] == ["-m", "venv"]:
                    python.parent.mkdir(parents=True)
                    python.touch()

            html = self.run_cell("40b63ed3", repository, check_call=check_call)
        self.assertEqual(
            commands[0], [sys.executable, "-m", "venv", str(demo_dir / ".venv-linux")]
        )
        self.assertEqual(commands[1][0], str(python))
        self.assertEqual(
            commands[2][-4:], ["--name", KERNEL_NAME, "--display-name", KERNEL_DISPLAY]
        )
        self.assertIn("Created", html)
        self.assertIn(KERNEL_DISPLAY, html)

    def test_bootstrap_refuses_to_run_outside_linux(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / "agent-framework-demo").mkdir()
            windows_notebook = "zolab-agent-framework-sdk-win11.ipynb on Windows"
            with self.assertRaisesRegex(RuntimeError, windows_notebook):
                self.run_cell(
                    "40b63ed3", Path(directory), check_call=AssertionError, platform="win32"
                )

    def test_kernel_check_accepts_only_the_linux_demo_interpreter(self):
        with TemporaryDirectory() as directory:
            demo_dir = Path(directory) / "agent-framework-demo"
            python = demo_dir / ".venv-linux" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            html = self.run_cell("e6f2c7e3", demo_dir, executable=str(python))
            self.assertIn("Notebook kernel verified", html)
            other_python = str(Path(directory) / "other-python")
            with self.assertRaisesRegex(RuntimeError, re.escape(KERNEL_DISPLAY)):
                self.run_cell("e6f2c7e3", demo_dir, executable=other_python)


class AspireDashboardCellTests(unittest.TestCase):
    """Run both notebooks' Aspire Dashboard cells against a fake Docker CLI."""

    def test_both_notebooks_publish_a_pinned_dashboard_on_loopback_only(self):
        for notebook in NOTEBOOKS:
            with self.subTest(notebook=notebook.name):
                commands, _, scope = run_aspire_cell(notebook)
                run = next(command for command in commands if command[1] == "run")
                published = [
                    run[index + 1] for index, argument in enumerate(run) if argument == "-p"
                ]
                self.assertEqual(len(published), 2)
                self.assertTrue(
                    all(mapping.startswith("127.0.0.1:") for mapping in published), published
                )
                self.assertEqual(
                    {mapping.rsplit(":", 1)[1] for mapping in published}, {"18888", "18889"}
                )
                self.assertRegex(run[-1], PINNED_ASPIRE_IMAGE)
                self.assertTrue(scope["ASPIRE_DASHBOARD_RUNNING"])
                self.assertEqual(scope["OTEL_EXPORTER_ENDPOINT"], "http://localhost:4317")
                cleanup = sources(load(notebook))["81348edc"]
                self.assertIn(
                    f"globals().get('ASPIRE_IMAGE_REF', '{scope['ASPIRE_IMAGE_REF']}')", cleanup
                )

    def test_a_container_from_another_image_is_recreated_and_a_pinned_one_is_reused(self):
        for notebook in NOTEBOOKS:
            with self.subTest(notebook=notebook.name):
                commands, _, scope = run_aspire_cell(
                    notebook,
                    existing=("Up 5 minutes", "mcr.microsoft.com/dotnet/aspire-dashboard:latest"),
                )
                actions = [command[1] for command in commands]
                self.assertLess(actions.index("rm"), actions.index("run"))
                pinned = scope["ASPIRE_IMAGE_REF"]
                commands, _, scope = run_aspire_cell(notebook, existing=("Up 5 minutes", pinned))
                self.assertNotIn("run", [command[1] for command in commands])
                self.assertNotIn("rm", [command[1] for command in commands])
                self.assertTrue(scope["ASPIRE_DASHBOARD_RUNNING"])

    def test_linux_shows_port_forwarding_only_while_running(self):
        _, panels, _ = run_aspire_cell(LINUX_NOTEBOOK)
        self.assertIn("VS Code Ports panel", panels[-1][1]["Remote access"])
        commands, panels, scope = run_aspire_cell(LINUX_NOTEBOOK, engine_ready=False)
        self.assertEqual([command[1] for command in commands], ["info"])
        self.assertNotIn("Remote access", panels[-1][1])
        self.assertFalse(scope["ASPIRE_DASHBOARD_RUNNING"])
        self.assertEqual(scope["OTEL_EXPORTER_ENDPOINT"], "")


class DocumentationTests(unittest.TestCase):
    """Check notebooks and modules state their author, date, purpose and docstrings."""

    def test_notebooks_state_author_and_update_date(self):
        for notebook in NOTEBOOKS:
            with self.subTest(notebook=notebook.name):
                intro = sources(load(notebook))["2ea36b16"]
                self.assertRegex(
                    intro,
                    r"Author: dcodev1702 \(with GitHub Copilot assistance\)"
                    r" · Updated: \d{4}-\d{2}-\d{2}",
                )

    def test_every_code_cell_starts_with_its_section_and_purpose(self):
        for notebook in NOTEBOOKS:
            for cell in load(notebook)["cells"]:
                if cell["cell_type"] == "code":
                    with self.subTest(notebook=notebook.name, cell=cell["id"]):
                        first_line = "".join(cell["source"]).splitlines()[0]
                        self.assertRegex(first_line, r"^# \d+(\.\d+)?\.? [A-Z].+\.$")

    def test_modules_have_headers_and_documented_top_level_definitions(self):
        for module in MODULES:
            source = (DEMO_DIR / module).read_text(encoding="utf-8")
            with self.subTest(module=module):
                self.assertIn(f"# File: {module}", source)
                self.assertIn("# Author: dcodev1702 (with GitHub Copilot assistance)", source)
                self.assertRegex(source, r"# Updated: \d{4}-\d{2}-\d{2}")
                tree = ast.parse(source)
                self.assertIsNotNone(ast.get_docstring(tree), f"{module}: module docstring")
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        self.assertIsNotNone(ast.get_docstring(node), f"{module}: {node.name}")

    def test_wheel_helper_defaults_to_the_platform_environment(self):
        script = (DEMO_DIR / "build_source_wheels.ps1").read_text(encoding="utf-8")
        self.assertIn("if ($env:OS -eq 'Windows_NT')", script)
        self.assertIn("'.venv\\Scripts\\python.exe'", script)
        self.assertIn("'.venv-linux/bin/python'", script)


class WorkspaceLintConfigTests(unittest.TestCase):
    """Check .pylintrc lets VS Code's Pylint resolve this demo's separate environment."""

    def setUp(self):
        # Pylint reads INI files with these inline comment prefixes.
        self.config = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        self.config.read(ROOT / ".pylintrc", encoding="utf-8")

    def run_init_hook(self, search_path):
        """Run the init-hook against a copy of search_path and return what it appended."""
        pylint_config = ModuleType("pylint.config")
        pylint_config.find_default_config_files = lambda: iter([ROOT / ".pylintrc"])
        pylint = ModuleType("pylint")
        pylint.config = pylint_config
        path = list(search_path)
        with (
            patch.dict(sys.modules, {"pylint": pylint, "pylint.config": pylint_config}),
            patch.object(sys, "path", path),
        ):
            exec(self.config["MAIN"]["init-hook"], {})
        self.assertEqual(path[: len(search_path)], search_path)
        return path[len(search_path):]

    def test_demo_environment_is_appended_after_the_linting_interpreter(self):
        if os.name == "nt":
            site_packages = DEMO_DIR / ".venv" / "Lib" / "site-packages"
        else:
            site_packages = DEMO_DIR / ".venv-linux" / "lib" / "python3.*" / "site-packages"
        appended = self.run_init_hook(["root-environment-site-packages"])
        self.assertEqual(appended, [str(DEMO_DIR), *sorted(glob.glob(str(site_packages)))])
        # Run from the demo environment, the hook must find that environment's packages.
        if Path(sys.prefix).resolve().parent == DEMO_DIR:
            appended_paths = {Path(entry).resolve() for entry in appended}
            purelib = Path(sysconfig.get_paths()["purelib"]).resolve()
            self.assertIn(purelib, appended_paths)

    def test_lazy_agent_framework_exports_and_test_docstrings_are_configured(self):
        self.assertEqual(self.config["TYPECHECK"]["ignored-modules"], "agent_framework")
        no_docstring = re.compile(self.config["BASIC"]["no-docstring-rgx"])
        self.assertTrue(no_docstring.match("test_helper_is_regenerated"))
        self.assertTrue(no_docstring.match("_private_helper"))
        self.assertIsNone(no_docstring.match("run_cell"))


if __name__ == "__main__":
    unittest.main()
