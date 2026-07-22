"""A2A-typed agent handoffs.

Every agent run (a spawned agent) is modelled as an
A2A ``Task``: a typed lifecycle (``working`` / ``completed`` / ``failed`` / ...)
whose durable deliverable is an ``Artifact`` composed of typed ``Part``s. The
A2A Task is the single canonical object for an agent's outcome — it is what gets
persisted to the database, streamed to the client, handed to downstream agents,
and rendered by the UI. Nothing is flattened into a bespoke shape; the structured
A2A object travels end to end so a run is fully reproducible at every layer.
"""

from __future__ import annotations

from a2a.types import Artifact, Part, Task, TaskState, TaskStatus, TextPart

from daisy.identifiers import new_id


def build_artifact(step_id: str, agent: str, text: str) -> Artifact:
    """Wrap an agent's final report as its deliverable artifact."""
    return Artifact(
        artifact_id=new_id("artifact"),
        name=f"{step_id} result" if step_id else "result",
        description=f"Final result produced by agent '{agent}'." if agent else "",
        parts=[Part(root=TextPart(text=text))],
    )


def build_task(
    step_id: str,
    agent: str,
    state: TaskState,
    text: str = "",
) -> Task:
    """Build the canonical A2A Task carrying an agent's outcome.

    A successful run produces a ``completed`` task with one text artifact; a
    failed/cancelled run carries the terminal state and whatever text was
    produced, so downstream agents still receive a legible explanation.
    """
    return Task(
        id=f"task-{step_id}" if step_id else new_id("task"),
        context_id=new_id("ctx"),
        status=TaskStatus(state=state),
        artifacts=[build_artifact(step_id, agent, text)] if text else [],
    )


def serialize_task(task: Task) -> dict:
    """Canonical A2A JSON for a task (camelCase aliases, no null fields).

    This is the on-the-wire / in-database / handoff representation — identical to
    what an A2A endpoint would emit, so it stays interoperable and reproducible.
    The structured task is passed around verbatim; it is never flattened to a
    bare string.
    """
    return task.model_dump(by_alias=True, exclude_none=True, mode="json")
