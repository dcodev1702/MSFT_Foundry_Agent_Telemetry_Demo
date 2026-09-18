"""MAF orchestration around the notebook's existing Foundry operations."""

from asyncio import CancelledError
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from uuid import UUID

from agent_framework import WorkflowBuilder, WorkflowContext, WorkflowRunResult, executor
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode


@dataclass(frozen=True)
class NotebookStep:
    name: str
    action: Callable[[], None | Awaitable[None]]


async def run_notebook_workflow(
    *,
    name: str,
    run_id: str,
    session_id: str,
    steps: Sequence[NotebookStep],
    agent_attributes: Mapping[str, str],
) -> WorkflowRunResult:
    """Run each step once, in order, without retrying API or persistence actions."""
    run_id = str(UUID(run_id))
    if not name.strip() or not session_id.strip():
        raise ValueError("A workflow name and telemetry session ID are required.")
    step_names = [step.name for step in steps]
    if not step_names or any(not value.strip() for value in step_names):
        raise ValueError("A workflow requires at least one named step.")
    if len(set(step_names)) != len(step_names):
        raise ValueError("Workflow step names must be unique.")
    if any(not callable(step.action) for step in steps):
        raise TypeError("Every workflow step must have a callable action.")

    attributes = {
        **agent_attributes,
        "demo.run_id": run_id,
        "app.session.id": session_id,
        "app.workflow.name": name,
        "app.orchestration.framework": "microsoft-agent-framework",
    }
    completed: list[str] = []

    def make_executor(step: NotebookStep, *, last: bool):
        @executor(id=step.name)
        async def execute(message: str, ctx: WorkflowContext[str, str]) -> None:
            # Enrich MAF's existing executor span instead of creating another root.
            span = trace.get_current_span()
            span.set_attributes({
                **attributes,
                "app.workflow.step": step.name,
                "app.interaction": step.name,
                "app.interaction.root": True,
            })
            try:
                result = step.action()
                if isawaitable(result):
                    await result
            except CancelledError:
                span.set_attribute("error.type", "CancelledError")
                span.set_attribute("app.workflow.step.status", "cancelled")
                span.set_status(Status(StatusCode.ERROR, "Workflow step cancelled"))
                raise
            except Exception as error:
                span.set_attribute("error.type", type(error).__name__)
                span.set_attribute("app.workflow.step.status", "failed")
                raise
            completed.append(step.name)
            span.set_attribute("app.workflow.step.status", "completed")
            if last:
                await ctx.yield_output(message)
            else:
                await ctx.send_message(message)

        return execute

    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(
        f"notebook.workflow {name}",
        attributes={
            **attributes,
            "app.workflow.root": True,
            "app.workflow.expected_steps": step_names,
        },
    ) as workflow_span:
        try:
            executors = [
                make_executor(step, last=index == len(steps) - 1)
                for index, step in enumerate(steps)
            ]
            builder = WorkflowBuilder(start_executor=executors[0], name=name)
            for source, target in zip(executors, executors[1:]):
                builder.add_edge(source, target)
            workflow = builder.build()
            workflow_span.set_attribute("workflow.id", workflow.id)
            # Only an opaque run ID crosses MAF edges; prompts/results stay in callbacks.
            result = await workflow.run(run_id)
            if completed != step_names or result.get_outputs() != [run_id]:
                raise RuntimeError(f"Workflow {name!r} did not complete every expected step.")
            workflow_span.set_attribute("app.workflow.status", "completed")
            workflow_span.set_status(Status(StatusCode.OK))
            return result
        except CancelledError:
            workflow_span.set_attribute("error.type", "CancelledError")
            workflow_span.set_attribute("app.workflow.status", "cancelled")
            workflow_span.set_status(Status(StatusCode.ERROR, "Workflow cancelled"))
            raise
        except Exception as error:
            workflow_span.set_attribute("error.type", type(error).__name__)
            workflow_span.set_attribute("app.workflow.status", "failed")
            raise
        finally:
            workflow_span.set_attribute("app.workflow.completed_steps", completed)
