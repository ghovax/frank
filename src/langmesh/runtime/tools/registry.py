from __future__ import annotations

import asyncio
import os
import signal
import tempfile
from pathlib import Path
from typing import Any, Literal

from langchain.tools import tool
from pydantic import Field

from langmesh.base.identifiers import new_id
from langmesh.runtime.background import current_background_jobs, current_tool_call_id
from langmesh.base.tuning import Tunable, active_tuning, clip_to_tokens
from langmesh.base.serialization import compact
from langmesh.runtime.tools import context as tool_context

from langmesh.base.configuration import PromptLoader
#: Why a call is happening, in the words the person watching reads. Every tool takes one.
EXPLANATION = "A concise, user-facing reason this action is needed for the current task. Always required."

#: What a call reaches for beyond its confinement. A difference against the profile, not an inventory.
ACCESS_REQUEST = (
    "What this call needs beyond what the session already holds — a difference against the "
    "confinement listed in your context, not a list of everything the call touches. Omit it "
    "entirely when the call works inside paths already writable or readable, which is the usual "
    "case. When present it must set `mutates`. Use `writes`/`reads` for paths outside the "
    "confinement and `network` only where the confinement denies the network. A granted path "
    "stays granted for the rest of the session; ask for the narrowest thing that does the work, "
    "and never use a path granted for one purpose to do something else."
)

#: bash is synchronous by default: the model chooses whether a command backgrounds, so it is never a surprise.


#: What a kernel refusal looks like through a shell. Matched on the message, since the errno never arrives.
_SANDBOX_REFUSAL_PHRASES = (
    "operation not permitted",
    "permission denied",
    "read-only file system",
)


def _sandbox_refusal_note(return_code: int, output: str, profile, workspace: str) -> dict:
    """What to say when a command died on the confinement rather than its own work, since `EPERM` names no path."""
    if return_code == 0 or not output:
        return {}
    lowered = output.lower()
    if not any(phrase in lowered for phrase in _SANDBOX_REFUSAL_PHRASES):
        return {}
    from langmesh.base import confinement as _confinement

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
    background: bool = False,
    timeout: float = Tunable.bash_sync_window_seconds.default,
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/bash.md."""
    from langmesh.base import confinement as _confinement

    active = tool_context.current()
    profile, workspace = active.sandbox, active.workspace
    # The tool's log lands where the profile permits, and never in the tree the session is working in.
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
        # The session's own tools ride in the environment the confinement builds, already on `PATH`.
        spawn = _confinement.spawn_recipe(
            _confinement.first_attempt(profile, workspace=workspace),
            workspace=workspace, extra_environment=active.child_environment(),
        )
        process = await asyncio.create_subprocess_exec(
            # Still a shell command; the working directory is the process's own, not a `cd` the model can escape.
            *_confinement.resolve_command(command, spawn),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace or None,
            env=spawn.environment,
            preexec_fn=spawn.preexec,
            # A process group, not a session: `killpg` needs the group, and the session is what makes it attributable.
            process_group=0,
        )
        process_holder["process"] = process
        process_id = process.pid
        # Persist the group id, so a subtree orphaned by a crash is reaped on the next startup.
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
            # Read off the loop: a large log would otherwise block every session sharing it.
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
        # Off the loop, for the same reason: a multi-megabyte output must not stall it.
        output = await asyncio.to_thread(output_path.read_text)
        # A non-zero exit is a failure the model must see, or `exit 7` reads as success.
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
            "background": background,
        },
        # Correlate the job with its tool call, so a blocking foreground command can be backgrounded by that id.
        tool_call_identifier=current_tool_call_id(),
        # A backgrounded command is detached and survives a Stop; a synchronous one is killed by it.
        detached=background,
    )
    if not background:
        # Block and hand back real output, so the model never mistakes a placeholder for unfinished work.
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

    # Mint the id up front, so a delivered result can be matched to the search that started it.
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
        # A search outliving the turn keeps running, so its result still lands and wakes the agent.
        detached=True,
    )
    # A short inline window, so the common case returns results rather than a pending handle.
    settled = await jobs.settle_inline(job_id, active_tuning().duration(Tunable.web_search_sync_window_seconds))
    if settled is not None:
        return settled.result
    # No path or fetch-looking handle in the acknowledgement: the id is the only thing the model needs.
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
) -> str:
    """Dispatched by AgentRuntime._execute_tool; described in descriptions/edit_file.md."""
    raise NotImplementedError("Dispatched by AgentRuntime._execute_tool.")


@tool
def write_file(
    file_path: str,
    content: str,
    location: str = "",
    explanation: str = Field(..., description=EXPLANATION),
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


# Background jobs are cancelled by whoever owns the process, since a library configures nothing unasked.


# What each tool tells the model, read from `descriptions/*.md` at import rather than from a docstring.
_DESCRIPTIONS = PromptLoader(Path(__file__).parent / "descriptions")

_DESCRIBED = (
    bash, search_web, list_mcp_tools, call_mcp_tool, list_mcp_resources, read_mcp_resource,
    wait_for, read_turn, set_tasks, update_tasks, update_goal, read_file, search_code,
    edit_file, write_file, fetch_url, download_file, control_screen, ask_user, load_skill,
)


def _apply_descriptions() -> None:
    """Give every tool its description, and fail at import rather than ship one offered as an empty string."""
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
