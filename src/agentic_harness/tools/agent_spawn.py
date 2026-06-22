import asyncio
import uuid
from typing import Optional

from langchain.tools import tool


_spawned_tasks: dict[str, asyncio.Task] = {}
_spawned_results: dict[str, str] = {}


@tool
def spawn_agent(prompt: str, agent: str = "main") -> str:
    """Spawn a sub-agent to work on a task in the background.

    The sub-agent runs asynchronously and its result will be injected
    into the conversation when complete.

    Args:
        prompt: The task description for the sub-agent.
        agent: Name of the agent profile to use (default: main).
    """
    task_id = f"agent-{uuid.uuid4().hex[:12]}"
    return f"[sub-agent {task_id} started with agent '{agent}']\nTask: {prompt[:200]}"


def register_spawned_task(task_id: str, coro):
    task = asyncio.create_task(coro)
    _spawned_tasks[task_id] = task


def collect_completed_agents() -> list[tuple[str, str, str]]:
    done = []
    for task_id, task in list(_spawned_tasks.items()):
        if task.done():
            try:
                result = task.result()
            except Exception as e:
                result = f"[error: {e}]"
            done.append((task_id, result))
            del _spawned_tasks[task_id]
    return done
