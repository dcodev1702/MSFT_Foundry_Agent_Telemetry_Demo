"""Contracts for notebook imports, dependency paths and relocated documentation."""

import ast
import importlib
import io
import json
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "zolab-ai-agent-demo-win11.ipynb"
MODULES = ("agent_endpoints", "observability", "response_observability", "workflow")
PROFILES = (
    "requirements-notebook.txt", "requirements-notebook-shared.txt",
    "requirements-notebook-validation.txt",
)
GUIDES = ("observability.md", "OTEL-Agent-Spans.md")


class NotebookLayoutTests(unittest.TestCase):
    def setUp(self):
        notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        self.cells = {cell["id"]: cell for cell in notebook["cells"]}

    def test_support_modules_import_from_the_package(self):
        for name in MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(f"notebook_support.{name}")
                self.assertEqual(Path(module.__file__).resolve(), ROOT / "notebook_support" / f"{name}.py")
        self.assertTrue((ROOT / "notebook_support" / "__init__.py").is_file())

    def test_notebook_uses_qualified_imports_without_path_injection(self):
        imported = set()
        for cell in self.cells.values():
            if cell["cell_type"] != "code":
                continue
            tree = ast.parse("".join(cell["source"]))
            imported.update(node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
        for name in MODULES:
            self.assertIn(f"notebook_support.{name}", imported)
        self.assertTrue(imported.isdisjoint({
            "notebook_agent_endpoints", "notebook_observability", "notebook_workflow",
        }))

    def test_old_root_files_are_not_left_as_duplicate_sources(self):
        names = (
            "notebook_agent_endpoints.py", "notebook_observability.py", "notebook_workflow.py",
            *PROFILES, "constraints-notebook-win11.txt", *GUIDES,
        )
        for name in names:
            with self.subTest(path=name):
                self.assertFalse((ROOT / name).exists())

    def test_requirement_includes_resolve_relative_to_their_manifests(self):
        directory = ROOT / "requirements"
        for name in PROFILES:
            path = directory / name
            self.assertTrue(path.is_file())
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith(("-r ", "-c ")):
                    include = path.parent / line.split(maxsplit=1)[1]
                    self.assertTrue(include.is_file(), f"{path.name}: missing {include.name}")
                    self.assertEqual(include.parent, directory)
        self.assertTrue((directory / "constraints-notebook-win11.txt").is_file())

    def test_bootstrap_passes_the_relocated_constraint_path_to_pip(self):
        source = "".join(self.cells["fcc00444"]["source"])
        commands = []
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("pathlib.Path.cwd", return_value=root),
                patch("subprocess.check_call", side_effect=commands.append),
                redirect_stdout(io.StringIO()),
            ):
                exec(compile(source, "notebook-bootstrap", "exec"), {})
            install = next(command for command in commands if "pip" in command)
            constraint = Path(install[install.index("-c") + 1])
            self.assertEqual(constraint, root / "requirements" / "constraints-notebook-win11.txt")

    def test_wheel_builder_instructions_use_the_relocated_profiles(self):
        source = (ROOT / "build_notebook_wheels.ps1").read_text(encoding="utf-8")
        for name in PROFILES:
            self.assertIn(f"-r requirements\\{name}", source)

    def test_documentation_links_resolve_from_their_actual_directories(self):
        documents = [
            ROOT / "README.md", ROOT / "CHANGELOG.md",
            *(ROOT / "docs" / name for name in GUIDES),
            ROOT / "agent-framework-demo" / "README-agent-framework-sdk-poc.md",
        ]
        sources = [(path, path.read_text(encoding="utf-8")) for path in documents]
        sources.extend(
            (NOTEBOOK, "".join(cell["source"]))
            for cell in self.cells.values() if cell["cell_type"] == "markdown"
        )
        checked = 0
        for document, source in sources:
            for target in re.findall(r"\]\(([^)\s]+)\)", source):
                parts = urlsplit(target)
                if parts.scheme or parts.netloc or not parts.path:
                    continue
                with self.subTest(document=document.name, target=target):
                    self.assertTrue((document.parent / unquote(parts.path)).exists())
                checked += 1
        self.assertGreater(checked, 30)


if __name__ == "__main__":
    unittest.main()
