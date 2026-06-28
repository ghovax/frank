import asyncio
import atexit
import json
import re
import signal
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable, Literal

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
        identifier = f"{self._prefix}-{uuid.uuid4().hex}"
        if output_path is None:
            output_path = self._output_directory / f"{self._prefix}-{uuid.uuid4().hex}.log"
        task = asyncio.create_task(coroutine)
        self._tasks[identifier] = (task, output_path)
        return identifier, output_path

    def register(self, task: asyncio.Task, output_path: Path | None = None, identifier: str | None = None) -> str:
        if identifier is None:
            identifier = f"{self._prefix}-{uuid.uuid4().hex}"
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
_mcp_client_manager: Any | None = None


def set_exa_client(client: Exa | None) -> None:
    global _exa_client
    _exa_client = client


def set_mcp_client_manager(manager: Any | None) -> None:
    global _mcp_client_manager
    _mcp_client_manager = manager


def _require_mcp_client_manager():
    if _mcp_client_manager is None:
        raise RuntimeError("MCP is not configured.")
    return _mcp_client_manager


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
    output_path = Path("/tmp") / f"bash-{uuid.uuid4().hex}.log"

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

    output_path = Path("/tmp") / f"search-{uuid.uuid4().hex}.log"

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
                    entry["summary"] = result.text
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
async def list_mcp_tools(server: str = "", justification: str = "") -> str:
    """List tools exposed by configured MCP servers.

    Args:
        server: Optional configured MCP server name. Leave empty to list every
            enabled server.
        justification: A concise, user-facing reason for inspecting MCP tools.
    """
    try:
        result = await _require_mcp_client_manager().list_tools(server)
        return json.dumps(result)
    except Exception as exception:
        return json.dumps({"code": "mcp_list_tools_error", "message": str(exception)})


