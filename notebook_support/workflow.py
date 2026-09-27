"""MAF orchestration around the notebook's existing Foundry operations."""

import asyncio
from asyncio import CancelledError
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from inspect import isawaitable, iscoroutinefunction
from uuid import UUID

from agent_framework import WorkflowBuilder, WorkflowContext, WorkflowRunResult, executor
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

# Executor that hands the run ID to the steps of a leading parallel stage. It is workflow plumbing,
# tagged app.workflow.dispatch, not an interaction root.
DISPATCH_EXECUTOR_ID = "fan-out"


@dataclass(frozen=True)
class NotebookStep:
    """One named workflow step and the sync or async notebook action it runs."""

    name: str
    action: Callable[[], None | Awaitable[None]]


@dataclass(frozen=True)
class ParallelSteps:
    """Independent steps that run at the same time; the next stage starts after all of them finish.

    Sync actions run in worker threads that carry the current OpenTelemetry context, so their spans
    nest under their own executor span. A thread cannot be interrupted, so when a sibling fails the
    other steps' calls still finish before the workflow reports the failure.
    """

    steps: tuple[NotebookStep, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        if len(self.steps) < 2 or not all(isinstance(step, NotebookStep) for step in self.steps):
            raise TypeError("ParallelSteps needs at least two NotebookStep entries.")


@contextmanager
def buffered_output(title: str) -> Iterator[Callable[[str], None]]:
    """Collect one step's messages and print them as one block, so parallel output never mixes."""
    lines = [f"\n=== {title} ==="]
    try:
        yield lines.append
    finally:
        print("\n".join(lines), flush=True)


async def _run_action(action: Callable[[], None | Awaitable[None]], *, threaded: bool) -> None:
    if not threaded or iscoroutinefunction(action):
        result = action()
        if isawaitable(result):
            await result
        return
    # A worker thread keeps the event loop free for sibling steps; to_thread copies the context.
    call = asyncio.ensure_future(asyncio.to_thread(action))
    try:
        result = await asyncio.shield(call)
    except CancelledError:
        # Let the uninterruptible call finish so its spans and output close inside this run.
        await asyncio.wait({call})
        if not call.cancelled():
            call.exception()
        raise
    if isawaitable(result):
        await result


def _stages(steps: Sequence[NotebookStep | ParallelSteps]) -> list[tuple[NotebookStep, ...]]:
    stages = [stage.steps if isinstance(stage, ParallelSteps) else (stage,) for stage in steps]
    if not stages or any(not isinstance(step, NotebookStep) for stage in stages for step in stage):
        raise ValueError("A workflow requires at least one named step.")
    for current, following in zip(stages, stages[1:]):
        if len(current) > 1 and len(following) > 1:
            raise ValueError("Separate two parallel stages with a single step.")
    return stages


async def run_notebook_workflow(
    *,
    name: str,
    run_id: str,
    session_id: str,
    steps: Sequence[NotebookStep | ParallelSteps],
    agent_attributes: Mapping[str, str],
) -> WorkflowRunResult:
    """Run each step once, in stage order, without retrying API or persistence actions.

    A ParallelSteps stage runs its steps at the same time; the next stage waits for all of them.
    """
    run_id = str(UUID(run_id))
    if not name.strip() or not session_id.strip():
        raise ValueError("A workflow name and telemetry session ID are required.")
    stages = _stages(steps)
    step_names = [step.name for stage in stages for step in stage]
    if any(not value.strip() for value in step_names):
        raise ValueError("A workflow requires at least one named step.")
    if len(set(step_names)) != len(step_names):
        raise ValueError("Workflow step names must be unique.")
    if DISPATCH_EXECUTOR_ID in step_names:
        raise ValueError(f"{DISPATCH_EXECUTOR_ID!r} is reserved for the parallel dispatch step.")
    if any(not callable(step.action) for stage in stages for step in stage):
        raise TypeError("Every workflow step must have a callable action.")

    attributes = {
        **agent_attributes,
        "demo.run_id": run_id,
        "app.session.id": session_id,
        "app.workflow.name": name,
        "app.orchestration.framework": "microsoft-agent-framework",
    }
    completed: list[str] = []

    def make_executor(step: NotebookStep, *, fan_in: bool, last: bool, threaded: bool):
        # A step after a parallel stage receives one run ID from each parallel step.
        @executor(id=step.name, input=list[str] if fan_in else str)
        async def execute(message, ctx: WorkflowContext[str, str]) -> None:
            # Enrich MAF's existing executor span instead of creating another root.
            span = trace.get_current_span()
            span.set_attributes({
                **attributes,
                "app.workflow.step": step.name,
                "app.interaction": step.name,
                "app.interaction.root": True,
            })
            try:
                received = message if fan_in else [message]
                if not received or any(value != run_id for value in received):
                    raise RuntimeError(f"Workflow step {step.name!r} got an unexpected run ID.")
                await _run_action(step.action, threaded=threaded)
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
                await ctx.yield_output(run_id)
            else:
                await ctx.send_message(run_id)

        return execute

    @executor(id=DISPATCH_EXECUTOR_ID)
    async def dispatch(message: str, ctx: WorkflowContext[str]) -> None:
        trace.get_current_span().set_attributes({**attributes, "app.workflow.dispatch": True})
        await ctx.send_message(message)

    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(
        f"notebook.workflow {name}",
        attributes={
            **attributes,
            "app.workflow.root": True,
            "app.workflow.expected_steps": step_names,
            "app.workflow.stages": ["+".join(step.name for step in stage) for stage in stages],
        },
    ) as workflow_span:
        try:
            executors = [
                [
                    make_executor(
                        step, fan_in=index > 0 and len(stages[index - 1]) > 1,
                        last=index == len(stages) - 1, threaded=len(stage) > 1,
                    )
                    for step in stage
                ]
                for index, stage in enumerate(stages)
            ]
            start = dispatch if len(executors[0]) > 1 else executors[0][0]
            builder = WorkflowBuilder(start_executor=start, name=name)
            if start is dispatch:
                builder.add_fan_out_edges(dispatch, executors[0])
            for sources, targets in zip(executors, executors[1:]):
                if len(sources) > 1:
                    builder.add_fan_in_edges(sources, targets[0])
                elif len(targets) > 1:
                    builder.add_fan_out_edges(sources[0], targets)
                else:
                    builder.add_edge(sources[0], targets[0])
            workflow = builder.build()
            workflow_span.set_attribute("workflow.id", workflow.id)
            # Only an opaque run ID crosses MAF edges; prompts/results stay in callbacks.
            result = await workflow.run(run_id)
            expected_outputs = [run_id] * len(stages[-1])
            if sorted(completed) != sorted(step_names) or result.get_outputs() != expected_outputs:
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
