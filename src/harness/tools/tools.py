import asyncio
import atexit
import json
import os
import re
import signal
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from exa_py import Exa
from langchain.tools import tool

from harness.identifiers import new_id


class TaskRegistry:
    """Manages a collection of background async tasks.

    Each task writes its output to a file and is identified by a unique
    string identifier prefixed with the registry's prefix.
    """

    def __init__(self, prefix: str):
        self._prefix = prefix
        self._output_directory = Path("/tmp")
        self._tasks: dict[str, tuple[asyncio.Task, Path | None, Callable[[], None] | None]] = {}

    def start(self, coroutine, output_path: Path | None = None, cancel_callback: Callable[[], None] | None = None) -> tuple[str, Path]:
        identifier = new_id(self._prefix)
        if output_path is None:
            output_path = self._output_directory / f"{identifier}.log"
        task = asyncio.create_task(coroutine)
        self._tasks[identifier] = (task, output_path, cancel_callback)
        return identifier, output_path

    def register(
        self,
        task: asyncio.Task,
        output_path: Path | None = None,
        identifier: str | None = None,
        cancel_callback: Callable[[], None] | None = None,
    ) -> str:
        if identifier is None:
            identifier = new_id(self._prefix)
        self._tasks[identifier] = (task, output_path, cancel_callback)
        return identifier

    def add_done_callback(self, identifier: str, callback) -> bool:
        entry = self._tasks.get(identifier)
        if entry is None:
            return False
        task, _output_path, _cancel_callback = entry
        task.add_done_callback(lambda _task: callback(identifier))
        return True

    def collect_completed(self, identifiers: Iterable[str] | None = None) -> list[tuple[str, str]]:
        allowed_identifiers = set(identifiers) if identifiers is not None else None
        completed = []
        for identifier, (task, output_path, _cancel_callback) in list(self._tasks.items()):
            if allowed_identifiers is not None and identifier not in allowed_identifiers:
                continue
            if task.done():
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    result = json.dumps({
                        "code": f"{self._prefix}_cancelled",
                        "task_identifier": identifier,
                        "output_file": str(output_path) if output_path else "",
                    })
                except Exception as exception:
                    result = str(exception)
                completed.append((identifier, result))
                del self._tasks[identifier]
        return completed

    def cancel(self, identifier: str) -> bool:
        entry = self._tasks.get(identifier)
        if entry is None:
            return False
        task, _output_path, cancel_callback = entry
        if cancel_callback is not None:
            cancel_callback()
        task.cancel()
        return True

    def cancel_many(self, identifiers: Iterable[str]) -> None:
        for identifier in list(identifiers):
            self.cancel(identifier)

    def cancel_all(self) -> None:
        for identifier in list(self._tasks):
            self.cancel(identifier)
            self._tasks.pop(identifier, None)

    @property
    def active_count(self) -> int:
        return sum(1 for task, _, _ in self._tasks.values() if not task.done())

    def active_count_for(self, identifiers: Iterable[str]) -> int:
        allowed_identifiers = set(identifiers)
        return sum(
            1
            for identifier, (task, _, _) in self._tasks.items()
            if identifier in allowed_identifiers and not task.done()
        )

    def list_active(self, identifiers: Iterable[str] | None = None) -> list[str]:
        allowed_identifiers = set(identifiers) if identifiers is not None else None
        return [
            identifier
            for identifier, (task, _, _) in self._tasks.items()
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


# Rich tool descriptions live alongside the code as markdown templates so they
# can be tuned without touching function bodies. They are loaded through the same
# PromptLoader the runtime uses for its prompts (guidance lives in files, not in
# code) and support {{ variable }} substitution if a description ever needs it.
from harness.core.configuration import PromptLoader as _PromptLoader

_DESCRIPTION_LOADER = _PromptLoader(Path(__file__).parent / "descriptions")


def _load_tool_description(name: str) -> str:
    return _DESCRIPTION_LOADER.load(name, {}).strip()


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
    output_path = Path("/tmp") / f"{new_id('bash')}.log"
    process_holder: dict[str, Any] = {}

    def cancel_process() -> None:
        process = process_holder.get("process")
        if process is None or process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            try:
                process.terminate()
            except ProcessLookupError:
                return

    async def run() -> str:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        process_holder["process"] = process
        process_id = process.pid

        async def write_stream(stream, handle):
            while True:
                line = await stream.readline()
                if not line:
                    break
                handle.write(line.decode())
                handle.flush()

        try:
            with output_path.open("w") as file_handle:
                await asyncio.gather(
                    write_stream(process.stdout, file_handle),
                    write_stream(process.stderr, file_handle),
                )

            await process.wait()
        except asyncio.CancelledError:
            cancel_process()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
            output = output_path.read_text(errors="replace") if output_path.exists() else ""
            payload = {
                "code": "bash_cancelled",
                "output": output[: 1 << 16],
                "output_file": str(output_path),
                "truncated": len(output) > 1 << 16,
                "pid": process_id,
                "size": len(output),
            }
            return json.dumps(payload)
        output = output_path.read_text()
        if not output:
            return json.dumps({"code": "bash_completed", "output": "", "output_file": str(output_path), "truncated": False, "pid": process_id, "size": 0})
        truncated = len(output) > 1 << 16
        return json.dumps({
            "code": "bash_completed",
            "output": output[: 1 << 16],
            "output_file": str(output_path),
            "truncated": truncated,
            "pid": process_id,
            "size": len(output),
        })

    task = asyncio.create_task(run())
    task_identifier = bash_tasks.register(task, output_path, cancel_callback=cancel_process)
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

    The search runs in the background. You do NOT fetch the results yourself:
    when the search finishes, its results are delivered to you automatically as a
    separate ``web_search_completed`` message carrying the same
    ``task_identifier``. This call only returns a ``web_search_started``
    acknowledgement — never call ``read_task`` on the returned identifier and
    never poll for it. Just keep working (you can start several searches at once);
    the results will appear on their own.

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

    # Mint the identifier up front so the eventual completed/error result can echo
    # it — the model correlates a delivered result to the search it started by
    # this id, instead of guessing whether its searches have finished.
    task_identifier = new_id("search")
    output_path = Path("/tmp") / f"{task_identifier}.log"

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
                "task_identifier": task_identifier,
                "query": query,
                "results": entries,
            })
            output_path.write_text(payload)
            return payload
        except Exception as exception:
            payload = json.dumps({
                "code": "web_search_error",
                "task_identifier": task_identifier,
                "message": str(exception),
            })
            output_path.write_text(payload)
            return payload

    task = asyncio.create_task(run())
    web_tasks.register(task, output_path, identifier=task_identifier)
    # The started acknowledgement intentionally omits any file path or other
    # fetch-looking handle: the only thing the model needs is the id to match the
    # auto-delivered result against. The "do not poll/read_task" guidance is
    # attached by the runtime from a prompt template (user-facing wording lives in
    # prompts, not in tool code).
    return json.dumps({
        "code": "web_search_started",
        "task_identifier": task_identifier,
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


def build_web_preview_result(
    source: str,
    *,
    is_file: bool,
    title: str = "Preview",
    height: int = 0,
    artifact_id: str = "",
    artifact_update_mode: str = "append",
    artifact_target_id: str = "",
    summary: str = "",
) -> dict[str, Any]:
    """Build the tool result for an ``open_preview`` call.

    ``source`` is either an ``http(s)`` URL (``is_file=False``) or an absolute local
    file path (``is_file=True``). The artifact only carries the reference — the front
    end points a sandboxed iframe at it (a file path is served by the backend
    ``/preview`` route) — so nothing heavy ever enters the model's context. Kept pure
    so it can be dispatched from the agent runtime.
    """
    identifier = (artifact_id or "").strip() or new_id("preview")
    mode = _widget_update_mode(artifact_update_mode)
    target_id = (artifact_target_id or "").strip() or identifier
    # Height defaults to automatic — a previewed local page reports its own content
    # height (via the injected runtime) and the front end sizes to it. A positive
    # value pins a fixed height instead.
    try:
        requested_height = int(height)
    except (TypeError, ValueError):
        requested_height = 0
    artifact_height = max(120, min(900, requested_height)) if requested_height > 0 else "auto"
    preview_summary = (summary or "").strip() or f'Opened a web preview of "{title}".'

    reference = {"file": source} if is_file else {"src": source}
    artifact = {
        "artifact_id": identifier,
        "artifact_target_id": target_id,
        "artifact_update_mode": mode,
        "type": "iframe",
        "title": title,
        **reference,
        "height": artifact_height,
        "summary": preview_summary,
        # A fresh token on every call — including an in-place refresh that reuses the
        # same artifact_id — so the front end can bust the iframe and reload the
        # (possibly rewritten) source instead of showing the previous render.
        "version": new_id("rev"),
    }
    model_context = {
        "code": "preview_opened",
        "summary": preview_summary,
        "source": source,
        "artifacts": [
            {
                "artifact_id": identifier,
                "artifact_target_id": target_id,
                "artifact_update_mode": mode,
                "type": "iframe",
                "title": title,
                "height": artifact_height,
            }
        ],
    }
    return {"artifacts": [artifact], "model_context": model_context}


@tool
def open_preview(
    url: str,
    title: str = "Preview",
    height: int = 0,
    artifact_id: str = "",
    artifact_update_mode: str = "append",
    artifact_target_id: str = "",
    summary: str = "",
) -> str:
    """Open a live preview in the chat — a mini-browser pointed at a URL or a
    local file — rendered in a sandboxed iframe outside the tool card.

    This is the general-purpose visual tool. Rather than passing markup inline, you
    point it at something that already exists: an ``http(s)`` URL, or a path to a
    file you have written (``url="/abs/path/chart.html"``, or a path relative to the
    working directory). To show a visualization, **write a complete HTML document to
    a file first** (with ``bash``: a heredoc, or an editor) and preview that file —
    then you can refine it by editing the file and re-previewing, which is far faster
    and cheaper than re-emitting a whole document each time. Reach for an existing web
    library inside the page rather than hand-rolling: a CDN ``<script>``/``<link>``
    (Plotly, D3, Mermaid, Leaflet, KaTeX, highlight.js, …) or just an ``<img>``.
    Think "which library already does this?" first.

    A previewed **local HTML file** gets the harness runtime injected automatically,
    so it sizes to its content (no need to pass ``height``), reports render errors
    back to you, and can be interactive. To make it interactive, post events back to
    the agent from inside the page — each becomes a structured ``widget_event`` turn:

        window.parent.postMessage(
            {source: "harness-widget", event: "<name>", data: {/* ... */}},
            "*"
        );

    Uncaught errors and rejected promises in a previewed local page are reported back
    to you automatically as a ``render_error`` event, so you can see what broke, edit
    the file, and re-preview. (External URLs render as-is — some sites refuse to load
    in a frame, and they cannot self-size or report errors.)

    Args:
        url: An ``http(s)`` URL, or a local file path (absolute, or relative to the
            working directory) — for example one you just wrote.
        title: Short caption shown above the preview. May contain markdown.
        height: Optional fixed height in pixels (120-900). Omit for automatic sizing
            (local HTML pages report their own height; the default).
        artifact_id: Stable id for this preview; generated when omitted. Reuse it
            with ``artifact_update_mode="replace"`` to refresh a preview in place.
        artifact_update_mode: ``append`` opens a new preview, ``replace``/``update``
            refresh an existing one, ``upsert`` refreshes if present else appends.
        artifact_target_id: Existing preview id to refresh; defaults to ``artifact_id``.
        summary: One-line description of what the preview shows.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def read_task(task_id: str = "", justification: str = "") -> str:
    """Read another A2A task in this context — a sibling or sub-agent task — by
    its id, returning its current status and artifact (deliverable).

    Use this to coordinate with agents working alongside you in the same context:
    check whether a sibling has finished and read what it produced, then build on
    it. Task ids are the ones returned when an *agent* is spawned.

    This is NOT how you retrieve background results. A web_search
    ("search-…") or background-bash ("bg-…") identifier is not a readable task —
    those results are delivered to you automatically when ready, so never call
    read_task on them and never use it to poll.

    Args:
        task_id: The id of a spawned sibling/sub-agent task to read.
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


@tool
def read_file(
    file_path: str,
    offset: int = 1,
    limit: int | None = 2000,
    justification: str = "",
) -> str:
    """Read a file, returning its lines in cat -n format.

    Args:
        file_path: Absolute path (or path relative to the working directory).
        offset: 1-indexed line number to start reading from.
        limit: Maximum number of lines to return (defaults to 2000).
        justification: A concise, user-facing reason for this read.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


read_file.description = _load_tool_description("read_file")


@tool
def find_files(pattern: str, justification: str = "") -> str:
    """Find files by glob pattern.

    Args:
        pattern: Glob pattern such as "**/*.py" or "src/**/*.ts".
        justification: A concise, user-facing reason for this search.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


find_files.description = _load_tool_description("find_files")


@tool
def search_content(
    pattern: str,
    include: str | None = None,
    path: str | None = None,
    justification: str = "",
) -> str:
    """Search file contents by regular expression.

    Args:
        pattern: Regular expression to search for.
        include: File-pattern filter such as "*.py" or "*.{ts,tsx}".
        path: Directory or file to search (defaults to the working directory).
        justification: A concise, user-facing reason for this search.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


search_content.description = _load_tool_description("search_content")


@tool
def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    justification: str = "",
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Replace an exact substring in one existing file.

    Args:
        file_path: Absolute path (or path relative to the working directory).
        old_string: The exact text to replace, copied verbatim from the file.
        new_string: The text to replace it with (must differ from old_string).
        replace_all: Replace every occurrence instead of requiring a unique match.
        justification: A concise, user-facing reason for this edit.
        risk: "low" for targeted edits, "medium" broad, "high" hard to reverse.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


edit_file.description = _load_tool_description("edit_file")


@tool
def write_file(
    file_path: str,
    content: str,
    justification: str = "",
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Write content to a file, overwriting it if it exists.

    Args:
        file_path: Absolute path (or path relative to the working directory).
        content: The full text to write to the file.
        justification: A concise, user-facing reason for this write.
        risk: "low" new file, "medium" broad rewrite, "high" hard to reconstruct.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


write_file.description = _load_tool_description("write_file")


@tool
async def fetch_url(
    url: str,
    format: Literal["markdown", "text", "html"] = "markdown",
    timeout: int = 30,
    justification: str = "",
) -> str:
    """Fetch content from a URL and convert it to the requested format.

    Args:
        url: Fully-formed https URL (http is upgraded to https automatically).
        format: "markdown" (default), "text", or "html".
        timeout: Request timeout in seconds.
        justification: A concise, user-facing reason for this fetch.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


fetch_url.description = _load_tool_description("fetch_url")


@tool
def ask_user(
    questions: list[dict],
    justification: str = "",
) -> str:
    """Ask the user one or more questions and receive their answers.

    Args:
        questions: List of question objects, each with "question" (full text),
            "header" (short label, max ~30 chars), "options" (list of
            {"label", "description"}), and optional "multiple" (bool) and
            "custom" (bool, default true).
        justification: A concise, user-facing reason for asking.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


ask_user.description = _load_tool_description("ask_user")


@tool
def load_skill(name: str, justification: str = "") -> str:
    """Load a specialized skill's instructions into the conversation.

    Args:
        name: The skill name, matching one listed in "Available skills".
        justification: A concise, user-facing reason for loading this skill.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


load_skill.description = _load_tool_description("load_skill")


# Give every pre-existing tool the same rich, hand-written description loaded from
# descriptions/<name>.md, overriding the docstring-derived default. Docstrings are
# still parsed for the per-parameter JSON schema; the description file carries the
# user-facing guidance — so every tool, old and new, is shaped the same way.
bash.description = _load_tool_description("bash")
web_search.description = _load_tool_description("web_search")
spawn_agent.description = _load_tool_description("spawn_agent")
read_task.description = _load_tool_description("read_task")
write_tasks.description = _load_tool_description("write_tasks")
update_tasks.description = _load_tool_description("update_tasks")
update_goal.description = _load_tool_description("update_goal")
open_preview.description = _load_tool_description("open_preview")
list_mcp_tools.description = _load_tool_description("list_mcp_tools")
call_mcp_tool.description = _load_tool_description("call_mcp_tool")
list_mcp_resources.description = _load_tool_description("list_mcp_resources")
read_mcp_resource.description = _load_tool_description("read_mcp_resource")


def cancel_all_background_tasks() -> None:
    bash_tasks.cancel_all()
    web_tasks.cancel_all()
    spawned_tasks.cancel_all()


def _cleanup_on_exit():
    cancel_all_background_tasks()


atexit.register(_cleanup_on_exit)

for termination_signal in (signal.SIGTERM, signal.SIGHUP):
    signal.signal(termination_signal, lambda signum, frame: (cancel_all_background_tasks(), sys.exit(1)))
