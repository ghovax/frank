import asyncio
import json
import subprocess
import uuid
from pathlib import Path
from typing import Literal

from langchain.tools import tool


_bash_background_tasks: dict[str, asyncio.Task] = {}
_spawned_agent_tasks: dict[str, asyncio.Task] = {}


@tool
def bash(
    command: str,
    read_only: bool = True,
    justification: str = "",
    risk: Literal["low", "medium", "high"] = "low",
    background: bool = False,
) -> str:
    """Execute a bash command and return its output.

    Always provide a clear justification and risk assessment for the command.
    Use read_only=True for commands that only read state (cat, head, tail, ls,
    grep, find, etc.). Set read_only=False for commands that modify state.

    Args:
        command: The shell command to execute.
        read_only: Whether this command only reads state without modifying it.
        justification: Explain why this command is needed for the task.
        risk: One of "low", "medium", "high" — assess the potential damage.
              Low for read-only commands, medium for modifications,
              high for destructive operations.
        background: If True, runs asynchronously and returns a task identifier.
    """
    if background:
        task_identifier = _start_background_bash(command)
        return json.dumps({"code": "background_started", "task_identifier": task_identifier})
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr
    if not output:
        return ""
    output_path = Path("/tmp") / f"bash-{uuid.uuid4().hex[:12]}.log"
    output_path.write_text(output)
    return json.dumps({
        "code": "bash_completed",
        "output_file": str(output_path),
        "size": len(output),
    })


def _start_background_bash(command: str) -> str:
    task_identifier = f"bg-{len(_bash_background_tasks) + 1}-{id(command) % 10000}"

    async def run():
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        output = (stdout.decode() + stderr.decode()).strip()
        if not output:
            return ""
        output_path = Path("/tmp") / f"bash-{uuid.uuid4().hex[:12]}.log"
        output_path.write_text(output)
        return json.dumps({
            "code": "bash_completed",
            "output_file": str(output_path),
            "size": len(output),
        })

    _bash_background_tasks[task_identifier] = asyncio.create_task(run())
    return task_identifier


def collect_background_bash_results() -> list[tuple[str, str]]:
    completed = []
    for task_identifier, task in list(_bash_background_tasks.items()):
        if task.done():
            try:
                result = task.result()
            except Exception as exception:
                result = str(exception)
            completed.append((task_identifier, result))
            del _bash_background_tasks[task_identifier]
    return completed


@tool
def spawn_agent(prompt: str = "", agent: str = "main", tools: str = "") -> str:
    """Spawn a sub-agent to work on a task in the background.

    The sub-agent runs asynchronously and its result will be injected
    into the conversation when complete.

    Args:
        prompt: The task description for the sub-agent.
        agent: Name of the agent profile to use (default: main).
        tools: Comma-separated list of tool names to restrict (leave empty for all).
    """
    task_identifier = f"agent-{uuid.uuid4().hex[:12]}"
    return (
        f"Started sub-agent ({task_identifier}) using profile '{agent}'.\n"
        f"Task: {prompt[:200]}"
    )


def register_spawned_task(task_identifier: str, coroutine):
    task = asyncio.create_task(coroutine)
    _spawned_agent_tasks[task_identifier] = task


def collect_completed_agents() -> list[tuple[str, str]]:
    completed = []
    for task_identifier, task in list(_spawned_agent_tasks.items()):
        if task.done():
            try:
                result = task.result()
            except Exception as exception:
                result = str(exception)
            completed.append((task_identifier, result))
            del _spawned_agent_tasks[task_identifier]
    return completed


@tool
def write_tasks(tasks: list[dict]) -> str:
    """Create new tasks in the task list. Tasks can depend on each other.

    Use this to break down complex work into steps that can run in
    parallel or sequentially. Tasks with no dependencies can be worked
    on immediately. Tasks with dependencies must wait for their
    dependencies to complete first.

    Args:
        tasks: List of task objects. Each object has:
            - description (required): What needs to be done.
            - dependencies (optional): List of task identifiers this
              task depends on (e.g. ["task-1", "task-2"]).
    """
    return "Handled by orchestrator."


@tool
def update_task(task_id: str, status: str, result: str = "") -> str:
    """Update the status of a task and optionally record a result.

    Args:
        task_id: The task identifier (e.g. 'task-1').
        status: One of 'pending', 'in_progress', 'completed', 'blocked'.
        result: Summary of what was accomplished when marking as completed.
    """
    return "Handled by orchestrator."


@tool
def orchestrate(steps: list[dict]) -> str:
    """Run a graph of agents where each step's output is automatically
    fed to its dependants as JSON appended to their prompts.

    Use ``depends_on`` to define fan-out (parallel execution) and fan-in
    (join barrier). Steps with no explicit ``depends_on`` run in sequence
    (each step depends on the preceding one).

    Args:
        steps: List of step objects. Each step must have:
            - id: A short unique name for this step (e.g. "research").
            - agent: Agent profile name (e.g. "explore", "code", "main").
            - prompt: Task description for this step. The harness
              automatically appends the outputs of all dependency steps
              as JSON to this prompt.
            - depends_on (optional): List of step IDs that this step
              depends on. Omit for sequential execution. Set to an empty
              list ``[]`` for a root step with no dependencies. Multiple
              steps with the same dependency run in parallel; a step
              depending on multiple steps waits for all of them (fan-in).
    """
    task_identifier = f"orch-{uuid.uuid4().hex[:12]}"
    return json.dumps({
        "code": "orchestration_started",
        "task_identifier": task_identifier,
        "step_count": len(steps),
        "steps": [{"id": step["id"], "agent": step["agent"]} for step in steps],
    })


def cancel_all_background_tasks() -> None:
    for task_identifier, task in list(_bash_background_tasks.items()):
        task.cancel()
        del _bash_background_tasks[task_identifier]
    for task_identifier, task in list(_spawned_agent_tasks.items()):
        task.cancel()
        del _spawned_agent_tasks[task_identifier]
