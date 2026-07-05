import asyncio
import atexit
import json
import os
import re
import signal
import sys
from pathlib import Path
from typing import Any, Literal

from exa_py import Exa
from langchain.tools import tool

from harness.identifiers import new_id
from harness.core.background import current_background_jobs, cancel_all_background_jobs
from harness.core.background_store import get_background_job_store

_exa_client: Exa | None = None
_mcp_client_manager: Any | None = None

# bash is synchronous by default: the model chooses whether a command backgrounds
# (background=true), so backgrounding is never a surprise it has to reason about.
# A synchronous command blocks and returns its real output up to this ceiling; the
# ceiling only trips for a command that runs unexpectedly long without being
# backgrounded, at which point it falls through to the background path as a safety
# net rather than holding the turn open forever. Ordinary git/network/package
# commands finish well within it and return real output — the old 2s auto-background
# window is exactly what surprised the model into re-running mutating commands (a
# `gh pr merge` that crossed the threshold looked unfinished and got issued twice).
# Genuinely long work is the model's cue to pass background=true.
_BASH_SYNC_CEILING_SECONDS = 60.0


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
    background: bool = False,
) -> str:
    """Execute a bash command and return its output.

    Synchronous by default: the command runs to completion and its real output is
    returned directly, so you always see the result of the action you took.

    Set background=True only for genuinely long-running work you do NOT need the
    result of before your turn can continue — a build, a test suite, a dev server,
    a broad scan. A backgrounded command returns immediately with a task
    identifier; its result is auto-injected into the conversation when it finishes,
    and the harness re-engages you then. Do NOT background a command whose output
    you need next (and never background then re-run the same command — it is
    already running).

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
        background: Run the command in the background instead of waiting for it.
              Use for long-running work whose result is not needed immediately.
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
        # start_new_session=True makes this shell a session/group leader, so its
        # pgid == pid and killpg reaps the whole subtree. Persist the group id so a
        # crash-orphaned subtree (survived a SIGKILL of the server) is reaped on the
        # next startup. No-op UPDATE when the job is not durably tracked (no context).
        try:
            get_background_job_store().record_process_group(task_identifier, os.getpgid(process_id))
        except (ProcessLookupError, OSError):
            pass

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

    jobs = current_background_jobs()
    task_identifier = jobs.spawn(
        "bash", run(), output_path=output_path, cancel_callback=cancel_process,
        spec={"command": command, "read_only": read_only, "risk": risk, "background": background},
        # A model-backgrounded command is detached: it outlives the turn and a Stop
        # leaves it running. A synchronous command is foreground — Stop kills it.
        detached=background,
    )
    if not background:
        # Block until the command finishes and hand its real output straight back,
        # so the model always sees the outcome of the action it took — never an
        # opaque "scheduled" placeholder it might mistake for unfinished and re-run.
        # The ceiling only trips for a command that runs unexpectedly long without
        # being backgrounded; it then falls through to the background path below as
        # a safety net rather than holding the turn open indefinitely.
        settled = await jobs.settle_inline(task_identifier, _BASH_SYNC_CEILING_SECONDS)
        if settled is not None:
            return settled.result
    return json.dumps({
        "code": "bash_started",
        "task_identifier": task_identifier,
        "output_file": str(output_path),
    })


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

    current_background_jobs().spawn(
        "web_search", run(), identifier=task_identifier, output_path=output_path,
        spec={"query": query, "result_count": result_count},
        # web_search always runs in the background — a Stop ends the turn but leaves
        # the search running, so its result still lands and wakes the agent.
        detached=True,
    )
    # The started acknowledgement intentionally omits any file path or other
    # fetch-looking handle: the only thing the model needs is the id to match the
    # auto-delivered result against. The "do not poll/read_task" guidance is
    # attached by the runtime from a prompt template (user-facing wording lives in
    # prompts, not in tool code).
    return json.dumps({
        "code": "web_search_started",
        "task_identifier": task_identifier,
    })


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


_WIDGET_UPDATE_MODES = {"append", "replace", "update", "upsert"}

# Runtime scripts injected into previewed HTML live as their own properly
# formatted .js files under assets/, read once at import and wrapped in a
# <script> tag for inline injection — so they can be edited and linted like code
# instead of minified strings. ASSETS_DIRECTORY is also read by server.py for the
# preview-proxy runtime.
ASSETS_DIRECTORY = Path(__file__).parent / "assets"
_WIDGET_RUNTIME = f"<script>\n{(ASSETS_DIRECTORY / 'widget_runtime.js').read_text(encoding='utf-8')}</script>"


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
def set_tasks(tasks: list[dict]) -> str:
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
    find: str,
    replace_with: str,
    replace_all: bool = False,
    skip_validation: bool = False,
    justification: str = "",
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Replace exact text in a file, staged and validated before commit.

    ``find`` must occur verbatim in the file. Unless ``replace_all`` is set,
    it must be unique. Copy it character-for-character from ``read_file``.

    Args:
        file_path: Absolute path (or path relative to the working directory).
        find: The exact text to find, copied verbatim from the file.
        replace_with: The text to replace it with.
        replace_all: Replace every occurrence instead of requiring a unique match.
        skip_validation: Skip AST/syntax validation before writing.
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
set_tasks.description = _load_tool_description("set_tasks")
update_tasks.description = _load_tool_description("update_tasks")
update_goal.description = _load_tool_description("update_goal")
open_preview.description = _load_tool_description("open_preview")
list_mcp_tools.description = _load_tool_description("list_mcp_tools")
call_mcp_tool.description = _load_tool_description("call_mcp_tool")
list_mcp_resources.description = _load_tool_description("list_mcp_resources")
read_mcp_resource.description = _load_tool_description("read_mcp_resource")


def cancel_all_background_tasks() -> None:
    cancel_all_background_jobs()


def _cleanup_on_exit():
    cancel_all_background_tasks()


atexit.register(_cleanup_on_exit)

for termination_signal in (signal.SIGTERM, signal.SIGHUP):
    signal.signal(termination_signal, lambda signum, frame: (cancel_all_background_tasks(), sys.exit(1)))
