import asyncio
import atexit
import json
import signal
import sys
import uuid
from pathlib import Path
from typing import Literal

from exa_py import Exa
from langchain.tools import tool


class TaskRegistry:
    """Manages a collection of background async tasks.

    Each task writes its output to a file and is identified by a unique
    string identifier prefixed with the registry's prefix.
    """

    def __init__(self, prefix: str):
        self._prefix = prefix
        self._output_directory = Path("/tmp")
        self._tasks: dict[str, tuple[asyncio.Task, Path | None]] = {}

    def start(self, coroutine, output_path: Path | None = None) -> tuple[str, Path]:
        identifier = f"{self._prefix}-{uuid.uuid4().hex[:12]}"
        if output_path is None:
            output_path = self._output_directory / f"{self._prefix}-{uuid.uuid4().hex[:12]}.log"
        task = asyncio.create_task(coroutine)
        self._tasks[identifier] = (task, output_path)
        return identifier, output_path

    def register(self, task: asyncio.Task, output_path: Path | None = None, identifier: str | None = None) -> str:
        if identifier is None:
            identifier = f"{self._prefix}-{uuid.uuid4().hex[:12]}"
        self._tasks[identifier] = (task, output_path)
        return identifier

    def collect_completed(self) -> list[tuple[str, str]]:
        completed = []
        for identifier, (task, output_path) in list(self._tasks.items()):
            if task.done():
                try:
                    result = task.result()
                except Exception as exception:
                    result = str(exception)
                completed.append((identifier, result))
                del self._tasks[identifier]
        return completed

    def cancel_all(self) -> None:
        for identifier, (task, _) in list(self._tasks.items()):
            task.cancel()
            del self._tasks[identifier]

    @property
    def active_count(self) -> int:
        return sum(1 for task, _ in self._tasks.values() if not task.done())

    def list_active(self) -> list[str]:
        return [identifier for identifier, (task, _) in self._tasks.items() if not task.done()]


bash_tasks = TaskRegistry("bg")
web_tasks = TaskRegistry("search")
spawned_tasks = TaskRegistry("agent")

_exa_client: Exa | None = None


def set_exa_client(client: Exa | None) -> None:
    global _exa_client
    _exa_client = client


