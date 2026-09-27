"""Real MAF workflow tests with local callbacks and an in-memory OTEL exporter."""

import ast
import asyncio
import io
import json
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_framework.observability import enable_instrumentation
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from notebook_support.workflow import (
    DISPATCH_EXECUTOR_ID, NotebookStep, ParallelSteps, buffered_output, run_notebook_workflow,
)


RUN_ID = "00000000-0000-0000-0000-000000000001"
ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = TracerProvider()
        self.exporter = InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("workflow-test")
        self.addCleanup(self.provider.shutdown)
        for target in (
            "notebook_support.workflow.trace.get_tracer",
            "agent_framework.observability.get_tracer",
        ):
            patcher = patch(target, return_value=self.tracer)
            patcher.start()
            self.addCleanup(patcher.stop)
        enable_instrumentation(enable_sensitive_data=False, enable_message_events=False, force=True)
        self.calls = []

    def action(self, name):
        def invoke():
            self.calls.append(name)
            with self.tracer.start_as_current_span(f"existing {name}"):
                with self.tracer.start_as_current_span("transport"):
                    pass
        return invoke

    async def run_workflow(self, steps=None, **overrides):
        arguments = {
            "name": "story-facts", "run_id": RUN_ID, "session_id": "session-test",
            "steps": steps if steps is not None else [
                NotebookStep(name, self.action(name)) for name in ("story", "facts", "persistence")
            ],
            "agent_attributes": {"gen_ai.agent.name": "existing-agent", "gen_ai.agent.version": "8"},
        }
        return await run_notebook_workflow(**(arguments | overrides))

    async def test_real_maf_executes_in_order_and_persists_once(self):
        result = await self.run_workflow()
        self.assertEqual(self.calls, ["story", "facts", "persistence"])
        self.assertEqual(self.calls.count("persistence"), 1)
        self.assertEqual(result.get_outputs(), [RUN_ID])
        self.assertEqual(len([event for event in result if event.type == "executor_invoked"]), 3)

    async def test_all_steps_and_sdk_children_share_trace_but_not_interaction_roots(self):
        await self.run_workflow()
        spans = self.exporter.get_finished_spans()
        self.assertEqual(len({span.context.trace_id for span in spans}), 1)
        roots = [span for span in spans if span.attributes.get("app.interaction.root")]
        self.assertEqual(len(roots), 3)
        self.assertEqual({span.attributes["app.interaction"] for span in roots},
                         {"story", "facts", "persistence"})
        native = next(span for span in spans if span.name == "workflow.run")
        outer = next(span for span in spans if span.name == "notebook.workflow story-facts")
        self.assertEqual(native.parent.span_id, outer.context.span_id)
        self.assertEqual(outer.attributes["app.workflow.status"], "completed")
        for root in roots:
            self.assertTrue(root.name.startswith("executor.process "))
            self.assertEqual(root.parent.span_id, native.context.span_id)
            self.assertEqual(root.attributes["demo.run_id"], RUN_ID)
            self.assertEqual(root.attributes["gen_ai.agent.version"], "8")
            child = next(span for span in spans
                         if span.name == f"existing {root.attributes['app.interaction']}")
            self.assertEqual(child.parent.span_id, root.context.span_id)
        self.assertFalse(trace.get_current_span().get_span_context().is_valid)

    async def test_failure_stops_downstream_steps_and_marks_native_spans(self):
        def fail():
            self.calls.append("facts")
            raise RuntimeError("test-only MCP failure")
        with self.assertRaisesRegex(RuntimeError, "MCP failure"):
            await self.run_workflow([
                NotebookStep("story", self.action("story")),
                NotebookStep("facts", fail),
                NotebookStep("persistence", self.action("persistence")),
            ])
        self.assertEqual(self.calls, ["story", "facts"])
        spans = self.exporter.get_finished_spans()
        for name in ("executor.process facts", "workflow.run", "notebook.workflow story-facts"):
            failed = next(span for span in spans if span.name == name)
            self.assertEqual(failed.status.status_code, StatusCode.ERROR)
            self.assertEqual(failed.attributes["error.type"], "RuntimeError")
        outer = next(span for span in spans if span.attributes.get("app.workflow.root"))
        self.assertEqual(list(outer.attributes["app.workflow.completed_steps"]), ["story"])

    async def test_persistence_failure_is_not_retried(self):
        def fail():
            self.calls.append("persistence")
            raise OSError("test-only write failure")
        with self.assertRaisesRegex(OSError, "write failure"):
            await self.run_workflow([NotebookStep("persistence", fail)])
        self.assertEqual(self.calls, ["persistence"])

    async def test_sentinel_is_an_independent_opt_in_workflow(self):
        await self.run_workflow()
        await self.run_workflow([NotebookStep("sentinel", self.action("sentinel"))], name="sentinel")
        runs = [span for span in self.exporter.get_finished_spans() if span.name == "workflow.run"]
        self.assertEqual({span.attributes["workflow.name"] for span in runs}, {"story-facts", "sentinel"})
        self.assertEqual(len({span.context.trace_id for span in runs}), 2)
        self.assertEqual(self.calls, ["story", "facts", "persistence", "sentinel"])

    async def test_async_callbacks_are_awaited(self):
        async def action():
            await asyncio.sleep(0)
            self.calls.append("async")
        await self.run_workflow([NotebookStep("async", action)])
        self.assertEqual(self.calls, ["async"])

    async def test_cancellation_is_not_success_and_does_not_persist(self):
        async def cancel():
            self.calls.append("facts")
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.run_workflow([
                NotebookStep("facts", cancel),
                NotebookStep("persistence", self.action("persistence")),
            ])
        self.assertEqual(self.calls, ["facts"])
        spans = self.exporter.get_finished_spans()
        outer = next(span for span in spans if span.attributes.get("app.workflow.root"))
        executor_span = next(span for span in spans if span.attributes.get("app.interaction.root"))
        self.assertEqual(outer.attributes["app.workflow.status"], "cancelled")
        self.assertEqual(executor_span.attributes["app.workflow.step.status"], "cancelled")
        self.assertEqual(outer.status.status_code, StatusCode.ERROR)
        self.assertEqual(executor_span.status.status_code, StatusCode.ERROR)
        self.assertFalse(trace.get_current_span().get_span_context().is_valid)

    async def test_workflow_events_never_carry_agent_payloads(self):
        private_result = "sensitive prompt and response"
        captured = []
        def invoke():
            captured.append(private_result)
        result = await self.run_workflow([NotebookStep("story", invoke)])
        self.assertEqual(captured, [private_result])
        self.assertNotIn(private_result, str(result))
        self.assertNotIn(private_result, str([dict(span.attributes)
                                            for span in self.exporter.get_finished_spans()]))

    async def test_invalid_configuration_fails_before_side_effects(self):
        for changes in (
            {"run_id": "invalid"}, {"name": ""}, {"session_id": ""},
            {"steps": []},
            {"steps": [NotebookStep("", self.action("bad"))]},
            {"steps": [NotebookStep("same", self.action("a")), NotebookStep("same", self.action("b"))]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await self.run_workflow(**changes)
        self.assertEqual(self.calls, [])

    def parallel_steps(self, story=None, facts=None):
        return [
            ParallelSteps([
                NotebookStep("story", story or self.action("story")),
                NotebookStep("facts", facts or self.action("facts")),
            ]),
            NotebookStep("persistence", self.action("persistence")),
        ]

    async def test_parallel_steps_overlap_and_persistence_waits_for_both(self):
        both_started = threading.Barrier(2, timeout=5)

        def overlapping(name):
            record = self.action(name)

            def invoke():
                # Each step waits for the other, so the barrier breaks if they run one after another.
                both_started.wait()
                record()
            return invoke

        result = await self.run_workflow(self.parallel_steps(overlapping("story"), overlapping("facts")))
        self.assertEqual(sorted(self.calls[:2]), ["facts", "story"])
        self.assertEqual(self.calls[2:], ["persistence"])
        self.assertEqual(result.get_outputs(), [RUN_ID])
        invoked = [event.executor_id for event in result if event.type == "executor_invoked"]
        self.assertEqual(invoked[0], DISPATCH_EXECUTOR_ID)
        self.assertEqual(sorted(invoked[1:3]), ["facts", "story"])
        self.assertEqual(invoked[3:], ["persistence"])

    async def test_parallel_steps_keep_their_own_executor_parents(self):
        await self.run_workflow(self.parallel_steps())
        spans = self.exporter.get_finished_spans()
        self.assertEqual(len({span.context.trace_id for span in spans}), 1)
        roots = {span.attributes["app.interaction"]: span
                 for span in spans if span.attributes.get("app.interaction.root")}
        self.assertEqual(set(roots), {"story", "facts", "persistence"})
        for name, root in roots.items():
            child = next(span for span in spans if span.name == f"existing {name}")
            self.assertEqual(child.parent.span_id, root.context.span_id)
        dispatch = next(span for span in spans if span.name == f"executor.process {DISPATCH_EXECUTOR_ID}")
        self.assertTrue(dispatch.attributes["app.workflow.dispatch"])
        self.assertEqual(dispatch.attributes["demo.run_id"], RUN_ID)
        self.assertNotIn("app.interaction.root", dispatch.attributes)
        self.assertNotIn("app.workflow.step", dispatch.attributes)
        outer = next(span for span in spans if span.attributes.get("app.workflow.root"))
        self.assertEqual(list(outer.attributes["app.workflow.stages"]), ["story+facts", "persistence"])
        self.assertEqual(list(outer.attributes["app.workflow.expected_steps"]), ["story", "facts", "persistence"])
        self.assertEqual(sorted(outer.attributes["app.workflow.completed_steps"]),
                         ["facts", "persistence", "story"])
        self.assertFalse(trace.get_current_span().get_span_context().is_valid)

    async def test_parallel_failure_lets_the_sibling_finish_and_skips_persistence(self):
        def slow_story():
            time.sleep(0.2)
            self.calls.append("story")

        def fail():
            self.calls.append("facts")
            raise RuntimeError("test-only MCP failure")

        with self.assertRaisesRegex(RuntimeError, "MCP failure"):
            await self.run_workflow(self.parallel_steps(slow_story, fail))
        # The story thread finished before the workflow reported the failure; nothing persisted.
        self.assertEqual(sorted(self.calls), ["facts", "story"])
        spans = self.exporter.get_finished_spans()
        story = next(span for span in spans if span.name == "executor.process story")
        facts = next(span for span in spans if span.name == "executor.process facts")
        self.assertEqual(story.attributes["app.workflow.step.status"], "cancelled")
        self.assertEqual(facts.attributes["app.workflow.step.status"], "failed")
        for span in (story, facts):
            self.assertEqual(span.status.status_code, StatusCode.ERROR)
        self.assertFalse(any(span.name == "executor.process persistence" for span in spans))

    async def test_invalid_parallel_configuration_fails_before_side_effects(self):
        with self.assertRaises(TypeError):
            ParallelSteps([NotebookStep("story", self.action("story"))])
        for steps in (
            [ParallelSteps([NotebookStep("a", self.action("a")), NotebookStep("b", self.action("b"))]),
             ParallelSteps([NotebookStep("c", self.action("c")), NotebookStep("d", self.action("d"))])],
            [ParallelSteps([NotebookStep(DISPATCH_EXECUTOR_ID, self.action("x")),
                            NotebookStep("y", self.action("y"))])],
            [ParallelSteps([NotebookStep("same", self.action("a")), NotebookStep("same", self.action("b"))])],
        ):
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                await self.run_workflow(steps)
        self.assertEqual(self.calls, [])

    def test_buffered_output_prints_each_step_as_one_block(self):
        output = io.StringIO()
        with redirect_stdout(output):
            with buffered_output("Story: generate") as log:
                log("Conversation created: conv_1")
                log("Response status: completed")
        self.assertEqual(
            output.getvalue(),
            "\n=== Story: generate ===\nConversation created: conv_1\nResponse status: completed\n",
        )
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(RuntimeError):
            with buffered_output("Facts: retrieve") as log:
                log("Conversation created: conv_2")
                raise RuntimeError("failure after partial output")
        self.assertIn("Conversation created: conv_2", output.getvalue())


class NotebookWorkflowWiringTests(unittest.TestCase):
    def setUp(self):
        notebook = json.loads((ROOT / "zolab-ai-agent-demo-win11.ipynb").read_text(encoding="utf-8"))
        self.cells = {cell["id"]: "".join(cell["source"]) for cell in notebook["cells"]}

    def test_main_cell_awaits_workflow_and_keeps_existing_foundry_calls(self):
        source = self.cells["2692d274"]
        self.assertIn("await run_notebook_workflow(", source)
        self.assertIn('name="story-facts"', source)
        for name in ("story", "facts", "persistence"):
            self.assertIn(f'NotebookStep("{name}",', source)
        self.assertIn("get_agent_openai_client(project_client, agent_runtime, main_agent_display_name)", source)
        self.assertIn("run_query_with_auto_approval(", source)
        self.assertEqual(source.count("story_id = append_story(stories_file, story_record)"), 1)

    def test_story_and_facts_run_as_one_parallel_stage_before_persistence(self):
        for notebook_name in ("zolab-ai-agent-demo-win11.ipynb", "zolab-ai-agent-demo-linux.ipynb"):
            notebook = json.loads((ROOT / notebook_name).read_text(encoding="utf-8"))
            source = next(cell for cell in notebook["cells"] if cell["id"] == "2692d274")["source"]
            source = "".join(source)
            with self.subTest(notebook=notebook_name):
                stage = source[source.index("ParallelSteps(["):source.index('NotebookStep("persistence"')]
                self.assertIn('NotebookStep("story", generate_story_step)', stage)
                self.assertIn('NotebookStep("facts", retrieve_facts_step)', stage)
                # Each parallel step buffers its messages; the approval loop logs instead of printing.
                self.assertEqual(source.count("with buffered_output("), 2)
                self.assertEqual(source.count("log=log,"), 3)
                loop = source[source.index("def run_query_with_auto_approval"):source.index("def run_interaction_with_span")]
                self.assertNotIn("print(", loop)
                self.assertIn("ParallelSteps, buffered_output", "".join(
                    "".join(cell["source"]) for cell in notebook["cells"] if cell["id"] == "8b1659dd"
                ))

    def test_sentinel_keeps_its_own_endpoint_and_approval_path(self):
        source = self.cells["ef551c01"]
        self.assertIn("await run_notebook_workflow(", source)
        self.assertIn('name="sentinel"', source)
        self.assertIn('NotebookStep("sentinel",', source)
        self.assertIn("get_agent_openai_client(project_client, agent_runtime, sentinel_agent_display_name)", source)
        self.assertIn('"type": "mcp_approval_response"', source)
        self.assertIn('response_options(agent_runtime, sentinel_agent_reference_payload)', source)

    def test_maf_uses_existing_monitor_pipeline_and_explicit_content_policy(self):
        source = self.cells["3c78effc"]
        self.assertIn("enable_instrumentation(", source)
        self.assertIn("enable_sensitive_data=content_enabled", source)
        self.assertIn("enable_message_events=False", source)
        self.assertNotIn("configure_otel_providers(", source)
        self.assertNotIn("OTLPSpanExporter", source)
        self.assertIn("sampling_ratio=1.0", source)

    def test_disabled_sentinel_makes_no_api_calls(self):
        source = self.cells["ef551c01"]
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_sentinel_stage")
        scope = {"sentinel_workflow_enabled": False}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "sentinel-disabled", "exec"), scope)
        self.assertIsNone(asyncio.run(scope["run_sentinel_stage"]()))


if __name__ == "__main__":
    unittest.main()
