import asyncio
import atexit
import json
import signal
import sys
import uuid
from pathlib import Path
from typing import Iterable, Literal

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

    def collect_completed(self, identifiers: Iterable[str] | None = None) -> list[tuple[str, str]]:
        allowed_identifiers = set(identifiers) if identifiers is not None else None
        completed = []
        for identifier, (task, output_path) in list(self._tasks.items()):
            if allowed_identifiers is not None and identifier not in allowed_identifiers:
                continue
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

    def active_count_for(self, identifiers: Iterable[str]) -> int:
        allowed_identifiers = set(identifiers)
        return sum(
            1
            for identifier, (task, _) in self._tasks.items()
            if identifier in allowed_identifiers and not task.done()
        )

    def list_active(self, identifiers: Iterable[str] | None = None) -> list[str]:
        allowed_identifiers = set(identifiers) if identifiers is not None else None
        return [
            identifier
            for identifier, (task, _) in self._tasks.items()
            if (allowed_identifiers is None or identifier in allowed_identifiers) and not task.done()
        ]


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
        process_id = process.pid

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
            return json.dumps({"code": "bash_completed", "output": "", "pid": process_id})
        if len(output) > 1 << 17:
            return json.dumps({
                "code": "bash_completed",
                "output_file": str(output_path),
                "pid": process_id,
                "size": len(output),
            })
        return json.dumps({
            "code": "bash_completed",
            "output": output,
            "pid": process_id,
            "size": len(output),
        })

    task = asyncio.create_task(run())
    task_identifier = bash_tasks.register(task, output_path)
    return json.dumps({
        "code": "bash_started",
        "task_identifier": task_identifier,
        "output_file": str(output_path),
    })


def collect_background_bash_results(identifiers: Iterable[str] | None = None) -> list[tuple[str, str]]:
    return bash_tasks.collect_completed(identifiers)


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


def collect_web_search_results(identifiers: Iterable[str] | None = None) -> list[tuple[str, str]]:
    return web_tasks.collect_completed(identifiers)


@tool
def spawn_agent(prompt: str = "", agent: str = "researcher", read_only: bool = False, justification: str = "") -> str:
    """Delegate a task to another agent (a real A2A call to its endpoint).

    The sub-agent runs as a related A2A task in the same context. Its activity
    streams live, and its structured deliverable (the completed A2A task with its
    artifact) is returned as this tool's result, so you can read it and decide
    what to do next — including spawning further agents that build on it. To run
    several agents at once, call this tool multiple times in one response.

    Args:
        prompt: The task for the sub-agent. State the goal clearly and, when it
            should build on or coordinate with other agents, name their task ids.
        agent: Name of the agent profile to delegate to (e.g. 'explorer',
            'coder', 'researcher').
        read_only: Force the sub-agent into read-only mode — it may only run
            read-only commands and cannot modify the system or write files. Use
            for investigation/research sub-agents that should report back rather
            than make changes.
        justification: A concise, user-facing description of what this sub-agent
            will do — shown directly as the label for this call.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


def register_spawned_task(task_identifier: str, coroutine):
    task = asyncio.create_task(coroutine)
    spawned_tasks.register(task, identifier=task_identifier)


def collect_completed_agents(identifiers: Iterable[str] | None = None) -> list[tuple[str, str]]:
    return spawned_tasks.collect_completed(identifiers)


@tool
def read_task(task_id: str = "", justification: str = "") -> str:
    """Read another A2A task in this context — a sibling or sub-agent task — by
    its id, returning its current status and artifact (deliverable).

    Use this to coordinate with agents working alongside you in the same context:
    check whether a sibling has finished and read what it produced, then build on
    it. Task ids are the ones returned when an agent is spawned.

    Args:
        task_id: The id of the task to read.
        justification: A concise, user-facing description of why you are reading
            this task — shown as the label for this call.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


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
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def update_tasks(updates: list[dict]) -> str:
    """Update the status of one or more tasks at once.

    Args:
        updates: List of update objects. Each object has:
            - task_id (required): The task identifier (e.g. 'task-1').
            - status (required): One of 'pending', 'in_progress', 'completed', 'blocked'.
            - result (optional): Summary of what was accomplished when marking as completed.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def update_goal(
    goal: str = "",
    status: Literal["active", "satisfied", "cleared"] = "active",
    justification: str = "",
) -> str:
    """Set, replace, satisfy, or clear the single active goal for this turn.

    A goal is not a task list. It is the top-level completion contract the
    harness injects back into your context until you explicitly satisfy or clear
    it. Use it when a user request has a concrete outcome that must not be lost
    while you run tools, delegate, or continue across multiple model passes.

    Args:
        goal: The goal text to set when status is "active". Leave empty when
            marking the current goal as "satisfied" or "cleared".
        status: "active" sets/replaces the goal, "satisfied" removes it because
            the requested outcome is done, and "cleared" removes it because it
            is obsolete or no longer applicable.
        justification: A concise, user-facing reason for this update.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


def cancel_all_background_tasks() -> None:
    bash_tasks.cancel_all()
    web_tasks.cancel_all()
    spawned_tasks.cancel_all()


def _cleanup_on_exit():
    cancel_all_background_tasks()


atexit.register(_cleanup_on_exit)

for termination_signal in (signal.SIGTERM, signal.SIGHUP):
    signal.signal(termination_signal, lambda signum, frame: (cancel_all_background_tasks(), sys.exit(1)))
