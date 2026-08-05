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

from frank.base.configuration import PromptLoader
#: Why a tool call is happening, in the words the person watching will read. Every tool takes
#: one, because the transcript is the only place a call explains itself: a command and its
#: arguments say what ran, never why, and a call that cannot say why is a call somebody has to
#: reverse-engineer to trust. Written once here so the twenty-odd tools that ask for it cannot
#: drift into asking for twenty slightly different things.
EXPLANATION = "A concise, user-facing reason this action is needed for the current task. Always required."

#: What a call reaches for beyond the confinement it already has, and whether it changes anything.
#:
#: One argument where there were two. `read_only` used to be a separate flag answering "does this
#: mutate", weighed only where the static scan of the command could not decide — a narrow job,
#: carried by a wire of its own. `access_request` answers the same question and a larger one with
#: it: not merely whether the call writes, but *where* it reaches. That is the version worth
#: asking, because reach is structural and checkable where a bare boolean was neither.
#:
#: It is a difference against the profile, never an inventory of the call. A command working
#: inside what the session already holds omits it, which is nearly every command, so the ordinary
#: case costs no tokens and the argument's presence is itself the signal that something is being
#: asked for.
ACCESS_REQUEST = (
    "What this call needs beyond what the session already holds — a difference against the "
    "confinement listed in your context, not a list of everything the call touches. Omit it "
    "entirely when the call works inside paths already writable or readable, which is the usual "
    "case. When present it must set `mutates`. Use `writes`/`reads` for paths outside the "
    "confinement and `network` only where the confinement denies the network. A granted path "
    "stays granted for the rest of the session; ask for the narrowest thing that does the work, "
    "and never use a path granted for one purpose to do something else."
)

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


#: What a kernel refusal looks like coming back through a shell. Matched on the message rather
#: than on an errno, because the errno never reaches here: the shell prints its own sentence and
#: exits, so the text is the only evidence there is.
_SANDBOX_REFUSAL_PHRASES = (
    "operation not permitted",
    "permission denied",
    "read-only file system",
)


def _sandbox_refusal_note(return_code: int, output: str, profile, workspace: str) -> dict:
    """What to say when a command probably died on the confinement rather than on its own work.

    A boundary that refuses in errno is a boundary nobody can act on. `Operation not permitted`
    names no path, says nothing about what would have been permitted, and reads as a broken tool
    — so the usual response is to run the same command again.

    **This is a hint and not an attribution.** Seatbelt writes the denied path to the system log
    and Landlock returns a bare `EACCES`; neither can be tied back to one child reliably, and a
    note naming the wrong path would be worse than one naming none. So it states what is
    certainly true — where this session may write — and leaves the model to match that against
    what it was trying to do. Returned as structured data rather than pasted into the output,
    because the output is the command's and this is the harness's.
    """
    if return_code == 0 or not output:
        return {}
    lowered = output.lower()
    if not any(phrase in lowered for phrase in _SANDBOX_REFUSAL_PHRASES):
        return {}
    from frank.base import confinement as _confinement

    writable = [
        resolved for entry in profile.filesystem.writable
        if (resolved := _confinement.expand(entry, workspace=workspace))
    ]
    return {
        "note": (
            "This may be the sandbox rather than the command. A path outside the confinement is "
            "refused by the operating system, which reports it as a permission error without "
            "naming the path."
        ),
        "writable": writable,
        "network": profile.network,
        "remedy": (
            "Write somewhere already listed, or re-issue the call with an access_request naming "
            "the path you need."
        ),
    }


def _require_mcp_client_manager():
    manager = tool_context.current().mcp_manager
    if manager is None:
        raise RuntimeError("MCP is not configured.")
    return manager


