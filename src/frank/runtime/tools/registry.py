from __future__ import annotations

import asyncio
import os
import signal
import tempfile
from pathlib import Path
from typing import Any, Literal

from langchain.tools import tool
from pydantic import Field

from frank.base.identifiers import new_id
from frank.runtime.background import current_background_jobs, current_tool_call_id
from frank.base.tuning import Tunable, active_tuning, clip_to_tokens
from frank.base.serialization import compact
from frank.runtime.tools import context as tool_context

# bash is synchronous by default: the model chooses whether a command backgrounds
# (background=true), so backgrounding is never a surprise it has to reason about.
# A synchronous command blocks and returns its real output up to its ``timeout`` (a
# per-call window, defaulting to the value below); the timeout only trips for a command
# that runs unexpectedly long without being backgrounded, at which point it falls through
# to the background path as a safety net rather than holding the turn open forever.
# Ordinary git/network/package commands finish well within it and return real output — the
# old 2s auto-background window is exactly what surprised the model into re-running
# mutating commands (a `gh pr merge` that crossed the threshold looked unfinished and got
# issued twice). Genuinely long work is the model's cue to pass background=true or raise
# timeout. The default windows live centrally in Tunable (bash_sync_window_seconds,
# slow_tool_sync_window_seconds, web_search_sync_window_seconds) and are scaled by the
# tuning timeout multiplier at each call site.


def _require_mcp_client_manager():
    manager = tool_context.current().mcp_manager
    if manager is None:
        raise RuntimeError("MCP is not configured.")
    return manager