@tool
async def call_mcp_tool(
    server: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    read_only: bool = True,
    justification: str = "",
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Call a tool exposed by a configured MCP server.

    Args:
        server: Configured MCP server name.
        tool_name: Tool name as advertised by list_mcp_tools.
        arguments: JSON object matching the MCP tool input schema.
        read_only: Whether this MCP tool call only reads state.
        justification: A concise, user-facing reason for the tool call.
        risk: One of "low", "medium", "high" for non-read-only calls.
    """
    try:
        result = await _require_mcp_client_manager().call_tool(server, tool_name, arguments or {})
        return json.dumps(result)
    except Exception as exception:
        return json.dumps({"code": "mcp_call_tool_error", "message": str(exception)})


async def call_mcp_tool_with_events(
    server: str,
    tool_name: str,
    arguments: dict[str, Any] | None,
    event_callback,
) -> dict[str, Any]:
    return await _require_mcp_client_manager().call_tool(
        server,
        tool_name,
        arguments or {},
        event_callback=event_callback,
    )


@tool
async def list_mcp_resources(server: str = "", justification: str = "") -> str:
    """List resources exposed by configured MCP servers.

    Args:
        server: Optional configured MCP server name. Leave empty to list every
            enabled server.
        justification: A concise, user-facing reason for inspecting resources.
    """
    try:
        result = await _require_mcp_client_manager().list_resources(server)
        return json.dumps(result)
    except Exception as exception:
        return json.dumps({"code": "mcp_list_resources_error", "message": str(exception)})


@tool
async def read_mcp_resource(server: str, uri: str, justification: str = "") -> str:
    """Read a resource exposed by a configured MCP server.

    Args:
        server: Configured MCP server name.
        uri: Resource URI as advertised by list_mcp_resources.
        justification: A concise, user-facing reason for reading the resource.
    """
    try:
        result = await _require_mcp_client_manager().read_resource(server, uri)
        return json.dumps(result)
    except Exception as exception:
        return json.dumps({"code": "mcp_read_resource_error", "message": str(exception)})


@tool
def spawn_agent(prompt: str = "", agent: str = "assistant", read_only: bool = False, justification: str = "") -> str:
    """Delegate a task to another agent (a real A2A call to its endpoint).

    The sub-agent runs as a related A2A task in the same context. Its activity
    streams live, and its structured deliverable (the completed A2A task with its
    artifact) is returned as this tool's result, so you can read it and decide
    what to do next — including spawning further agents that build on it. To run
    several agents at once, call this tool multiple times in one response.

    Args:
        prompt: The task for the sub-agent. State the goal clearly and, when it
            should build on or coordinate with other agents, name their task ids.
            Include the expected return shape: findings, evidence, uncertainty,
            and recommended next action and all else relevant.
        agent: Name of the agent profile to delegate to (e.g. 'reader',
            'builder', 'scout').
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


_WIDGET_UPDATE_MODES = {"append", "replace", "update", "upsert"}

# Injected into model-authored widget HTML. Two jobs, both over the same
# back-channel the front end already listens on: (1) report an uncaught error or
# rejected promise as a structured `render_error` event so the model can see the
# failure and iterate; (2) report the document's content height as a
# `__widget_resize` event so the front end can size the widget automatically —
# the model never has to guess a height.
_WIDGET_RUNTIME = (
    "<script>(function(){"
    "function send(event,data){try{window.parent.postMessage("
    "{source:'harness-widget',event:event,data:data},'*');}catch(error){}}"
    "function report(message,source){send('render_error',{message:String(message),source:source||''});}"
    "window.addEventListener('error',function(event){"
    "report(event.message||(event.error&&event.error.message)||'script error',event.filename||'');});"
    "window.addEventListener('unhandledrejection',function(event){"
    "report((event.reason&&event.reason.message)||String(event.reason),'promise');});"
    "function measure(){var body=document.body;var root=document.documentElement;"
    "var height=Math.max(root?root.scrollHeight:0,body?body.scrollHeight:0,body?body.offsetHeight:0);"
    "if(height>0){send('__widget_resize',{height:height});}}"
    "function start(){measure();if(window.ResizeObserver){var observer=new ResizeObserver(measure);"
    "observer.observe(document.documentElement);if(document.body){observer.observe(document.body);}}}"
    "if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',start);}else{start();}"
    "window.addEventListener('load',measure);setTimeout(measure,300);setTimeout(measure,1000);"
    "})();</script>"
)


def _widget_update_mode(value: str) -> str:
    normalized = (value or "append").strip().lower()
    if normalized == "new":
        return "append"
    return normalized if normalized in _WIDGET_UPDATE_MODES else "append"


def _inject_widget_runtime(html: str) -> str:
    """Place the widget runtime (error + resize reporting) as early as possible in
    the document so it catches failures in the model's own scripts and can size the
    widget. Falls back to prepending when there is no recognizable
    ``<head>``/``<body>``/``<html>`` insertion point."""
    for pattern in (r"<head[^>]*>", r"<body[^>]*>", r"<html[^>]*>"):
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return html[: match.end()] + _WIDGET_RUNTIME + html[match.end() :]
    return _WIDGET_RUNTIME + html


def build_widget_result(
    html: str,
    title: str = "Widget",
    height: int = 420,
    artifact_id: str = "",
    artifact_update_mode: str = "append",
    artifact_target_id: str = "",
    summary: str = "",
) -> dict[str, Any]:
    """Build the tool result for a model-authored HTML widget.

    Mirrors the MCP artifact contract: the full ``html`` rides in ``artifacts``
    (streamed to the UI and persisted for replay), while ``model_context`` carries
    only compact metadata so the rendered markup never re-enters the model's
    context. Kept pure so it can be dispatched from the agent runtime.
    """
    identifier = (artifact_id or "").strip() or f"widget-{uuid.uuid4().hex[:10]}"
    mode = _widget_update_mode(artifact_update_mode)
    target_id = (artifact_target_id or "").strip() or identifier
    # Height defaults to automatic — the widget reports its own content height and
    # the front end sizes to it. A positive value pins a fixed height instead.
    try:
        requested_height = int(height)
    except (TypeError, ValueError):
        requested_height = 0
    artifact_height = max(120, min(900, requested_height)) if requested_height > 0 else "auto"
    widget_summary = (summary or "").strip() or f'Rendered HTML widget "{title}".'

    artifact = {
        "artifact_id": identifier,
        "artifact_target_id": target_id,
        "artifact_update_mode": mode,
        "type": "html",
        "title": title,
        "html": _inject_widget_runtime(html),
        "height": artifact_height,
        "summary": widget_summary,
    }
    model_context = {
        "code": "widget_rendered",
        "summary": widget_summary,
        "artifacts": [
            {
                "artifact_id": identifier,
                "artifact_target_id": target_id,
                "artifact_update_mode": mode,
                "type": "html",
                "title": title,
                "height": artifact_height,
            }
        ],
    }
    return {"artifacts": [artifact], "model_context": model_context}


@tool
def render_widget(
    html: str,
    title: str = "Widget",
    height: int = 0,
    artifact_id: str = "",
    artifact_update_mode: str = "append",
    artifact_target_id: str = "",
    summary: str = "",
) -> str:
    """Render a self-contained HTML widget directly in the chat, outside the tool card.

    This is the general-purpose visual tool — a Swiss-army knife for showing the
    user almost anything: an image, a table, a chart, a map, a small interactive UI,
    a formatted document. You author a complete HTML document; it renders in a
    sandboxed iframe. External scripts and assets load over HTTPS, so reach for an
    existing web library rather than hand-rolling: a `<script>`/`<link>` from a CDN
    (Chart.js, D3, Plotly, Leaflet, KaTeX, highlight.js, …) or just an `<img>` tag.
    Think "which library already does this?" first. The markup is shown to the user
    but stripped from your own context, so a large document costs you nothing.

    Height is automatic — the widget sizes to its content, so you do not need to
    pass `height` (set it only to pin a fixed height, e.g. for a full-bleed map).

    To make a widget interactive, post events back to the agent from inside the
    iframe — each becomes a structured `widget_event` turn you can react to:

        window.parent.postMessage(
            {source: "harness-widget", event: "<name>", data: {/* ... */}},
            "*"
        );

    Uncaught errors and rejected promises in the widget are reported back to you
    automatically as a `render_error` event, so you can see what broke and fix it.

    Args:
        html: A complete HTML document (``<!doctype html>`` … ``</html>``).
        title: Short caption shown above the widget. May contain markdown.
        height: Optional fixed height in pixels (120-900). Omit for automatic
            sizing to the content (the default).
        artifact_id: Stable id for this widget; generated when omitted. Reuse it
            with ``artifact_update_mode="replace"`` to refresh a widget in place.
        artifact_update_mode: ``append`` renders a new widget, ``replace``/``update``
            refresh an existing one, ``upsert`` refreshes if present else appends.
        artifact_target_id: Existing widget id to refresh; defaults to ``artifact_id``.
        summary: One-line description of what the widget shows.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


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