@tool
async def bash(
    command: str,
    location: str = "",
    access_request: dict[str, Any] | None = Field(None, description=ACCESS_REQUEST),
    explanation: str = Field(..., description=EXPLANATION),
    risk: Literal["low", "medium", "high"] = "low",
    background: bool = False,
    timeout: float = Tunable.bash_sync_window_seconds.default,
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/bash.md."""
    from frank.base import confinement as _confinement

    active = tool_context.current()
    profile, workspace = active.sandbox, active.workspace
    # The tool's own log has to land somewhere the profile permits, or bash fails on its
    # bookkeeping rather than on anything that was asked of it.
    #
    # The workspace is not a fallback, and that is the whole of a bug worth naming. A profile
    # that permits nowhere writable — every `read_only` session has exactly that — resolved to
    # no scratch directory, and the workspace was next in line, so every command a read-only
    # reviewer ran dropped a `bash-<id>.log` into the tree it is forbidden to modify. The
    # sandbox did not stop it because the log is written by *this* process, not by the confined
    # child: forty-nine of them accumulated in a source tree during one review.
    #
    # So the last resort is the system temporary directory, which is scratch by definition and
    # is thrown away. If a profile denies even reading that back, the command still ran and the
    # output still reaches the model inline; only the file is lost, which is the right thing to
    # lose.
    _scratch = _confinement.temporary_directory(profile, workspace=workspace)
    output_path = Path(_scratch or tempfile.gettempdir()) / f"{new_id('bash')}.log"
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
        # The session's own tools ride in the same environment the confinement builds: its
        # profile at the front of `PATH`, and the package manager already pointed at that
        # profile — so `nix profile add nixpkgs#jq` needs no path, no flag and no variable, and
        # a missing tool has an ordinary ending instead of becoming a wall to route around.
        spawn = _confinement.spawn_recipe(
            profile, workspace=workspace, extra_environment=active.child_environment(),
        )
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
        result = {
            "code": result_code,
            "status": result_status,
            "output": inline_output,
            "output_file": str(output_path),
            "truncated": truncated,
            "pid": process_id,
            "size": len(output),
            "returncode": return_code,
        }
        refusal = _sandbox_refusal_note(return_code, inline_output, profile, workspace)
        if refusal:
            result["sandbox"] = refusal
        return compact(result)

    jobs = current_background_jobs()
    job_id = jobs.spawn(
        "bash", run(), output_path=output_path, cancel_callback=cancel_process,
        arguments={
            "command": command,
            "location": location,
            "access_request": access_request,
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
    explanation: str = Field(..., description=EXPLANATION),
    result_count: int = 5,
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/search_web.md."""
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
async def list_mcp_tools(server: str = "", explanation: str = Field(..., description=EXPLANATION)) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/list_mcp_tools.md."""
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
    access_request: dict[str, Any] | None = Field(None, description=ACCESS_REQUEST),
    explanation: str = Field(..., description=EXPLANATION),
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/call_mcp_tool.md."""
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
async def list_mcp_resources(server: str = "", explanation: str = Field(..., description=EXPLANATION)) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/list_mcp_resources.md."""
    try:
        result = await _require_mcp_client_manager().list_resources(server)
        return compact(result)
    except Exception as exception:
        return compact({"code": "mcp_list_resources_error", "status": "error", "message": str(exception)})


@tool
async def read_mcp_resource(server: str, uri: str, explanation: str = Field(..., description=EXPLANATION)) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/read_mcp_resource.md."""
    try:
        result = await _require_mcp_client_manager().read_resource(server, uri)
        return compact(result)
    except Exception as exception:
        return compact({"code": "mcp_read_resource_error", "status": "error", "message": str(exception)})


@tool
async def wait_for(
    seconds: float,
    explanation: str = Field(..., description=EXPLANATION),
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/wait_for.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def read_turn(turn_id: str = "", explanation: str = Field(..., description=EXPLANATION)) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/read_turn.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def set_tasks(tasks: list[dict], explanation: str = Field(..., description=EXPLANATION)) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/set_tasks.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def update_tasks(updates: list[dict], explanation: str = Field(..., description=EXPLANATION)) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/update_tasks.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def update_goal(
    status: Literal["active", "satisfied", "blocked", "cleared"] = "active",
    goal: str = "",
    requirements: list[str] | None = None,
    evidence: list[str] | None = None,
    blocker: str = "",
    explanation: str = Field(..., description=EXPLANATION),
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/update_goal.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def read_file(
    file_path: str,
    location: str = "",
    offset: int = 1,
    limit: int | None = 2048,
    explanation: str = Field(..., description=EXPLANATION),
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/read_file.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def search_code(
    query: str,
    top_k: int = 10,
    reindex: bool = False,
    explanation: str = Field(..., description=EXPLANATION),
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/search_code.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def edit_file(
    file_path: str,
    find: str,
    replace_with: str,
    location: str = "",
    replace_all: bool = False,
    explanation: str = Field(..., description=EXPLANATION),
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/edit_file.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def write_file(
    file_path: str,
    content: str,
    location: str = "",
    explanation: str = Field(..., description=EXPLANATION),
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/write_file.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
async def fetch_url(
    url: str,
    format: Literal["markdown", "text", "html"] = "markdown",
    timeout: float = Tunable.slow_tool_sync_window_seconds.default,
    hard_deadline: float = 30,
    background: bool = False,
    explanation: str = Field(..., description=EXPLANATION),
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/fetch_url.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
async def download_file(
    url: str,
    path: str,
    location: str = "",
    timeout: float = Tunable.slow_tool_sync_window_seconds.default,
    hard_deadline: float = 120,
    background: bool = False,
    explanation: str = Field(..., description=EXPLANATION),
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/download_file.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
async def control_screen(
    script: str,
    target: str = "",
    explanation: str = Field(..., description=EXPLANATION),
    risk: Literal["low", "medium", "high"] = "low",
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/control_screen.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def ask_user(
    questions: list[dict],
    explanation: str = Field(..., description=EXPLANATION),
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/ask_user.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def load_skill(name: str, explanation: str = Field(..., description=EXPLANATION)) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/load_skill.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


# Background jobs are cancelled by whoever owns the process: the worker's entry point on
# shutdown, and `SessionExecutor.aclose` when a session ends. This module used to register an
# `atexit` hook and install process-wide SIGTERM and SIGHUP handlers that called `sys.exit(1)`,
# at import — which made importing the harness enough to seize a host program's signals, and
# killed a forked child the moment it was signalled. A library configures nothing it was not
# asked to configure.


# What each tool tells the model it does, read from `descriptions/*.md` at import.
#
# It used to be the function's docstring, which put a document written *for the model* inside a
# construct meant for whoever reads the code — 11,507 characters of it in `control_screen`'s
# case, wrapped in quotes, re-indented by every formatter, and unreviewable beside the prose it
# belongs with. Every other thing this harness says to a model already lives in a `.md` under
# `prompts/` or `messages/`, loaded through `PromptLoader`; there was no reason for the tool
# descriptions to be the exception except that a decorator happened to read `__doc__`.
#
# The function keeps a one-line docstring saying where its description lives and who dispatches
# it, because that is what a reader of the code needs and it is not the same fact.
_DESCRIPTIONS = PromptLoader(Path(__file__).parent / "descriptions")

_DESCRIBED = (
    bash, search_web, list_mcp_tools, call_mcp_tool, list_mcp_resources, read_mcp_resource,
    wait_for, read_turn, set_tasks, update_tasks, update_goal, read_file, search_code,
    edit_file, write_file, fetch_url, download_file, control_screen, ask_user, load_skill,
)


def _apply_descriptions() -> None:
    """Give every tool the description written for it, and refuse to ship one that has none.

    Loudly, at import, rather than silently: `PromptLoader.load` answers "" for a file that is not
    there, and a tool whose description is the empty string is offered to the model as a name with
    no explanation — which it will still call, and then guess at. A missing file is a packaging
    mistake, and the moment to hear about it is the build rather than the first turn that reaches
    for that tool."""
    missing = []
    for entity in _DESCRIBED:
        text = _DESCRIPTIONS.load(entity.name, {}).strip()
        if not text:
            missing.append(entity.name)
            continue
        entity.description = text
    if missing:
        raise RuntimeError(
            "These tools have no description file in runtime/tools/descriptions: "
            + ", ".join(sorted(missing))
        )


_apply_descriptions()