@tool
async def bash(
    command: str,
    read_only: bool = True,
    justification: str = "",
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Execute a bash command and return its output.

    Fast commands (under ~2s) return output directly.
    Slow commands return immediately with a task identifier and output file
    path — the result is auto-injected into the conversation when finished.

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
    """
    output_path = Path("/tmp") / f"bash-{uuid.uuid4().hex[:12]}.log"

    async def run() -> str:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        pid = process.pid

        async def write_stream(stream, handle):
            while True:
                line = await stream.readline()
                if not line:
                    break
                handle.write(line.decode())
                handle.flush()

        with output_path.open("w") as file_handle:
            await asyncio.gather(
                write_stream(process.stdout, file_handle),
                write_stream(process.stderr, file_handle),
            )

        await process.wait()
        output = output_path.read_text()
        if not output:
            return json.dumps({"code": "bash_completed", "output": "", "pid": pid})
        if len(output) > 1 << 17:
            return json.dumps({
                "code": "bash_completed",
                "output_file": str(output_path),
                "pid": pid,
                "size": len(output),
            })
        return json.dumps({
            "code": "bash_completed",
            "output": output,
            "pid": pid,
            "size": len(output),
        })

    task = asyncio.create_task(run())
    task_identifier = bash_tasks.register(task, output_path)
    return json.dumps({
        "code": "bash_started",
        "task_identifier": task_identifier,
        "output_file": str(output_path),
    })


def collect_background_bash_results() -> list[tuple[str, str]]:
    return bash_tasks.collect_completed()


@tool
async def web_search(
    query: str,
    justification: str = "",
    result_count: int = 5,
) -> str:
    """Search the web using Exa. Returns a list of results with titles, URLs, and summaries.

    The search runs asynchronously — the result is auto-injected into the
    conversation when ready. You can start multiple searches concurrently.

    Use this when you need current information from the internet, recent events,
    or external knowledge not available in the training data.

    Args:
        query: The search query.
        justification: A concise, user-facing description of why this search is needed.
        result_count: Number of results to return (1-10, default 5).
    """
    client = _exa_client
    if client is None:
        return json.dumps({"code": "web_search_error", "message": "Web search is not configured."})

    output_path = Path("/tmp") / f"search-{uuid.uuid4().hex[:12]}.log"

    async def run() -> str:
        try:
            results = await asyncio.to_thread(
                client.search,
                query,
                num_results=min(result_count, 10),
                contents={"text": True},
            )
            entries = []
            for result in results.results:
                entry = {"title": result.title, "url": result.url}
                if result.text:
                    entry["summary"] = result.text[:500]
                if result.published_date:
                    entry["published_date"] = result.published_date
                entries.append(entry)
            payload = json.dumps({
                "code": "web_search_completed",
                "query": query,
                "results": entries,
            })
            output_path.write_text(payload)
            return payload
        except Exception as exception:
            payload = json.dumps({"code": "web_search_error", "message": str(exception)})
            output_path.write_text(payload)
            return payload

    task = asyncio.create_task(run())
    task_identifier = web_tasks.register(task, output_path)
    return json.dumps({
        "code": "web_search_started",
        "task_identifier": task_identifier,
        "output_file": str(output_path),
    })


def collect_web_search_results() -> list[tuple[str, str]]:
    return web_tasks.collect_completed()


@tool
def spawn_agent(prompt: str = "", agent: str = "main", tools: str = "", justification: str = "") -> str:
    """Spawn a sub-agent to work on a task in the background.

    The sub-agent runs asynchronously and its result will be injected
    into the conversation when complete.

    Args:
        prompt: The task description for the sub-agent.
        agent: Name of the agent profile to use (default: main).
        tools: Comma-separated list of tool names to restrict (leave empty for all).
        justification: A concise, user-facing description of what this
            sub-agent will do — shown directly as the label for this call.
    """
    task_identifier = f"agent-{uuid.uuid4().hex[:12]}"
    return (
        f"Started sub-agent ({task_identifier}) using profile '{agent}'.\n"
        f"Task: {prompt[:200]}"
    )


def register_spawned_task(task_identifier: str, coroutine):
    task = asyncio.create_task(coroutine)
    spawned_tasks.register(task, identifier=task_identifier)


def collect_completed_agents() -> list[tuple[str, str]]:
    return spawned_tasks.collect_completed()


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
def update_tasks(updates: list[dict]) -> str:
    """Update the status of one or more tasks at once.

    Args:
        updates: List of update objects. Each object has:
            - task_id (required): The task identifier (e.g. 'task-1').
            - status (required): One of 'pending', 'in_progress', 'completed', 'blocked'.
            - result (optional): Summary of what was accomplished when marking as completed.
    """
    return "Handled by orchestrator."


@tool
def orchestrate(steps: list[dict], justification: str = "") -> str:
    """Run a graph of agents where each step's output is automatically
    fed to its dependants as JSON appended to their prompts.

    Use ``depends_on`` to define fan-out (parallel execution) and fan-in
    (join barrier). Steps with no explicit ``depends_on`` run in sequence
    (each step depends on the preceding one).

    Args:
        justification: A concise, user-facing description of what this
            orchestration accomplishes — it is shown directly as the label
            for this call (e.g. "Gathering BBC news across four sections").
            Write a short phrase, not a generic placeholder.
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
    bash_tasks.cancel_all()
    web_tasks.cancel_all()
    spawned_tasks.cancel_all()


def _cleanup_on_exit():
    cancel_all_background_tasks()


atexit.register(_cleanup_on_exit)

for _sig in (signal.SIGTERM, signal.SIGHUP):
    signal.signal(_sig, lambda signum, frame: (cancel_all_background_tasks(), sys.exit(1)))
