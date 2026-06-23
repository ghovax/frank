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
    justification: str = "",
    risk: Literal["low", "medium", "high"] = "low",
    background: bool = False,
) -> str:
    """Execute a bash command and return its output.

    Always provide a clear justification and risk assessment for the command.

    Args:
        command: The shell command to execute.
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
def read(path: str, first_line: int = 1, last_line: int = 2000) -> str:
    """Read a file from the filesystem.

    Args:
        path: Absolute path to the file.
        first_line: First line to read (1-indexed, default 1).
        last_line: Last line to read (inclusive, default 2000).
    """
    if first_line < 1:
        return "first_line must be >= 1"
    if last_line < first_line:
        return "last_line must be >= first_line"

    resolved_path = Path(path).expanduser().resolve()
    if not resolved_path.exists():
        return json.dumps({"code": "file_not_found", "path": str(path)})
    if not resolved_path.is_file():
        return json.dumps({"code": "not_a_file", "path": str(path)})

    file_size = resolved_path.stat().st_size
    limit = last_line - first_line + 1

    with open(resolved_path) as file_handle:
        if first_line > 1:
            for _ in range(first_line - 1):
                next(file_handle)
        lines = []
        for line_index, line in enumerate(file_handle):
            if line_index >= limit:
                break
            lines.append(line)

    content = "".join(lines)
    return json.dumps({
        "path": str(resolved_path),
        "size": file_size,
        "line_count": len(lines),
        "first_line": first_line,
        "last_line": last_line,
        "content": content.rstrip("\n"),
    })


@tool
def edit(path: str, old_string: str, new_string: str) -> str:
    """Edit a file by replacing the first occurrence of old_string with new_string.

    Args:
        path: Absolute path to the file.
        old_string: Text to search for (must exist exactly once in the file).
        new_string: Text to replace it with.
    """
    resolved_path = Path(path).expanduser().resolve()
    if not resolved_path.exists():
        return json.dumps({"code": "file_not_found", "message": f"File not found: {path}"})
    if not resolved_path.is_file():
        return json.dumps({"code": "not_a_file", "message": f"Not a file: {path}"})

    content = resolved_path.read_text(encoding="utf-8")

    occurrence_count = content.count(old_string)
    if occurrence_count == 0:
        return f"old_string not found in file: {path}"
    if occurrence_count > 1:
        return (
            f"Found {occurrence_count} matches for old_string in {path}. "
            "Provide more surrounding context."
        )

    new_content = content.replace(old_string, new_string, 1)
    resolved_path.write_text(new_content, encoding="utf-8")
    return f"Edited {path}: replaced 1 occurrence"


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