@tool
async def bash(
    command: str,
    location: str = "",
    read_only: bool = False,
    explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
    risk: Literal["low", "medium", "high"] = "low",
    background: bool = False,
    timeout: float = Tunable.bash_sync_window_seconds.default,
) -> str:
    """Execute a bash command and return its output.

    Synchronous by default: the command runs to completion and its real output is returned directly, so you always see the result of the action you took.

    Set background=True only for genuinely long-running work you do NOT need the result of before your turn can continue — a build, a test suite, a dev server, a broad scan. A backgrounded command returns immediately with a task identifier; its result is auto-injected into the conversation when it finishes, and the harness re-engages you then. Do NOT background a command whose output you need next (and never background then re-run the same command — it is already running).

    Always provide a clear explanation and risk assessment for the command. Set read_only=True only for commands that provably just read state (cat, head, tail, ls, grep, find, etc.). Omitted, the command is treated as potentially mutating.

    **Prefer specialized tools** for file discovery, content search, file reads, edits, writes, URL fetching, and downloads. Use bash for tests, builds, Git, process and package management, pipelines, and work without a dedicated tool.

    **Work efficiently:** batch independent read-only commands, do not repeat a search whose answer is already available, and never run a broad recursive search over a user's home directory. Use ``background=True`` for managed long-running work instead of starting unmanaged ``&`` or ``nohup`` jobs.

    Arguments:
      - command: The shell command to execute.
      - location: The project location to run the command on — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
      - read_only: Whether this command only reads state without modifying it. Defaults to False (treated as mutating) when omitted.
      - explanation: Explain why this command is needed for the task.
      - risk: One of "low", "medium", "high" — assess the potential damage. Low for read-only commands, medium for modifications, high for destructive operations.
      - background: Run the command in the background instead of waiting for it. Use for long-running work whose result is not needed immediately.
      - timeout: How many seconds to wait synchronously for the command before it auto-backgrounds (its result is then delivered when it finishes). Raise it for a command you want to wait longer for; it does not kill the command.
    """
    from frank.base import confinement as _confinement

    active = tool_context.current()
    profile, workspace = active.sandbox, active.workspace
    # The tool's own log has to land somewhere the profile permits, or bash fails on its
    # bookkeeping rather than on anything that was asked of it. A profile that permits no
    # writable directory at all leaves the workspace as the only candidate — and if that is
    # refused too, the command could not have written anything anyway.
    _scratch = _confinement.temporary_directory(profile, workspace=workspace)
    output_path = Path(_scratch or workspace or tempfile.gettempdir()) / f"{new_id('bash')}.log"
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
        spawn = _confinement.spawn_recipe(profile, workspace=workspace,
                                          extra_environment={"FRANK_SESSION_ID": os.environ.get("FRANK_SESSION_ID", "")})
        process = await asyncio.create_subprocess_exec(
            # The command still runs through a shell — the confinement prefix wraps that shell,
            # it does not replace it — but the working directory is now the process's own rather
            # than a `cd` the model could write past in the same string.
            *_confinement.resolve_command(command, spawn),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace or None,
            env=spawn.environment,
            preexec_fn=spawn.preexec,
            # A new process *group*, not a new process session. The group is what `killpg`
            # needs to reap the whole subtree, and it is all that was ever wanted here — but
            # `start_new_session` also detached the shell from the worker's process session,
            # which is how the daemon recognises a caller on its socket as this session, and
            # what `frank kill` sweeps. Staying in the session is what makes it attributable.
            process_group=0,
        )
        process_holder["process"] = process
        process_id = process.pid
        # `process_group=0` puts the shell in its own group with pgid == pid, so killpg reaps
        # the whole subtree. Persist the group id so a crash-orphaned subtree (survived a
        # SIGKILL of the harness) is reaped on the next startup. No-op UPDATE when the job is
        # not durably tracked (no context).
        try:
            current_background_jobs().store.record_process_group(job_id, os.getpgid(process_id))
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
                await asyncio.wait_for(process.wait(), timeout=active_tuning().duration(Tunable.sigterm_grace_seconds))
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
            inline_output, output_truncated = clip_to_tokens(output, active_tuning().amount(Tunable.output_tokens))
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
            return compact(payload)
        # Off the loop: a multi-megabyte command output must not stall the event loop
        # (and every other session on it) while this coroutine reads it back.
        output = await asyncio.to_thread(output_path.read_text)
        # A non-zero exit code is a failure the model must be able to see — without it,
        # `exit 7` was indistinguishable from success.
        return_code = process.returncode or 0
        result_code = "bash_completed" if return_code == 0 else "bash_failed"
        result_status = "ok" if return_code == 0 else "error"
        if not output:
            return compact({"code": result_code, "status": result_status, "output": "", "output_file": str(output_path), "truncated": False, "pid": process_id, "size": 0, "returncode": return_code})
        inline_output, truncated = clip_to_tokens(output, active_tuning().amount(Tunable.output_tokens))
        return compact({
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
    job_id = jobs.spawn(
        "bash", run(), output_path=output_path, cancel_callback=cancel_process,
        arguments={
            "command": command,
            "location": location,
            "read_only": read_only,
            "explanation": explanation,
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
        settled = await jobs.settle_inline(job_id, active_tuning().scale_timeout(timeout))
        if settled is not None:
            return settled.result
    return compact({
        "code": "bash_started",
        "status": "running",
        "job_id": job_id,
        "output_file": str(output_path),
    })


@tool
async def search_web(
    query: str,
    explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
    result_count: int = 5,
) -> str:
    """Search the web using Exa. Returns a ranked list of results with titles, URLs, and a summary of each — so you can often answer directly without fetching the page.

    Most searches finish quickly and return their ``web_search_completed`` results directly from this call. A slow search returns a ``web_search_started`` acknowledgement instead; its results are then delivered to you automatically as a separate ``web_search_completed`` message carrying the same ``job_id`` — never call ``read_turn`` on the identifier and never poll for it. Just keep working (you can start several searches at once); pending results appear on their own.

    Use this when you need current information from the internet, recent events, changing documentation, standards, prices, schedules, or external knowledge not available in the training data. Use ``fetch_url`` when the URL is already known instead of searching for it.

    Arguments:
      - query: The search query.
      - explanation: A concise, user-facing description of why this search is needed.
      - result_count: Number of results to return (1-10, default 5).
    """
    client = tool_context.current().exa_client
    if client is None:
        return compact({"code": "web_search_error", "status": "error", "message": "Web search is not configured."})

    # Mint the identifier up front so the eventual completed/error result can echo
    # it — the model correlates a delivered result to the search it started by
    # this id, instead of guessing whether its searches have finished.
    job_id = new_id("search")
    output_path = Path("/tmp") / f"{job_id}.log"

    async def run() -> str:
        try:
            results = await asyncio.to_thread(
                client.search,
                query,
                num_results=min(result_count, active_tuning().amount(Tunable.web_search_maximum)),
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
            payload = compact({
                "code": "web_search_completed",
                "status": "ok",
                "job_id": job_id,
                "query": query,
                "results": entries,
            })
            await asyncio.to_thread(output_path.write_text, payload)
            return payload
        except Exception as exception:
            payload = compact({
                "code": "web_search_error",
                "status": "error",
                "job_id": job_id,
                "message": str(exception),
            })
            await asyncio.to_thread(output_path.write_text, payload)
            return payload

    jobs = current_background_jobs()
    jobs.spawn(
        "search_web", run(), identifier=job_id, output_path=output_path,
        arguments={"query": query, "explanation": explanation, "result_count": result_count},
        # A search that outlives the turn keeps running detached — a Stop ends the
        # turn but leaves it running, so its result still lands and wakes the agent.
        detached=True,
    )
    # Give the search a short window to finish inline. The common case returns the
    # real results directly, so the model never juggles a pending handle at all.
    settled = await jobs.settle_inline(job_id, active_tuning().duration(Tunable.web_search_sync_window_seconds))
    if settled is not None:
        return settled.result
    # The started acknowledgement intentionally omits any file path or other
    # fetch-looking handle: the only thing the model needs is the id to match the
    # auto-delivered result against. The "do not poll/read_turn" guidance is
    # attached by the runtime from a prompt template (user-facing wording lives in
    # prompts, not in tool code).
    return compact({
        "code": "web_search_started",
        "status": "running",
        "job_id": job_id,
    })


@tool
async def list_mcp_tools(server: str = "", explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required.")) -> str:
    """List tools exposed by configured MCP servers.

    Use this to discover the exact tool name and input schema before calling ``call_mcp_tool``. Pass a server name to inspect one configured server or leave it empty to inspect every enabled server.

    Arguments:
      - server: Optional configured MCP server name. Leave empty to list every enabled server.
      - explanation: A concise, user-facing reason for inspecting MCP tools.
    """
    try:
        result = await _require_mcp_client_manager().list_tools(server)
        return compact(result)
    except Exception as exception:
        return compact({"code": "mcp_list_tools_error", "status": "error", "message": str(exception)})


@tool
async def call_mcp_tool(
    server: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    read_only: bool = False,
    explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Call a tool exposed by a configured MCP server.

    Discover the exact ``tool_name`` and ``arguments`` schema with ``list_mcp_tools`` first. Treat safety exactly like ``bash``: set ``read_only=True`` explicitly only for inspection-only calls; omitted means potentially mutating. For state-changing calls, set an appropriate medium or high risk.

    Arguments:
      - server: Configured MCP server name.
      - tool_name: Tool name as advertised by list_mcp_tools.
      - arguments: JSON object matching the MCP tool input schema.
      - read_only: Whether this MCP tool call only reads state. Defaults to False (treated as mutating) when omitted.
      - explanation: A concise, user-facing reason for the tool call.
      - risk: One of "low", "medium", "high" for non-read-only calls.
    """
    try:
        result = await _require_mcp_client_manager().call_tool(server, tool_name, arguments or {})
        return compact(result)
    except Exception as exception:
        return compact({"code": "mcp_call_tool_error", "status": "error", "message": str(exception)})


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
async def list_mcp_resources(server: str = "", explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required.")) -> str:
    """List resources exposed by configured MCP servers.

    Use this to discover resource URIs before calling ``read_mcp_resource``. Pass a server name to inspect one configured server or leave it empty to inspect every enabled server.

    Arguments:
      - server: Optional configured MCP server name. Leave empty to list every enabled server.
      - explanation: A concise, user-facing reason for inspecting resources.
    """
    try:
        result = await _require_mcp_client_manager().list_resources(server)
        return compact(result)
    except Exception as exception:
        return compact({"code": "mcp_list_resources_error", "status": "error", "message": str(exception)})


@tool
async def read_mcp_resource(server: str, uri: str, explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required.")) -> str:
    """Read a resource exposed by a configured MCP server.

    Discover the exact URI with ``list_mcp_resources`` first.

    Arguments:
      - server: Configured MCP server name.
      - uri: Resource URI as advertised by list_mcp_resources.
      - explanation: A concise, user-facing reason for reading the resource.
    """
    try:
        result = await _require_mcp_client_manager().read_resource(server, uri)
        return compact(result)
    except Exception as exception:
        return compact({"code": "mcp_read_resource_error", "status": "error", "message": str(exception)})




@tool
async def wait_for(
    seconds: float,
    explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
) -> str:
    """Pause for a fixed number of seconds, then continue — a cheap, intentional wait with no model round-trip while it runs.

    Use this to POLL instead of hammering: when you are waiting on something to become ready (a server to come up, a file to appear, a background job you started), do the check, and if it is not ready, wait_for a few seconds and check again — rather than re-issuing the same call back-to-back and expecting a different result. To tell whether a repeated action changed anything, re-read the prior call's ``output_file``.

    Prefer short waits and re-check over one long sleep; a Stop interrupts the wait immediately. Do NOT use wait_for to pass time when you have nothing to check — end your turn instead, and the harness re-engages you when background work completes.

    Arguments:
      - seconds: How long to wait before continuing. Prefer small values (a few seconds) and re-check.
      - explanation: A concise, user-facing reason for the wait.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")





@tool
def read_turn(turn_id: str = "", explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required.")) -> str:
    """Read a sibling turn in this session by its id, returning its current status and artifact (deliverable).

    Use this to coordinate with externally supplied sibling A2A task ids: check whether a sibling has finished and read what it produced, then build on it.

    This is NOT how you retrieve background results, and it is not how you read a peer session. A search_web ("search-…") or background-bash ("bg-…") handle is not a readable task — those results are delivered to you automatically when ready, so never call read_turn on one and never use it to poll. To look at a peer session, use read_session; a peer's answer arrives on its own as a message.

    Arguments:
      - turn_id: The id of an externally supplied sibling turn to read.
      - explanation: A concise, user-facing description of why you are reading this task — shown as the label for this call.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def set_tasks(tasks: list[dict]) -> str:
    """Create new tasks in the task list. Tasks can depend on each other.

    Use this to break down complex work into steps that can run in parallel or sequentially. Tasks with no dependencies can be worked on immediately. Tasks with dependencies must wait for their dependencies to complete first. Keep tasks short, factual, and tied to observable work. Skip the list for work the next response can plainly finish; once created, keep it reconciled with reality through ``update_tasks``.

    Arguments:
      - tasks: List of task objects. Each object has:
            - description (required): What needs to be done.
            - dependencies (optional): List of task identifiers this task depends on (e.g. ["task-...", ...]).
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def update_tasks(updates: list[dict]) -> str:
    """Update the status of one or more tasks at once.

    Mark a task ``in_progress`` when work starts, ``completed`` only when it is actually done, and ``blocked`` when reality prevents progress. Update on real state changes—not as busy-work—and never end with completed work still shown as unresolved.

    Arguments:
      - updates: List of update objects. Each object has:
            - task_id (required): The task identifier (e.g. "task-...").
            - status (required): One of 'pending', 'in_progress', 'completed', 'blocked'.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def update_goal(
    goal: str = "",
    status: Literal["active", "satisfied", "cleared"] = "active",
    explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
) -> str:
    """Set, replace, satisfy, or clear the single active goal for this turn.

    A goal is not a task list. It is the top-level completion contract the harness injects back into your context until you explicitly satisfy or clear it. Use it when a user request has a concrete outcome that must not be lost while you run tools, hand work to a peer session, or continue across multiple model passes. Do not set a goal for a tiny one-shot answer. While a goal is active, keep working until it is satisfied, explicitly clear it if it becomes obsolete, or leave it active only when work genuinely remains.

    Arguments:
      - goal: The goal text to set when status is "active". Leave empty when marking the current goal as "satisfied" or "cleared".
      - status: "active" sets/replaces the goal, "satisfied" removes it because the requested outcome is done, and "cleared" removes it because it is obsolete or no longer applicable.
      - explanation: A concise, user-facing reason for this update.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def read_file(
    file_path: str,
    location: str = "",
    offset: int = 1,
    limit: int | None = 2048,
    explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
) -> str:
    """Read a file, returning its lines in cat -n format. Image files (.png/.jpg/.jpeg/.gif/.webp) are ingested natively instead: the result is structured metadata, and on a vision model the image itself follows.

    Text lines carry 1-indexed line numbers for orientation. Exclude that prefix when copying exact text into ``edit_file``. Large files can be read in windows with ``offset`` and ``limit``; lines over the inline ceiling are reported as truncated and must not be copied into an exact-match edit. Reads record a content hash so later edits can reject stale state. Use ``search_code`` to find code by meaning, and ``bash`` with ripgrep/fd for exact names or content; do not use this on a directory. Batch independent file reads in one response.

    Arguments:
      - file_path: Absolute path (or path relative to the working directory).
      - location: The project location to read from — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
      - offset: 1-indexed line number to start reading from.
      - limit: Maximum number of lines to return (defaults to 2048).
      - explanation: A concise, user-facing reason for this read.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def search_code(
    query: str,
    top_k: int = 10,
    reindex: bool = False,
    explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
) -> str:
    """Search the codebase by meaning, in plain language.

    Ranks the project's code against a natural-language query (semantic similarity plus lexical overlap) and returns just the best-matching chunks with their file and line range — a fraction of the tokens of grepping and reading whole files — finding code by what it does, not its exact name. Use ``bash`` with ripgrep for an exact string or filename; use this to find code by meaning. This tool is read-only.

    Arguments:
      - query: What you are looking for, in plain language.
      - top_k: How many matching chunks to return (default 10).
      - reindex: Rebuild the code index first — pass this after you have edited files and need fresh results.
      - explanation: A concise, user-facing reason for this search.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def edit_file(
    file_path: str,
    find: str,
    replace_with: str,
    location: str = "",
    replace_all: bool = False,
    explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Replace exact text in a file, staged and validated before commit.

    ``find`` must occur verbatim in the file. Unless ``replace_all`` is set, it must be unique. Copy it character-for-character from ``read_file`` without its line-number prefix. A prior read supplies a content hash so stale edits are rejected if the file changes externally.

    The prospective result is syntax-checked before writing: Python uses its AST and supported languages use tree-sitter. On validation failure, the file on disk remains unchanged and the returned diagnostic describes the prospective broken state; correct the edit without rereading unchanged disk content.

    Arguments:
      - file_path: Absolute path (or path relative to the working directory).
      - find: The exact text to find, copied verbatim from the file.
      - replace_with: The text to replace it with.
      - location: The project location to edit in — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
      - replace_all: Replace every occurrence instead of requiring a unique match.
      - explanation: A concise, user-facing reason for this edit.
      - risk: "low" for targeted edits, "medium" broad, "high" hard to reverse.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def write_file(
    file_path: str,
    content: str,
    location: str = "",
    explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Write content to a file, overwriting it if it exists.

    Prefer ``edit_file`` for a targeted change to an existing file. Read an existing file first when its current content must be preserved; the recorded hash lets the harness reject a stale overwrite. Do not create documentation files proactively unless the user asked for them. This tool modifies files.

    Arguments:
      - file_path: Absolute path (or path relative to the working directory).
      - content: The full text to write to the file.
      - location: The project location to write to — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
      - explanation: A concise, user-facing reason for this write.
      - risk: "low" new file, "medium" broad rewrite, "high" hard to reconstruct.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
async def fetch_url(
    url: str,
    format: Literal["markdown", "text", "html"] = "markdown",
    timeout: float = Tunable.slow_tool_sync_window_seconds.default,
    hard_deadline: float = 30,
    background: bool = False,
    explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
) -> str:
    """Fetch content from a URL and convert it to the requested format.

    Use this for a specific URL already known; use ``search_web`` to discover one. It returns page text and handles JavaScript-rendered pages and common anti-bot walls through rendering fallbacks. Very large responses are truncated inline and include an ``output_file`` containing the full conversion. Use ``download_file`` for raw binary files. This tool is read-only.

    Sync-if-fast: it waits up to ``timeout`` seconds for the fetch inline and returns the content directly; a fetch still running past ``timeout`` moves to the background and its result is injected when it lands, so a slow page never blocks your turn. ``timeout`` is that inline-wait window (the same meaning as bash's ``timeout``) — raise it to wait longer, or set ``background=true`` to background immediately. ``hard_deadline`` is the separate network cutoff that actually aborts the request.

    Arguments:
      - url: A fully-formed http or https URL. It is fetched exactly as given — nothing rewrites the scheme, so pass https yourself when you mean https.
      - format: "markdown" (default), "text", or "html".
      - timeout: Inline-wait window in seconds before the fetch backgrounds (does not abort it).
      - hard_deadline: Network deadline in seconds that aborts the request itself.
      - background: Skip the inline wait and background the fetch immediately.
      - explanation: A concise, user-facing reason for this fetch.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
async def download_file(
    url: str,
    path: str,
    location: str = "",
    timeout: float = Tunable.slow_tool_sync_window_seconds.default,
    hard_deadline: float = 120,
    background: bool = False,
    explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
) -> str:
    """Download a file from a URL to a path, defeating typical bot/TLS blocks.

    Uses full browser TLS/HTTP2 fingerprint impersonation (and the configured proxy), so files that a plain download gets blocked from still come through. For reading a page's text, use fetch_url instead — this saves raw bytes (PDFs, archives, data). It cannot pass an interactive JavaScript challenge or CAPTCHA. This tool writes a file and is unavailable to read-only agents.

    Sync-if-fast: it waits up to ``timeout`` seconds for the download inline; one still running past ``timeout`` moves to the background and completes on its own (the destination path is held against concurrent edits until it finishes). ``timeout`` is that inline-wait window (the same meaning as bash's ``timeout``); ``hard_deadline`` is the separate network cutoff that aborts the transfer; ``background=true`` backgrounds immediately.

    Arguments:
      - url: Fully-formed http(s) URL of the file to download.
      - path: Destination path (relative to the working directory, or absolute).
      - location: The project location to save into — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
      - timeout: Inline-wait window in seconds before the download backgrounds (does not abort it).
      - hard_deadline: Network deadline in seconds that aborts the transfer itself.
      - background: Skip the inline wait and background the download immediately.
      - explanation: A concise, user-facing reason for this download.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
async def control_screen(
    script: str,
    target: str = "",
    explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Read and drive the live screen by composing a short Python script — one program that both finds elements and acts on them.

    The script runs against one **target** — a window or a browser tab you name — calling bare-named primitives (no prefix). Targets are listed for you; you never say which kind of thing it is, because the id already does. Only the primitives that target supports exist in the script — reaching for one it does not have is a NameError on that line, before anything else runs. Reading is in the script: ``find_many`` and ``find_one`` rank the surface by meaning and return elements, each a dict with a stable ``id``, its ``role``, its text, and its ``context``. Acting is trusted input: a click is a real click (actionability-checked, works through overlays, opens file pickers and native dropdowns), typing fires the events pages listen for. Because it is ordinary Python, a whole task — loop over rows, branch on what you find, call the page's own API in one line — is a single call, not a round trip per action.

    Finding elements. `find_many` returns the ranked matches, for reading or harvesting a whole set; `find_one` returns the single best match and raises when the top matches are indistinguishable, which is what makes it the one to use before acting. **`clickable` is optional and you should leave it out unless you are sure.** Pass True when you want something you can act on and text would only get in the way ("the Save button"), False when you want the words on screen and controls would only get in the way ("the filename shown in the row"), and omit it — the default — whenever you are unsure or want both. It asks about your own intent, not about how the platform spells its widget names: a tab, a link and a checkbox are all clickable=True. Ranking happens *inside* whatever you narrow to, so narrowing beats lengthening the query — similarity has no notion of what a thing *is*, and "the search button" competes against every element that merely mentions search. `name` matches exactly; `context` matches by containment, and exists only on a page — a window carries no context, so that facet admits nothing there and the search quietly widens to everything. Write explicit, descriptive queries: name the target and the section it sits under. On a page, find also searches the page's own traffic (network requests and WebSocket frames), so you can pull data from the source instead of walking the DOM.

    The primitives, and their exact signatures, arrive in your context each turn under `primitives`, keyed by what each place `can` do. Read them from there rather than from memory: they are generated from the code, so they cannot be out of date, and a place is only ever offered what it may actually run — under a read-only policy the acting primitives are simply absent. Reaching for a name a place does not have raises NameError **on the line that uses it**, which means everything above that line has already happened; check the vocabulary before committing to a plan rather than after.

    **You are writing Python, and it is a real program.** Not a macro, not a step list — a module body, executed with the standard library available and one object, `screen`, already bound to the target you named. Everything Python gives you is on the table: loops, conditionals, comprehensions, `try`/`except`, functions, data structures, computing over what a find returned. `screen.wait_for(query, seconds=...)` blocks until something matches, which is how you say "once the pane has loaded" instead of hoping; `screen.sleep(seconds)` is there for the rest. The failure mode to avoid is three timid lines and another round trip to learn what the fourth should be — nothing carries between calls but element ids, so each new one starts blind. Write the program the task actually needs.

    **A workflow can be a file.** `screen` is an instance of `frank.screen.Screen`, an ordinary importable class, so the same calls work in a module somebody saved as they do inline:

        from frank.screen import Screen

        def open_topic(screen: Screen, topic: str) -> list[str]:
            field = screen.wait_for("Search help text field", clickable=True)
            screen.type(field, topic, submit=True)
            return screen.read()

    That file type-checks, lints, and lives in version control like any other source. Your project's root is on the import path, so `from workflows.rstudio_help import open_topic` reaches it and `open_topic(screen, "DateTimeClasses")` runs it. When you work something out that is worth having again, write it there with `write_file` — the `ran` trace in your result is exactly what happened, so you are recording rather than reconstructing. A script that imports a workflow cannot be read statically, so its first run asks the user; that is the honest cost of running code the harness did not write.


    **There are two places to compose, and they are peers.** In the script, Python composes the primitives: loop over what a find returned, branch on it, wait for what an action reveals, compute the answer, report once. On a page, `evaluate` composes inside the document: one expression can filter a table to the rows that matter, aggregate a list into a number, read the page's own state, or call its signed-in API with `fetch` through the user's real session. Neither is the fallback for the other, and the strongest scripts use both — `evaluate` to work out *what* to act on, the element primitives to act on it. `evaluate` is classified state-changing, because nothing reading a script can tell a query from a mutation, so it is absent under a read-only policy where `find` and `read` are the way.

    `element` — the first argument of an acting primitive — is an id a find returned, the find result itself, or a plain-language query. It is never the `target`, which names the *place* the script runs in and is fixed for the whole script. How a query is resolved depends on the verb: `click`, `type`, `choose`, `upload` and `drag` resolve it the way `find_one` does, so an unclear one is caught rather than guessed, while `read`, `hover`, `scroll`, `caret`, `select` and `focus` take the top match without that check. For anything that changes state, prefer an id you already hold. `press("Enter")` and `submit=True` both post a form, so be deliberate.

    Targets — the place a script runs. Every window and tab has an id minted by the platform, and the current list arrives in your context each turn. Pass the one you mean as `target`; there is no default, because "whatever is in front" is a race with the person using the computer. An application is not a target: two Finder windows are two places, and naming the application cannot say which. A target you name that is not on the list comes back with the current list, so you can pick another without looking it up. Tabs appear in that list only once a browser session exists — until then a browser contributes its *windows*, which are addressable exactly as any other place, and listing them never opens a connection or puts a consent prompt in front of the user.

    Each entry carries what it takes to tell one place from another, so read it before choosing:
      - `app` and `title` — who owns it and what it calls itself. Neither is unique: two Finder windows are both "Applications"
      - `can` — the vocabulary this place answers to, keying into the `primitives` map beside the list
      - `document` — the file or page it holds, as a plain path or url. The strongest discriminator there is, and usually the honest way to name a window to the user ("the window showing report.pdf")
      - `main` — the application's main window, when it says which
      - `bounds` — where it is and how big, in screen points. What separates two windows that agree on everything else
      - `visible: false` — behind others, minimized, or on another desktop. You can act in it exactly as in any other; say so to the user when you do, because they cannot see it happen
      - `addressable: false` — an application publishing no windows to accessibility. It names the app, not a place, and cannot be acted in
      - `focused` — where the user's keyboard is right now. Somebody is working there

    What changed. The primitives that alter something — `click`, `type`, `choose`, `upload`, `drag`, `press`, `navigate`, `caret`, `select` — each answer with what they *changed*, not merely what they touched: the globals that moved (`title` and `focus` everywhere, `selection` on a window, `url` on a page) and what became newly present (`appeared`, with `appeared_total` when there was more than a sample's worth). One that replaced the whole document reports `navigated` — the new title, url and element count — instead of listing a page's worth of elements at you. One that changed nothing says so with `changed: []`, which is your signal that the click missed or the pane had not loaded, rather than that the element was named differently. The reading primitives report no such record, so their silence means nothing either way.

    Tabs and frames. The browser has more than one page and you choose which one you are on; `tabs`, `tab`, `new_tab` and `close_tab` are in the page vocabulary with their signatures. These are the user's own tabs, open because they are working in them: read and switch freely, but leave one you did not open unless the task is about it, because closing it can throw away a half-filled form and there is no undo. An iframe is its own document with its own origin and session; `frames` lists them. Element ids are already frame-scoped, so `f1e3` is the third element of frame `f1` and acting on it needs no extra step — but reading or scripting a frame does need it named, which is the only way to reach an embedded checkout, consent screen or viewer through the credentials it holds.

    The script runs like a notebook cell: the value of a trailing bare expression is reported as the result, and whatever you ``print`` is returned too. The result lists what each action *changed* (``changed``), so you can confirm the click landed rather than only that it was aimed. If the surface can't be read — Accessibility not granted, or the browser not connected — that comes back as an error to raise with the user, not something to route around.

    Arguments:
      - script: The Python to run.
      - target: The window or tab to run it in, by the id from the target list. Required.
      - explanation: Why this is needed.
      - risk: Damage potential — higher for actions that change state.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def ask_user(
    questions: list[dict],
    explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required."),
) -> str:
    """Ask the user one or more questions and receive their answers.

    Ask only when the answer genuinely changes the work. If there is a clear safe default, choose it, state the choice, and continue. When recommending an option, place it first and append ``(Recommended)`` to its label. Custom answers are enabled by default, so never add a redundant Other or catch-all option. An answer comes back as the selected label, a bare string — including free text the user typed instead of choosing. Only a question marked `multiple` answers with an array.

    Arguments:
      - questions: List of question objects, each with "question" (full text), "header" (short label, max ~30 chars), "options" (list of {"label", "description"}), and optional "multiple" (bool) and "custom" (bool, default true).
      - explanation: A concise, user-facing reason for asking.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def load_skill(name: str, explanation: str = Field(..., description="A concise, user-facing reason this action is needed for the current task. Always required.")) -> str:
    """Load a specialized skill's instructions into the conversation.

    When a task matches a skill listed in ``Available skills``, load that skill before acting rather than guessing its workflow. The result injects the full instructions and references to any scripts, files, or resources it provides.

    Arguments:
      - name: The skill name, matching one listed in "Available skills".
      - explanation: A concise, user-facing reason for loading this skill.
    """
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


# Background jobs are cancelled by whoever owns the process: the worker's entry point on
# shutdown, and `SessionExecutor.aclose` when a session ends. This module used to register an
# `atexit` hook and install process-wide SIGTERM and SIGHUP handlers that called `sys.exit(1)`,
# at import — which made importing the harness enough to seize a host program's signals, and
# killed a forked child the moment it was signalled. A library configures nothing it was not
# asked to configure.
