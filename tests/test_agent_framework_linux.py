"""Linux edition of the Agent Framework SDK notebook."""

import json
import os
import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "agent-framework-demo"
LINUX_NOTEBOOK = DEMO_DIR / "zolab-agent-framework-sdk-linux.ipynb"
WINDOWS_NOTEBOOK = DEMO_DIR / "zolab-agent-framework-sdk-win11.ipynb"
KERNEL_NAME = "agent-framework-sdk-demo-linux"
KERNEL_DISPLAY = "Agent Framework SDK Demo (Linux, .venv-linux)"
# The only cells that differ from the Windows notebook; the Windows notebook's tests cover the rest.
LINUX_CELLS = {
    "2ea36b16", "1bc8dbb9", "40b63ed3", "eba4152c", "e6f2c7e3", "beca7704",
    "dc65fb6a", "cc566261", "8ec9f476", "91a844a6", "0f3f5f40", "4dd2f39c",
}
WINDOWS_ONLY = (
    "winget", "PowerShell", "powershell", "Scripts", "python.exe", "Docker Desktop",
    "pywin32", "pywintypes", "py -3", "Windows 11", "os.name == 'nt'",
)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sources(notebook):
    return {cell["id"]: "".join(cell["source"]) for cell in notebook["cells"]}


class LinuxNotebookParityTests(unittest.TestCase):
    def setUp(self):
        self.linux, self.windows = load(LINUX_NOTEBOOK), load(WINDOWS_NOTEBOOK)

    def test_structure_matches_the_windows_notebook_and_only_platform_cells_differ(self):
        self.assertEqual(
            [(cell["id"], cell["cell_type"]) for cell in self.linux["cells"]],
            [(cell["id"], cell["cell_type"]) for cell in self.windows["cells"]],
        )
        linux, windows = sources(self.linux), sources(self.windows)
        self.assertEqual({cell_id for cell_id in linux if linux[cell_id] != windows[cell_id]}, LINUX_CELLS)

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


class LinuxEnvironmentCellTests(unittest.TestCase):
    def setUp(self):
        self.cells = sources(load(LINUX_NOTEBOOK))

    def run_cell(self, cell_id, working_dir, *, check_call=None, executable=sys.executable, platform="linux"):
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
        self.assertEqual(commands[0], [sys.executable, "-m", "venv", str(demo_dir / ".venv-linux")])
        self.assertEqual(commands[1][0], str(python))
        self.assertEqual(
            commands[2][-4:], ["--name", KERNEL_NAME, "--display-name", KERNEL_DISPLAY],
        )
        self.assertIn("Created", html)
        self.assertIn(KERNEL_DISPLAY, html)

    def test_bootstrap_refuses_to_run_outside_linux(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / "agent-framework-demo").mkdir()
            with self.assertRaisesRegex(RuntimeError, "zolab-agent-framework-sdk-win11.ipynb on Windows"):
                self.run_cell("40b63ed3", Path(directory), check_call=AssertionError, platform="win32")

    def test_kernel_check_accepts_only_the_linux_demo_interpreter(self):
        with TemporaryDirectory() as directory:
            demo_dir = Path(directory) / "agent-framework-demo"
            python = demo_dir / ".venv-linux" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            self.assertIn("Notebook kernel verified", self.run_cell("e6f2c7e3", demo_dir, executable=str(python)))
            with self.assertRaisesRegex(RuntimeError, re.escape(KERNEL_DISPLAY)):
                self.run_cell("e6f2c7e3", demo_dir, executable=str(Path(directory) / "other-python"))


class LinuxAspireDashboardTests(unittest.TestCase):
    def run_aspire(self, *, engine_ready=True):
        commands, panels = [], []

        def fake_run(command, **kwargs):
            commands.append(command)
            action = command[1]
            if action == "info":
                return SimpleNamespace(returncode=0 if engine_ready else 1, stdout="27.0\n", stderr="" if engine_ready else "Cannot connect")
            if action == "port":
                host_port = "18888" if command[3].startswith("18888") else "4317"
                return SimpleNamespace(returncode=0, stdout=f"127.0.0.1:{host_port}\n", stderr="")
            if action in {"ps", "run", "logs"}:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected docker command: {command}")

        scope = {
            "demo_status": lambda text, enabled: text,
            "demo_text": lambda text, tone: text,
            "display_demo_panel": lambda title, rows: panels.append((title, dict(rows))),
        }
        source = sources(load(LINUX_NOTEBOOK))["cc566261"]
        with (
            patch("subprocess.run", side_effect=fake_run),
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True),
        ):
            exec(compile(source, f"{LINUX_NOTEBOOK}#cc566261", "exec"), scope)
        return commands, panels, scope

    def test_dashboard_ports_are_published_on_loopback_only(self):
        commands, panels, scope = self.run_aspire()
        run = next(command for command in commands if command[1] == "run")
        published = [run[index + 1] for index, argument in enumerate(run) if argument == "-p"]
        self.assertEqual(len(published), 2)
        self.assertTrue(all(mapping.startswith("127.0.0.1:") for mapping in published), published)
        self.assertEqual({mapping.rsplit(":", 1)[1] for mapping in published}, {"18888", "18889"})
        self.assertTrue(scope["ASPIRE_DASHBOARD_RUNNING"])
        self.assertEqual(scope["OTEL_EXPORTER_ENDPOINT"], "http://localhost:4317")
        self.assertIn("VS Code Ports panel", panels[-1][1]["Remote access"])

    def test_unreachable_engine_gives_linux_guidance_without_docker_desktop(self):
        commands, _, scope = self.run_aspire(engine_ready=False)
        self.assertEqual([command[1] for command in commands], ["info"])
        self.assertFalse(scope["ASPIRE_DASHBOARD_RUNNING"])
        self.assertEqual(scope["OTEL_EXPORTER_ENDPOINT"], "")


if __name__ == "__main__":
    unittest.main()
