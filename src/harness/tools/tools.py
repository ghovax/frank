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
from pydantic import Field

from harness.identifiers import new_id
from harness.core.background import current_background_jobs, cancel_all_background_jobs, current_tool_call_id
from harness.core.background_store import get_background_job_store
from harness.core.tuning import Limit, active_tuning, clip_to_tokens

_exa_client: Exa | None = None
_mcp_client_manager: Any | None = None

# bash is synchronous by default: the model chooses whether a command backgrounds
# (background=true), so backgrounding is never a surprise it has to reason about.
# A synchronous command blocks and returns its real output up to a ceiling; the ceiling
# only trips for a command that runs unexpectedly long without being backgrounded, at
# which point it falls through to the background path as a safety net rather than holding
# the turn open forever. Ordinary git/network/package commands finish well within it and
# return real output — the old 2s auto-background window is exactly what surprised the
# model into re-running mutating commands (a `gh pr merge` that crossed the threshold
# looked unfinished and got issued twice). Genuinely long work is the model's cue to pass
# background=true. The ceiling (and web_search's shorter one) live in the central tuning
# policy, scaled by the timeout knob, read at the settle_inline call sites below.


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
    location: str = "",
    read_only: bool = False,
    justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
    risk: Literal["low", "medium", "high"] = "low",
    background: bool = False,
) -> str:
    """Execute a bash command and return its output.

    Synchronous by default: the command runs to completion and its real output is returned directly, so you always see the result of the action you took.

    Set background=True only for genuinely long-running work you do NOT need the result of before your turn can continue — a build, a test suite, a dev server, a broad scan. A backgrounded command returns immediately with a task identifier; its result is auto-injected into the conversation when it finishes, and the harness re-engages you then. Do NOT background a command whose output you need next (and never background then re-run the same command — it is already running).

    Always provide a clear justification and risk assessment for the command. Set read_only=True only for commands that provably just read state (cat, head, tail, ls, grep, find, etc.). Omitted, the command is treated as potentially mutating.

    **Prefer specialized tools** for file discovery, content search, file reads, edits, writes, URL fetching, and downloads. Use bash for tests, builds, Git, process and package management, pipelines, and work without a dedicated tool.

    **Work efficiently:** batch independent read-only commands, do not repeat a search whose answer is already available, and never run a broad recursive search over a user's home directory. Use ``background=True`` for managed long-running work instead of starting unmanaged ``&`` or ``nohup`` jobs.

    Arguments:
        command: The shell command to execute.
        location: The project location to run the command on — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
        read_only: Whether this command only reads state without modifying it. Defaults to False (treated as mutating) when omitted.
        justification: Explain why this command is needed for the task.
        risk: One of "low", "medium", "high" — assess the potential damage. Low for read-only commands, medium for modifications, high for destructive operations.
        background: Run the command in the background instead of waiting for it. Use for long-running work whose result is not needed immediately.
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
                await asyncio.wait_for(process.wait(), timeout=active_tuning().duration(Limit.SIGTERM_GRACE_SECONDS))
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
            # Read the captured output off the loop — a large log would otherwise block
            # the whole event loop while this background-job coroutine reads it.
            output = (
                await asyncio.to_thread(output_path.read_text, errors="replace")
                if output_path.exists()
                else ""
            )
            inline_output, output_truncated = clip_to_tokens(output, active_tuning().amount(Limit.OUTPUT_TOKENS))
            payload = {
                "code": "bash_cancelled",
                "status": "error",
                "output": inline_output,
                "output_file": str(output_path),
                "truncated": output_truncated,
                "pid": process_id,
                "size": len(output),
                "returncode": process.returncode,
            }
            return json.dumps(payload)
        # Off the loop: a multi-megabyte command output must not stall the event loop
        # (and every other session on it) while this coroutine reads it back.
        output = await asyncio.to_thread(output_path.read_text)
        # A non-zero exit code is a failure the model must be able to see — without it,
        # `exit 7` was indistinguishable from success.
        return_code = process.returncode or 0
        result_code = "bash_completed" if return_code == 0 else "bash_failed"
        result_status = "ok" if return_code == 0 else "error"
        if not output:
            return json.dumps({"code": result_code, "status": result_status, "output": "", "output_file": str(output_path), "truncated": False, "pid": process_id, "size": 0, "returncode": return_code})
        inline_output, truncated = clip_to_tokens(output, active_tuning().amount(Limit.OUTPUT_TOKENS))
        return json.dumps({
            "code": result_code,
            "status": result_status,
            "output": inline_output,
            "output_file": str(output_path),
            "truncated": truncated,
            "pid": process_id,
            "size": len(output),
            "returncode": return_code,
        })

    jobs = current_background_jobs()
    task_identifier = jobs.spawn(
        "bash", run(), output_path=output_path, cancel_callback=cancel_process,
        arguments={
            "command": command,
            "location": location,
            "read_only": read_only,
            "justification": justification,
            "risk": risk,
            "background": background,
        },
        # Correlate the job with its tool call from the start, so the user can
        # background a still-blocking foreground command by that tool-call id.
        tool_call_identifier=current_tool_call_id(),
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
        settled = await jobs.settle_inline(task_identifier, active_tuning().duration(Limit.BASH_SYNC_CEILING_SECONDS))
        if settled is not None:
            return settled.result
    return json.dumps({
        "code": "bash_started",
        "status": "running",
        "task_identifier": task_identifier,
        "output_file": str(output_path),
    })


@tool
async def web_search(
    query: str,
    justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
    result_count: int = 5,
) -> str:
    """Search the web using Exa. Returns a list of results with titles, URLs, and summaries.

    Most searches finish quickly and return their ``web_search_completed`` results directly from this call. A slow search returns a ``web_search_started`` acknowledgement instead; its results are then delivered to you automatically as a separate ``web_search_completed`` message carrying the same ``task_identifier`` — never call ``read_task`` on the identifier and never poll for it. Just keep working (you can start several searches at once); pending results appear on their own.

    Use this when you need current information from the internet, recent events, changing documentation, standards, prices, schedules, or external knowledge not available in the training data. Use ``fetch_url`` when the URL is already known instead of searching for it.

    Arguments:
        query: The search query.
        justification: A concise, user-facing description of why this search is needed.
        result_count: Number of results to return (1-10, default 5).
    """
    client = _exa_client
    if client is None:
        return json.dumps({"code": "web_search_error", "status": "error", "message": "Web search is not configured."})

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
                num_results=min(result_count, active_tuning().amount(Limit.WEB_SEARCH_MAXIMUM)),
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
                "status": "ok",
                "task_identifier": task_identifier,
                "query": query,
                "results": entries,
            })
            await asyncio.to_thread(output_path.write_text, payload)
            return payload
        except Exception as exception:
            payload = json.dumps({
                "code": "web_search_error",
                "status": "error",
                "task_identifier": task_identifier,
                "message": str(exception),
            })
            await asyncio.to_thread(output_path.write_text, payload)
            return payload

    jobs = current_background_jobs()
    jobs.spawn(
        "web_search", run(), identifier=task_identifier, output_path=output_path,
        arguments={"query": query, "justification": justification, "result_count": result_count},
        # A search that outlives the turn keeps running detached — a Stop ends the
        # turn but leaves it running, so its result still lands and wakes the agent.
        detached=True,
    )
    # Give the search a short window to finish inline. The common case returns the
    # real results directly, so the model never juggles a pending handle at all.
    settled = await jobs.settle_inline(task_identifier, active_tuning().duration(Limit.WEB_SEARCH_SYNC_CEILING_SECONDS))
    if settled is not None:
        return settled.result
    # The started acknowledgement intentionally omits any file path or other
    # fetch-looking handle: the only thing the model needs is the id to match the
    # auto-delivered result against. The "do not poll/read_task" guidance is
    # attached by the runtime from a prompt template (user-facing wording lives in
    # prompts, not in tool code).
    return json.dumps({
        "code": "web_search_started",
        "status": "running",
        "task_identifier": task_identifier,
    })


@tool
async def list_mcp_tools(server: str = "", justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required.")) -> str:
    """List tools exposed by configured MCP servers.

    Use this to discover the exact tool name and input schema before calling ``call_mcp_tool``. Pass a server name to inspect one configured server or leave it empty to inspect every enabled server.

    Arguments:
        server: Optional configured MCP server name. Leave empty to list every enabled server.
        justification: A concise, user-facing reason for inspecting MCP tools.
    """
    try:
        result = await _require_mcp_client_manager().list_tools(server)
        return json.dumps(result)
    except Exception as exception:
        return json.dumps({"code": "mcp_list_tools_error", "status": "error", "message": str(exception)})


@tool
async def call_mcp_tool(
    server: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    read_only: bool = False,
    justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Call a tool exposed by a configured MCP server.

    Discover the exact ``tool_name`` and ``arguments`` schema with ``list_mcp_tools`` first. Treat safety exactly like ``bash``: set ``read_only=True`` explicitly only for inspection-only calls; omitted means potentially mutating. For state-changing calls, set an appropriate medium or high risk. MCP tools may return renderable artifacts, including HTML, images, iframes, and links.

    Arguments:
        server: Configured MCP server name.
        tool_name: Tool name as advertised by list_mcp_tools.
        arguments: JSON object matching the MCP tool input schema.
        read_only: Whether this MCP tool call only reads state. Defaults to False (treated as mutating) when omitted.
        justification: A concise, user-facing reason for the tool call.
        risk: One of "low", "medium", "high" for non-read-only calls.
    """
    try:
        result = await _require_mcp_client_manager().call_tool(server, tool_name, arguments or {})
        return json.dumps(result)
    except Exception as exception:
        return json.dumps({"code": "mcp_call_tool_error", "status": "error", "message": str(exception)})


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
async def list_mcp_resources(server: str = "", justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required.")) -> str:
    """List resources exposed by configured MCP servers.

    Use this to discover resource URIs before calling ``read_mcp_resource``. Pass a server name to inspect one configured server or leave it empty to inspect every enabled server.

    Arguments:
        server: Optional configured MCP server name. Leave empty to list every enabled server.
        justification: A concise, user-facing reason for inspecting resources.
    """
    try:
        result = await _require_mcp_client_manager().list_resources(server)
        return json.dumps(result)
    except Exception as exception:
        return json.dumps({"code": "mcp_list_resources_error", "status": "error", "message": str(exception)})


@tool
async def read_mcp_resource(server: str, uri: str, justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required.")) -> str:
    """Read a resource exposed by a configured MCP server.

    Discover the exact URI with ``list_mcp_resources`` first.

    Arguments:
        server: Configured MCP server name.
        uri: Resource URI as advertised by list_mcp_resources.
        justification: A concise, user-facing reason for reading the resource.
    """
    try:
        result = await _require_mcp_client_manager().read_resource(server, uri)
        return json.dumps(result)
    except Exception as exception:
        return json.dumps({"code": "mcp_read_resource_error", "status": "error", "message": str(exception)})


@tool
def spawn_agent(prompt: str = "", agent: str = "", read_only: bool = False, justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required.")) -> str:
    """Delegate a task to another agent (a real A2A call to its endpoint).

    **Non-blocking:** the agent runs in the background as a related A2A task in the same context. This call returns an ``agent-...`` handle immediately, its activity streams live, and its structured deliverable is injected automatically when it finishes—even after the current turn ends. Do not wait, poll, or spawn the same work again. Use ``ask_agent`` for a mid-run question and ``cancel_agent`` when its work is no longer needed.

    Call this tool multiple times in one response to run independent agents in parallel. Give each one a self-contained prompt with its goal, relevant paths, constraints, and expected return shape. Choose a suitable profile from ``available_agents`` and set ``read_only=True`` for investigation that should report rather than modify. Do not delegate tiny edits or final judgment.

    Arguments:
        prompt: The task for the agent. State the goal clearly and, when it should build on or coordinate with other agents, name their task ids. Include the expected return shape: findings, evidence, uncertainty, and recommended next action and all else relevant.
        agent: Name of the agent profile to delegate to (e.g. 'reader', 'builder', 'scout').
        read_only: Force the agent into read-only mode — it may only run read-only commands and cannot modify the system or write files. Use for investigation/research agents that should report back rather than make changes.
        justification: A concise, user-facing description of what this agent will do — shown directly as the label for this call.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def call_remote_agent(prompt: str = "", agent: str = "", justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required.")) -> str:
    """Delegate a task to an **external** agent running on another server (an A2A peer).

    A remote agent is not a local agent: it runs its **own model on its own credentials** (its cost is separate and opaque to you), it has **no access to this machine's filesystem** (so never reference local paths — attach any file the task needs), it is **one-shot** (a fresh context each time, no shared history), and it **cannot be reached with ``ask_agent``** (no mid-run mailbox). Data in your prompt and attachments **leaves this machine**, so send only what the task needs.

    **Non-blocking**, like ``spawn_agent``: this returns a handle immediately, the remote agent's activity streams live, and its deliverable is injected when it finishes. Choose a peer from ``remote_agents`` in your context. Use ``spawn_agent`` for local agents instead.

    Arguments:
        prompt: The self-contained task for the remote agent — goal, all needed context (it shares none of yours), and the expected return shape.
        agent: Name of the remote agent from ``remote_agents``.
        justification: A concise, user-facing description of what this remote agent will do — shown as the label for this call.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def cancel_agent(task_identifier: str = "", justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required.")) -> str:
    """Cancel one spawned agent by the handle returned from ``spawn_agent``.

    Use this when the work is no longer needed, has been superseded, or must stop before completion. Cancellation targets only that agent; other agents and intentionally backgrounded shell or search jobs continue.

    Arguments:
        task_identifier: The ``agent-...`` handle returned by ``spawn_agent``.
        justification: A concise, user-facing description of why this agent is no longer needed.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def ask_agent(
    task_identifier: str,
    question: str,
    justification: str = Field(
        ...,
        description="A concise, user-facing reason for contacting this agent. Always required.",
    ),
) -> str:
    """**Ask an active agent a question** without interrupting its current model or tool operation.

    Pass an exact identifier from ``active_agents`` or the ``agent-...`` handle returned by ``spawn_agent``. The question is delivered at the recipient's next safe opening. Do not poll: one unanswered question stays active until the agent responds, fails, or is cancelled, and its response is delivered automatically.

    Arguments:
        task_identifier: An exact identifier from ``active_agents`` or ``spawn_agent``.
        question: A concrete progress request, finding request, decision, or handoff question.
        justification: A concise, user-facing reason for contacting the agent.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def respond_agent(
    message_identifier: str,
    response: str,
    justification: str = Field(
        ...,
        description="A concise, user-facing description of the response. Always required.",
    ),
) -> str:
    """**Respond to a question from another active agent.**

    Use the exact message identifier supplied in the received agent message. Give one direct, useful answer, then continue the existing task. Each question accepts one response; never reuse or invent a message identifier.

    Arguments:
        message_identifier: The exact message identifier from the received question.
        response: A useful answer for the requesting agent.
        justification: A concise, user-facing description of the response.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


_ARTIFACT_IMAGE_SUFFIXES = {".apng", ".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}


def artifact_kind_for(path: str) -> str:
    """The render kind of a local artifact by extension: ``image`` (versioned bytes),
    ``html`` (served live so it can self-size/interact), ``iframe`` (a PDF, which the browser
    renders in place), or a generic ``file`` — which has no visual preview. Only the first three
    are things worth opening in the panel; ``file`` means "show this in the conversation instead."""
    suffix = Path(path).suffix.lower()
    if suffix in _ARTIFACT_IMAGE_SUFFIXES:
        return "image"
    if suffix in (".html", ".htm", ".xhtml"):
        return "html"
    if suffix == ".pdf":
        return "iframe"
    return "file"


# Runtime fragments injected into rendered HTML live as self-contained assets and
# are read once at import. ASSETS_DIRECTORY is also read by server.py for the
# artifact-proxy runtime.
ASSETS_DIRECTORY = Path(__file__).parent / "assets"
_ARTIFACT_RUNTIME = (ASSETS_DIRECTORY / "artifact_runtime.html").read_text(encoding="utf-8")


def _inject_artifact_runtime(html: str) -> str:
    """Place the artifact runtime (error + resize reporting) as early as possible in
    the document so it catches failures in the model's own scripts and can size the
    artifact. Falls back to prepending when there is no recognizable
    ``<head>``/``<body>``/``<html>`` insertion point."""
    for pattern in (r"<head[^>]*>", r"<body[^>]*>", r"<html[^>]*>"):
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return html[: match.end()] + _ARTIFACT_RUNTIME + html[match.end() :]
    return _ARTIFACT_RUNTIME + html


def build_open_artifact_result(
    *,
    artifact_id: str,
    kind: str,
    title: str,
    source: str,
    location_uri: str = "",
    absolute_path: str = "",
    url: str = "",
    height: int = 0,
) -> dict[str, Any]:
    """Build the tool result for an ``open_artifact`` call. Capture (the version history)
    happens in the background; this result just opens a tab in the artifacts panel and
    tells it which artifact to hydrate. The stable ``artifact_id`` correlates the tab with
    its surface + version history; ``kind`` selects the renderer (``image``/``file`` load
    the versioned bytes, ``html`` serves the live local file, ``iframe`` renders an external
    ``url``). Kept pure so it can be dispatched from the agent runtime."""
    try:
        requested_height = int(height)
    except (TypeError, ValueError):
        requested_height = 0
    artifact_height = max(120, min(900, requested_height)) if requested_height > 0 else "auto"
    artifact = {
        "type": "artifact",
        "kind": kind,  # image | html | iframe | file
        "artifact_id": artifact_id,
        "title": title,
        "source": source,  # the path or URL shown to the user
        "location": location_uri,
        "absolute_path": absolute_path,  # live serving for a local html artifact
        "file": absolute_path,  # the file the renderer serves live via /artifact-page (image/html)
        "url": url,  # external iframe source
        "height": artifact_height,
    }
    model_context = {
        "code": "artifact_opened",
        # Echo the id so the model can update this same artifact tab on a later call by
        # passing the same ``artifact_id`` (each write becomes a new version underneath).
        "artifact_id": artifact_id,
        "artifacts": [{"artifact_id": artifact_id, "kind": kind, "title": title}],
    }
    return {"artifacts": [artifact], "model_context": model_context}


@tool
def open_artifact(
    url: str,
    height: int = 0,
    artifact_id: str = "",
) -> str:
    """Open an artifact in the chat's side panel — a sandboxed iframe (or image view) pointed at a URL or a local file — rendered outside the tool card. This is where "show it on the side" / "as an artifact" content goes.

    It is a **preview surface** for things that render — web pages, HTML, images, SVGs, PDFs — not a file viewer. A code or text file has no visual form; show it in the conversation instead of opening an empty panel. Previewing a page here is for viewing; to interact with a live site (sign in, click through) use the ``browser`` tool, which drives the user's real Chrome.

    Rather than passing markup inline, you point it at something that already exists: an ``http(s)`` URL, or a path to a file you have written (``url="/abs/path/chart.html"``, or a path relative to the working directory). To show a visualization, **write a complete HTML document to a file first** (with ``bash``: a heredoc, or an editor) and open that file — then you can refine it by editing the file and re-opening, which is far faster and cheaper than re-emitting a whole document each time. Reach for an existing web library inside the page rather than hand-rolling: a CDN ``<script>``/``<link>`` (Plotly, D3, Mermaid, Leaflet, KaTeX, highlight.js, …) or just an ``<img>``. Think "which library already does this?" first.

    A local **HTML file** gets the harness runtime injected automatically, so it sizes to its content (no need to pass ``height``), reports render errors back to you, and can be interactive. To make it interactive, post events back to the agent from inside the page — each becomes a structured ``artifact_event`` turn:

        window.parent.postMessage(
            {source: "artifact", event: "<name>", data: {/* ... */}},
            "*"
        );

    Uncaught errors and rejected promises in a local page are reported back to you automatically as a ``render_error`` event, so you can see what broke, edit the file, and re-open. (External URLs render as-is — some sites refuse to load in a frame, and they cannot self-size or report errors.)

    **Version history is automatic.** Every file you write is versioned in the background, so an artifact you open carries its full history — the user can step through prior versions, diff them, download any one, and restore. To refresh an artifact you already opened — a regenerated plot, an edited page — pass that same ``artifact_id`` back and the panel updates that one tab instead of opening a new one; the new render becomes a new version. Omitting ``artifact_id`` but writing to the same path is still recognized as the same artifact.

    The artifact is labelled automatically from what it points at — the file name for a local file, the URL for a web page — so there is nothing to title.

    Arguments:
        url: An ``http(s)`` URL, or a local file path (absolute, or relative to the working directory) — for example one you just wrote.
        height: Optional fixed height in pixels (120-900). Omit for automatic sizing (local HTML pages report their own height; the default).
        artifact_id: The id returned by a previous ``open_artifact`` call. Pass it to update that artifact tab in place (a new render becomes a new version); omit it to open a new one (a new path is also treated as new).
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")

@tool
def read_task(task_id: str = "", justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required.")) -> str:
    """Read another A2A task in this context — a sibling or agent task — by its id, returning its current status and artifact (deliverable).

    Use this to coordinate with externally supplied sibling A2A task ids: check whether a sibling has finished and read what it produced, then build on it.

    This is NOT how you retrieve background results. A web_search ("search-…"), background-bash ("bg-…"), or spawned-agent ("agent-…") handle is not a readable task. Those results are delivered to you automatically when ready, so never call read_task on them and never use it to poll. Use ``cancel_agent`` only when a spawned agent should be stopped.

    Arguments:
        task_id: The id of an externally supplied sibling A2A task to read.
        justification: A concise, user-facing description of why you are reading this task — shown as the label for this call.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def set_tasks(tasks: list[dict]) -> str:
    """Create new tasks in the task list. Tasks can depend on each other.

    Use this to break down complex work into steps that can run in parallel or sequentially. Tasks with no dependencies can be worked on immediately. Tasks with dependencies must wait for their dependencies to complete first. Keep tasks short, factual, and tied to observable work. Skip the list for work the next response can plainly finish; once created, keep it reconciled with reality through ``update_tasks``.

    Arguments:
        tasks: List of task objects. Each object has:
            - description (required): What needs to be done.
            - dependencies (optional): List of task identifiers this task depends on (e.g. ["task-...", ...]).
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def update_tasks(updates: list[dict]) -> str:
    """Update the status of one or more tasks at once.

    Mark a task ``in_progress`` when work starts, ``completed`` only when it is actually done, and ``blocked`` when reality prevents progress. Update on real state changes—not as busy-work—and never end with completed work still shown as unresolved.

    Arguments:
        updates: List of update objects. Each object has:
            - task_id (required): The task identifier (e.g. "task-...").
            - status (required): One of 'pending', 'in_progress', 'completed', 'blocked'.
            - result (optional): Summary of what was accomplished when marking as completed.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def update_goal(
    goal: str = "",
    status: Literal["active", "satisfied", "cleared"] = "active",
    justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
) -> str:
    """Set, replace, satisfy, or clear the single active goal for this turn.

    A goal is not a task list. It is the top-level completion contract the harness injects back into your context until you explicitly satisfy or clear it. Use it when a user request has a concrete outcome that must not be lost while you run tools, delegate, or continue across multiple model passes. Do not set a goal for a tiny one-shot answer. While a goal is active, keep working until it is satisfied, explicitly clear it if it becomes obsolete, or leave it active only when work genuinely remains.

    Arguments:
        goal: The goal text to set when status is "active". Leave empty when marking the current goal as "satisfied" or "cleared".
        status: "active" sets/replaces the goal, "satisfied" removes it because the requested outcome is done, and "cleared" removes it because it is obsolete or no longer applicable.
        justification: A concise, user-facing reason for this update.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def read_file(
    file_path: str,
    location: str = "",
    offset: int = 1,
    limit: int | None = 2048,
    justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
) -> str:
    """Read a file, returning its lines in cat -n format. Image files (.png/.jpg/.jpeg/.gif/.webp) are ingested natively instead: the result is structured metadata, and on a vision model the image itself follows.

    Text lines carry 1-indexed line numbers for orientation. Exclude that prefix when copying exact text into ``edit_file``. Large files can be read in windows with ``offset`` and ``limit``; lines over the inline ceiling are reported as truncated and must not be copied into an exact-match edit. Reads record a content hash so later edits can reject stale state. Use ``find_files`` for discovery and ``search_content`` for matching lines; do not use this on a directory. Batch independent file reads in one response.

    Arguments:
        file_path: Absolute path (or path relative to the working directory).
        location: The project location to read from — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
        offset: 1-indexed line number to start reading from.
        limit: Maximum number of lines to return (defaults to 2048).
        justification: A concise, user-facing reason for this read.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def find_files(pattern: str, location: str = "", include_ignored: bool = False, justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required.")) -> str:
    """Find files by glob pattern.

    Results are newest-first by modification time and honor ``.gitignore`` by default. Keep searches scoped to the project. Set ``include_ignored=True`` only when the required file is known to be ignored. For an open-ended hunt requiring several reasoning rounds, delegate it to an agent. This tool is read-only.

    Arguments:
        pattern: Path glob; ** spans directories, * and ? stay within a segment.
        location: The project location to search — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
        include_ignored: Leave False. Results normally exclude what the project's .gitignore excludes (build output, dependencies, caches). Set True only when the file you need is itself gitignored and you have a specific reason to reach it.
        justification: A concise, user-facing reason for this search.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def search_content(
    pattern: str,
    location: str = "",
    include: str | None = None,
    path: str | None = None,
    include_ignored: bool = False,
    justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
) -> str:
    """Search file contents by regular expression.

    Matches include file paths and line numbers and honor ``.gitignore`` by default. Use ``include`` for a filename glob and ``path`` for a subtree or single file. Results are capped, so use ``bash`` with ripgrep only when an exact count is required. Keep searches inside the project; delegate an open-ended multi-round investigation. This tool is read-only.

    Arguments:
        pattern: Regular expression to search for.
        location: The project location to search — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
        include: Restrict the search to filenames matching this glob.
        path: Directory or file to search (defaults to the working directory).
        include_ignored: Leave False. The search normally skips what the project's .gitignore excludes. Set True only when what you are looking for lives in a gitignored file and you have a specific reason to reach it.
        justification: A concise, user-facing reason for this search.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def edit_file(
    file_path: str,
    find: str,
    replace_with: str,
    location: str = "",
    replace_all: bool = False,
    justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Replace exact text in a file, staged and validated before commit.

    ``find`` must occur verbatim in the file. Unless ``replace_all`` is set, it must be unique. Copy it character-for-character from ``read_file`` without its line-number prefix. A prior read supplies a content hash so stale edits are rejected if the file changes externally.

    The prospective result is syntax-checked before writing: Python uses its AST and supported languages use tree-sitter. On validation failure, the file on disk remains unchanged and the returned diagnostic describes the prospective broken state; correct the edit without rereading unchanged disk content.

    Arguments:
        file_path: Absolute path (or path relative to the working directory).
        find: The exact text to find, copied verbatim from the file.
        replace_with: The text to replace it with.
        location: The project location to edit in — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
        replace_all: Replace every occurrence instead of requiring a unique match.
        justification: A concise, user-facing reason for this edit.
        risk: "low" for targeted edits, "medium" broad, "high" hard to reverse.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def write_file(
    file_path: str,
    content: str,
    location: str = "",
    justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Write content to a file, overwriting it if it exists.

    Prefer ``edit_file`` for a targeted change to an existing file. Read an existing file first when its current content must be preserved; the recorded hash lets the harness reject a stale overwrite. Do not create documentation files proactively unless the user asked for them. This tool modifies files.

    Arguments:
        file_path: Absolute path (or path relative to the working directory).
        content: The full text to write to the file.
        location: The project location to write to — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
        justification: A concise, user-facing reason for this write.
        risk: "low" new file, "medium" broad rewrite, "high" hard to reconstruct.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
async def fetch_url(
    url: str,
    format: Literal["markdown", "text", "html"] = "markdown",
    timeout: int = 30,
    justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
) -> str:
    """Fetch content from a URL and convert it to the requested format.

    Use this for a specific URL already known; use ``web_search`` to discover one. It returns page text and handles JavaScript-rendered pages and common anti-bot walls through rendering fallbacks. Very large responses are truncated inline and include an ``output_file`` containing the full conversion. Use ``download_file`` for raw binary files. This tool is read-only.

    Arguments:
        url: Fully-formed https URL (http is upgraded to https automatically).
        format: "markdown" (default), "text", or "html".
        timeout: Request timeout in seconds.
        justification: A concise, user-facing reason for this fetch.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
async def download_file(
    url: str,
    path: str,
    location: str = "",
    timeout: int = 120,
    justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
) -> str:
    """Download a file from a URL to a path, defeating typical bot/TLS blocks.

    Uses full browser TLS/HTTP2 fingerprint impersonation (and the configured proxy), so files that a plain download gets blocked from still come through. For reading a page's text, use fetch_url instead — this saves raw bytes (PDFs, archives, data). It cannot pass an interactive JavaScript challenge or CAPTCHA. This tool writes a file and is unavailable to read-only agents.

    Arguments:
        url: Fully-formed http(s) URL of the file to download.
        path: Destination path (relative to the working directory, or absolute).
        location: The project location to save into — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
        timeout: Request timeout in seconds.
        justification: A concise, user-facing reason for this download.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
async def computer(
    action: Literal["observe", "pointer", "press", "type", "select", "caret", "scroll", "screenshot"],
    app: str = "",
    element: str | None = None,
    query: str = "",
    window: str = "focused",
    gesture: str = "click",
    to_element: str | None = None,
    clicks: int = 1,
    button: str = "left",
    key: str = "",
    modifiers: list[str] | None = None,
    text: str = "",
    mode: str = "replace",
    to_text: str = "",
    select_all: bool = False,
    occurrence: int = 1,
    before: str = "",
    after: str = "",
    at_offset: int | None = None,
    edge: str = "",
    direction: str = "",
    justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Look at and act on the local macOS computer the way a person does.

    Use this for native applications without an API or command-line path; use ``browser`` for web pages. ``observe`` returns accessible elements with stable references, roles, names, and values. Act on those references with pointer and keyboard operations—never guess screen coordinates. Acting calls report what changed, so do not observe again merely to confirm an already reported effect.

    Combine operations as the task requires. If an action changes nothing, inspect the state and choose a different route instead of repeating it. A screenshot is a last resort for seeing inaccessible custom-drawn content, not for blind coordinate-based action. When an interface cannot be read safely, explain the limitation and ask the user to perform that step.

    Arguments:
        action: What to do — observe (look), pointer (click/hover/drag an element), press (a key or shortcut), type (enter text), select (select text by content), caret (place the cursor), scroll (reveal content), or screenshot (see an app that can't be read).
        app: Which app to look at or act in, by name or bundle id. Omit to reuse the last one.
        element: The ref of an element from a recent observe (pointer/type/select/caret target; scroll brings it into view; observe expands that region). Refs stay valid across calls.
        query: Text to search the app's UI for (observe) — matches element names and values.
        window: Which window to observe — "focused" (default), "all", "menu" (the menu bar), or a window's title.
        gesture: For pointer — "click" (default), "hover", or "drag".
        to_element: For a pointer drag — the element to drop onto.
        clicks: For pointer — 1 (default) or 2 for a double-click.
        button: For pointer — "left" (default) or "right" (opens the element's context menu).
        key: For press — a named key (return, tab, escape, arrows, f1…) or a single letter/digit for a shortcut.
        modifiers: Modifier keys held with `key`, e.g. ["command"] for Cmd+C, ["command","shift"].
        text: Text to enter (type) or the substring to select (select).
        mode: For type — "replace" (rewrite the field, default) or "insert" (at the caret).
        to_text: For select — the end of a range: select from `text` through `to_text`.
        select_all: For select — select the whole field.
        occurrence: Which occurrence to target when the text appears more than once (1-based).
        before: For caret — place it just before this text.
        after: For caret — place it just after this text.
        at_offset: For caret — place it at this character offset.
        edge: For caret — the field "start" or "end".
        direction: For scroll — up, down, left, or right (or give `element` to bring it into view).
        justification: Why this action is needed.
        risk: Damage potential — low for reads, higher for actions that change state.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
async def browser(
    action: Literal[
        "navigate", "observe", "pointer", "press", "type", "select", "caret", "scroll",
        "choose", "upload", "evaluate", "network", "tabs", "screenshot",
    ],
    url: str = "",
    element: str | None = None,
    query: str = "",
    offset: int | None = None,
    goal: str = "",
    expect: str = "",
    history: str = "",
    gesture: str = "click",
    to_element: str | None = None,
    clicks: int = 1,
    button: str = "left",
    dialog: str = "",
    key: str = "",
    text: str = "",
    mode: str = "replace",
    submit: bool = False,
    to_text: str = "",
    select_all: bool = False,
    occurrence: int = 1,
    before: str = "",
    after: str = "",
    at_offset: int | None = None,
    edge: str = "",
    direction: str = "down",
    option: str = "",
    paths: list[str] | None = None,
    expression: str = "",
    tab: str = "",
    browser_name: str = "chrome",
    justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Look at and act on the web in the user's own signed-in Chrome the way a person does.

    This connects to the user's real browser profile and live sessions. Use ``observe`` to read page structure, stable element references, names, values, and context, including iframes. Narrow large pages with ``query``, ``element``, or ``goal``. Acting calls reread the page and report state and URL changes, so avoid redundant observations and never repeat an action that changed nothing.

    Use ``evaluate`` or ``network`` when structured page data is faster and more accurate than stepping through rendered controls; both operate with the user's signed-in privileges and require appropriate care. Use ``screenshot`` only to see custom-drawn content, never to guess coordinates. Prefer a fresh tab when it avoids disturbing the user's current page. This tool acts on the live web; ``open_artifact`` is only a side-panel preview surface.

    Arguments:
        action: What to do — navigate (go to a URL, or back/forward/reload), observe (look at the page), pointer (click/hover/drag an element), press (a key or shortcut), type (enter text), select (select text by content), caret (place the cursor), scroll, choose (pick a dropdown option), upload (attach files), evaluate (run JavaScript), network (read requests), tabs (list/open/switch/close), or screenshot (see a page that can't be read).
        url: The address to open (navigate, or tabs with mode "open").
        element: The ref of an element from a recent observe (pointer/type/select/caret target; scroll moves its pane; observe expands its subtree, or returns its text with `offset`). Refs stay valid across calls.
        query: Text to search the page for (observe) — matches names, values, and context, iframes included; or a url substring to filter by (network).
        offset: With observe on an element, return its text starting here (continue a long read; the result names the next offset).
        goal: What you're trying to reach (observe, navigate) — ranks a large page's listing so the relevant controls survive the cap.
        expect: Text you expect the action to produce — the tool waits for it and reports `expected_found`.
        history: For navigate without a url — "back", "forward", or "reload".
        gesture: For pointer — "click" (default), "hover", or "drag".
        to_element: For a pointer drag — the element to drop onto.
        clicks: For pointer — 1 (default) or 2 for a double-click.
        button: For pointer — "left" (default) or "right".
        dialog: What to do with a JavaScript confirm/prompt a click triggers — "accept" or "dismiss".
        key: For press — a key or chord, e.g. Enter, Escape, ArrowDown, or Meta+C / Control+A for a shortcut.
        text: Text to enter (type) or the substring to select (select).
        mode: For type — "replace" (rewrite the field, default) or "insert" (at the caret). For tabs — "list" (default), "open", "switch", or "close".
        submit: With type, press Enter after entering the text.
        to_text: For select — the end of a range: select from `text` through `to_text`.
        select_all: For select — select the whole field.
        occurrence: Which occurrence to target when the text appears more than once (1-based).
        before: For caret — place it just before this text.
        after: For caret — place it just after this text.
        at_offset: For caret — place it at this character offset.
        edge: For caret — the field "start" or "end".
        direction: For scroll — down (default), up, left, right, top, or bottom.
        option: For choose — the visible label (or value) to pick in a dropdown.
        paths: For upload — local file path(s) to attach.
        expression: For evaluate — a JavaScript expression to run in the page; the JSON result is returned.
        tab: For tabs switch/close — the tab id from tabs (list).
        browser_name: Which browser to connect to — "chrome" (default), "edge", or "brave".
        justification: Why this action is needed.
        risk: Damage potential — low for reads/navigation, higher for actions that change state.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def ask_user(
    questions: list[dict],
    justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
) -> str:
    """Ask the user one or more questions and receive their answers.

    Ask only when the answer genuinely changes the work. If there is a clear safe default, choose it, state the choice, and continue. When recommending an option, place it first and append ``(Recommended)`` to its label. Custom answers are enabled by default, so never add a redundant Other or catch-all option. Answers are returned as arrays of selected labels.

    Arguments:
        questions: List of question objects, each with "question" (full text), "header" (short label, max ~30 chars), "options" (list of {"label", "description"}), and optional "multiple" (bool) and "custom" (bool, default true).
        justification: A concise, user-facing reason for asking.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def load_skill(name: str, justification: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required.")) -> str:
    """Load a specialized skill's instructions into the conversation.

    When a task matches a skill listed in ``Available skills``, load that skill before acting rather than guessing its workflow. The result injects the full instructions and references to any scripts, files, or resources it provides.

    Arguments:
        name: The skill name, matching one listed in "Available skills".
        justification: A concise, user-facing reason for loading this skill.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


def cancel_all_background_tasks() -> None:
    cancel_all_background_jobs()


def _cleanup_on_exit():
    cancel_all_background_tasks()


atexit.register(_cleanup_on_exit)

for termination_signal in (signal.SIGTERM, signal.SIGHUP):
    signal.signal(termination_signal, lambda signum, frame: (cancel_all_background_tasks(), sys.exit(1)))
