import asyncio
import subprocess
from typing import Optional

from langchain.tools import tool


_bash_background_tasks: dict[str, asyncio.Task] = {}


@tool
def bash(command: str, background: bool = False) -> str:
    """Execute a bash command and return its output.

    Use background=True for long-running commands that should not block.
    Background tasks return a task ID; results are fetched automatically.
    """
    if background:
        task_id = _start_background_bash(command)
        return f"[background task {task_id} started]"
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
    task_id = f"bg-{len(_bash_background_tasks) + 1}-{id(command) % 10000}"

    async def run():
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = (stdout.decode() + stderr.decode()).strip()
        if len(output) > 100_000:
            output = output[:100_000] + "\n... [truncated]"
        return output or "(no output)"

    _bash_background_tasks[task_id] = asyncio.create_task(run())
    return task_id


def collect_background_bash_results() -> list[tuple[str, str]]:
    done = []
    for task_id, task in list(_bash_background_tasks.items()):
        if task.done():
            try:
                result = task.result()
            except Exception as e:
                result = f"[error: {e}]"
            done.append((task_id, result))
            del _bash_background_tasks[task_id]
    return done
