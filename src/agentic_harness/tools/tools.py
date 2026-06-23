import asyncio
import subprocess
import uuid
from pathlib import Path

from langchain.tools import tool


_bash_background_tasks: dict[str, asyncio.Task] = {}
_spawned_agent_tasks: dict[str, asyncio.Task] = {}


@tool
def bash(
    command: str,
    justification: str = "",
    risk: str = "",
    background: bool = False,
) -> str:
    """Execute a bash command and return its output.

    Always provide a clear justification and risk assessment for the command.

    Args:
        command: The shell command to execute.
        justification: Explain why this command is needed for the task.
        risk: Describe potential impact — for example "modifies files",
              "deletes data", "no risk" for read-only commands.
        background: If True, runs asynchronously and returns a task identifier.
    """
    if background:
        task_identifier = _start_background_bash(command)
        return f"Background task started ({task_identifier})."
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr
    if len(output) > 100_000:
        output = output[:100_000] + "\n... [truncated]"
    return output if output else "(no output)"


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
        if len(output) > 100_000:
            output = output[:100_000] + "\n... [truncated]"
        return output or "(no output)"

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
def read(path: str, offset: int = 0, limit: int = 2000) -> str:
    """Read a file from the filesystem.

    Args:
        path: Absolute path to the file.
        offset: Line number to start reading from (1-indexed, default 0 = start).
        limit: Maximum number of lines to read (default 2000).
    """
    resolved_path = Path(path).expanduser().resolve()
    if not resolved_path.exists():
        return f"File not found: {path}"
    if not resolved_path.is_file():
        return f"Not a file: {path}"

    file_size = resolved_path.stat().st_size

    with open(resolved_path) as file_handle:
        if offset > 0:
            for _ in range(offset - 1):
                next(file_handle)
        lines = []
        for line_index, line in enumerate(file_handle):
            if line_index >= limit:
                break
            lines.append(line)

    content = "".join(lines)
    header = f"File: {resolved_path} ({file_size} bytes, showing {len(lines)} lines from offset {offset})\n"
    return header + content


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
        return f"File not found: {path}"
    if not resolved_path.is_file():
        return f"Not a file: {path}"

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
def spawn_agent(prompt: str, agent: str = "main", tools: str = "") -> str:
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
