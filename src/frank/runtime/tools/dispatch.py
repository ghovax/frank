"""The AgentRuntime tool-execution concern (a mixin composed into AgentRuntime).

The ``_tool_*`` handlers, the ``_execute_tool`` permission/location/dispatch pipeline, concurrent
batch draining, argument validation, and the backgroundable-tool runner. Imports its dependencies
from the leaf ``agent_internals`` module and stable modules, so the graph stays a clean DAG."""
from __future__ import annotations

import logging

from dataclasses import replace
from datetime import datetime, timezone
from frank.base import telemetry as _telemetry
from frank.runtime.internals import (
    _background_handle_kind,
    _cap_model_result_payload,
    _coerce_mcp_arguments,
    _coerce_structured_arguments,
    _maybe_json,
    _model_result_status,
    _model_visible_tool_result,
    _ResolvedToolDecision,
    _ToolCall,
    _tool_timing_metadata,
    _utc_timestamp,
)
from frank.runtime.tools import context as tool_context, file_operations as file_tools
from frank.runtime.background import (
    bind_background_jobs,
    bind_tool_call_id,
    unbind_background_jobs,
    unbind_tool_call_id,
)
from frank.protocol.events import ToolStatus
from frank.base.file_leases import FileLeaseConflict
from frank.base.skills import enabled_skills, load_skills
from frank.runtime.locations import (
    _LOCATION_TOOLS,
    CallExecutionPolicy,
    ResolvedLocation,
    ToolLocationError,
)
from frank.base.tuning import active_tuning, current_context_window, Tunable
from frank.runtime.turn_events import DeniedInjection, Error, Mcp, ToolCall, ToolResult, TurnEvent
from frank.runtime.tools.registry import (
    bash as bash_tool,
    call_mcp_tool_with_events,
    list_mcp_resources as list_mcp_resources_tool,
    list_mcp_tools as list_mcp_tools_tool,
    read_mcp_resource as read_mcp_resource_tool,
    search_web as search_web_tool,
)
from langchain_core.messages import ToolMessage
from pathlib import Path
from pydantic import ValidationError
from typing import Any, AsyncIterator, cast
import asyncio
import shlex
import time
from frank.base.configuration import PermissionDenied
from frank.base.serialization import compact

logger = logging.getLogger(__name__)




class _ToolsMixin:

    async def _run_one_tool(
        self,
        tool_call_data: dict,
        turn_tool_calls_log: list[dict],
        turn_tool_results_log: list[dict],
        outcomes: dict[str, dict],
        decision: _ResolvedToolDecision,
    ) -> AsyncIterator[TurnEvent]:
        """Run a single tool call, yielding its events and recording its outcome
        in ``outcomes`` (keyed by tool_call_id). The caller appends ToolMessages
        afterward so the conversation stays consistent even on abort.

        Self-contained so it can run concurrently with other tools: each owns its
        TOOL_CALL emit, result collection, and outcome record. ``decision`` is the
        preflight verdict — approve, deny, or (ask_user) the answers — so this path
        never prompts; the human decision was resolved before the batch started.
        """
        tool_name = tool_call_data["name"]
        tool_arguments = tool_call_data["args"]
        tool_call_identifier = tool_call_data["id"]
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()

        yield ToolCall(name=tool_name,
            arguments=tool_arguments,
            id=tool_call_identifier,
        )
        turn_tool_calls_log.append({
            "name": tool_name,
            "arguments": tool_arguments,
            "tool_call_id": tool_call_identifier,
            "started_at": _utc_timestamp(started_at),
        })

        result_content: str = ""
        background_job_id: str | None = None
        denied_commands: list[str] = []
        image_followups: list[dict[str, str]] = []
        tool_failed = False

        tool_span = _telemetry.start_span("tool.execute", {"tool.name": tool_name})
        try:
            async for event in self._execute_tool(tool_name, tool_arguments, tool_call_identifier, decision):
                # An image read carries its pixels on a model-facing side channel;
                # strip it here so the base64 never reaches the UI or the event
                # log — the turn loop attaches it to the conversation instead.
                if isinstance(event, ToolResult) and "model_image" in event.extra:
                    result_payload = event.result
                    image_followups.append({
                        "path": str((result_payload or {}).get("path", "")) if isinstance(result_payload, dict) else "",
                        "data_uri": str(event.extra["model_image"]),
                    })
                    # Strip the model-facing image off the event before it goes downstream, so the
                    # base64 never reaches the UI or the event log.
                    event = replace(event, extra={key: value for key, value in event.extra.items() if key != "model_image"})
                yield event
                if isinstance(event, ToolResult):
                    result_str = event.result
                    if (
                        isinstance(result_str, dict)
                        and event.status == ToolStatus.RUNNING.value
                    ):
                        raw_job_id = result_str.get("job_id")
                        background_job_id = (
                            raw_job_id if isinstance(raw_job_id, str) else None
                        )
                        result_content = compact({
                            "code": "background_job_scheduled",
                            "job_id": background_job_id,
                        })
                        turn_tool_results_log.append({"name": tool_name, "result": compact(result_str)})
                    else:
                        if isinstance(result_str, dict):
                            # A structured result that reports an error status marks the
                            # call failed for the model and the UI alike.
                            if event.status == ToolStatus.ERROR.value:
                                tool_failed = True
                            # Minified for the model — no spaces, and non-ASCII kept verbatim
                            # (window titles, emoji) rather than \uXXXX-escaped. This is the one
                            # place every structured tool result (e.g. the computer tool's element
                            # list) is serialized for the model; the UI receives the dict on a
                            # separate path and prettifies it there, so this never affects display.
                            result_str = compact(result_str)
                        result_content = _cap_model_result_payload(str(result_str))
                        turn_tool_results_log.append({"name": tool_name, "result": result_content})
                elif isinstance(event, Error):
                    tool_failed = True
                    result_content = event.message
                    turn_tool_results_log.append({"name": tool_name, "result": result_content})
                elif isinstance(event, DeniedInjection):
                    denied_commands.append(event.command)
        except asyncio.CancelledError:
            result_content = "Tool call aborted."
            yield Error(id=tool_call_identifier, message=result_content, tool=tool_name,
            )
            turn_tool_results_log.append({"name": tool_name, "result": result_content})
        except Exception as exception:
            result_content = f"{exception}"
            yield Error(id=tool_call_identifier, message=result_content, tool=tool_name,
            )
            turn_tool_results_log.append({"name": tool_name, "result": result_content})
        finally:
            _telemetry.end_span(tool_span)

        completed_at = datetime.now(timezone.utc)
        duration_milliseconds = int((time.monotonic() - started_monotonic) * 1000)
        timing_metadata = _tool_timing_metadata(
            tool_name=tool_name,
            tool_call_identifier=tool_call_identifier,
            started_at=started_at,
            completed_at=completed_at,
            duration_milliseconds=duration_milliseconds,
            background_job_id=background_job_id,
        )

        outcomes[tool_call_identifier] = {
            "content": result_content,
            "ok": not tool_failed,
            "background_job_id": background_job_id,
            "denied_commands": denied_commands,
            "image_followups": image_followups,
            "metadata": timing_metadata,
        }

    async def _drain_tools_concurrently(
        self,
        tool_calls: list[dict],
        turn_tool_calls_log: list[dict],
        turn_tool_results_log: list[dict],
        outcomes: dict[str, dict],
        decisions: dict[str, _ResolvedToolDecision],
    ) -> AsyncIterator[TurnEvent]:
        """Run independent tool calls concurrently, yielding their events as they
        arrive (interleaved). The model emits several tool calls in one response
        when work is parallel; they run concurrently so multiple peer sessions
        and other independent tools progress together. ``decisions`` carries the
        preflight verdict for each call, so no tool prompts mid-batch."""
        if not tool_calls:
            return

        queue: asyncio.Queue[TurnEvent | None] = asyncio.Queue()
        remaining = len(tool_calls)

        async def runner(tool_call_data: dict) -> None:
            nonlocal remaining
            tool_call_identifier = tool_call_data["id"]
            current_task = asyncio.current_task()
            if current_task is not None:
                self._active_tool_tasks[tool_call_identifier] = current_task
            try:
                decision = decisions.get(tool_call_identifier) or _ResolvedToolDecision(tool_call_id=tool_call_identifier)
                async for event in self._run_one_tool(
                    tool_call_data, turn_tool_calls_log, turn_tool_results_log, outcomes, decision,
                ):
                    await queue.put(event)
            except Exception:
                # _run_one_tool handles its own errors; this guards the merge.
                pass
            finally:
                self._active_tool_tasks.pop(tool_call_identifier, None)
                remaining -= 1
                if remaining == 0:
                    await queue.put(None)

        tasks = [asyncio.create_task(runner(call)) for call in tool_calls]
        abort_waiter = asyncio.ensure_future(self._abort_event.wait())
        try:
            while True:
                if self._abort_event.is_set():
                    break
                # Race the next tool event against the abort so a Stop is honored even
                # when every tool is parked (a slow network/MCP call, a thread that
                # can't be cancelled) and nothing is arriving on the queue. The
                # `finally` cancels the runners; the turn loop then records "(interrupted)"
                # for any tool that never produced a result.
                get_future = asyncio.ensure_future(queue.get())
                await asyncio.wait(
                    {get_future, abort_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                if not get_future.done():
                    get_future.cancel()
                    break
                event = get_future.result()
                if event is None:
                    break
                yield event
        finally:
            abort_waiter.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _validate_tool_call(
        self,
        tool_name: str,
        arguments: dict,
    ) -> tuple[str, str] | None:
        tool_schemas = self._tool_schemas
        if not isinstance(arguments, dict):
            return ("invalid_tool_arguments", f"{tool_name} arguments must be an object.")
        if tool_name not in tool_schemas:
            accepted = ", ".join(sorted(tool_schemas))
            return ("unknown_tool", f"Unknown or unavailable tool '{tool_name}'. Available tools are: {accepted}.")
        schema = tool_schemas.get(tool_name)
        if schema is not None:
            fields = set(getattr(schema, "model_fields", {}).keys())
            unknown_arguments = sorted(set(arguments) - fields)
            if unknown_arguments:
                accepted = ", ".join(sorted(fields))
                rejected = ", ".join(unknown_arguments)
                return ("invalid_tool_arguments", f"The tool {tool_name} does not accept the following argument(s) with which it was invoked: {rejected}. Accepted arguments by it are only: {accepted}.")
            # `call_mcp_tool.arguments` is frequently emitted by models as a JSON *string*
            # rather than an object. Coerce it to a dict for validation (dispatch coerces it
            # the same way via _coerce_mcp_arguments), so a stringified-but-valid arguments
            # object is accepted instead of being rejected by the dict-typed schema field.
            if tool_name == "call_mcp_tool" and isinstance(arguments.get("arguments"), str):
                arguments = {**arguments, "arguments": _coerce_mcp_arguments(arguments.get("arguments"))}
            try:
                schema_validator = getattr(schema, "model_validate", None)
                if schema_validator is not None:
                    schema_validator(arguments)
            except ValidationError as exception:
                return ("invalid_tool_arguments", str(exception))
        if tool_name in ("bash", "call_mcp_tool"):
            risk = arguments.get("risk", "low")
            if risk not in ("low", "medium", "high"):
                return ("invalid_risk", f"risk must be one of 'low', 'medium', 'high', got '{risk}'.")
            # Omitted read_only is treated as mutating — the conservative default.
            read_only = arguments.get("read_only", False)
            if not isinstance(read_only, bool):
                return ("invalid_read_only", "read_only must be a boolean.")
        if tool_name == "call_mcp_tool":
            if not arguments.get("server"):
                return ("invalid_mcp_server", "server is required.")
            if not arguments.get("tool_name"):
                return ("invalid_mcp_tool", "tool_name is required.")
        return None

    def _path_like_token(self, token: str) -> str:
        if not token or token in ("-", "--"):
            return ""
        if "://" in token:
            return ""
        if token.startswith("--") and "=" in token:
            token = token.split("=", 1)[1]
        elif token.startswith("-"):
            return ""
        token = token.strip("'\"")
        if not token or token in (".",):
            return ""
        if token.startswith(("~", "/", "./", "../")):
            return token
        if "/" in token:
            return token
        return ""

    def _outside_working_directory_reads(self, command: str, working_directory: str | None = None) -> list[str]:
        """Reads this command names that leave the session's working directory.

        Not a boundary and no longer pretending to be one. Writes are enforced by the operating
        system now (see :mod:`frank.base.confinement`); reads outside the workspace remain allowed
        by design, because agents legitimately read system headers, sibling repositories and
        toolchain configuration. What this does is *notice* them, so the permission classifier and
        the person reading a prompt can see what a command reaches for — a prompt-injection
        tripwire rather than the thing standing between a session and the disk.

        It sees only literal path-shaped arguments. A path the shell computes is invisible to it,
        which was the fatal objection when this was load-bearing and is merely a limitation now.
        """
        if self._global_configuration.sandbox.enforce == "off":
            return []
        root = Path(working_directory or self._working_directory or Path.home()).expanduser()
        try:
            root = root.resolve(strict=False)
        except OSError:
            return []

        outside: list[str] = []
        seen: set[str] = set()
        for segment in self._agent_configuration.tools.bash._extract_segments(command):
            try:
                tokens = shlex.split(segment)
            except ValueError:
                tokens = segment.split()
            for token in tokens[1:]:
                path_token = self._path_like_token(token)
                if not path_token:
                    continue
                path = Path(path_token).expanduser()
                if not path.is_absolute():
                    path = root / path
                try:
                    resolved = path.resolve(strict=False)
                except OSError:
                    continue
                if resolved == root or resolved.is_relative_to(root):
                    continue
                display = str(Path(path_token).expanduser())
                if display not in seen:
                    seen.add(display)
                    outside.append(display)
        return outside

    def _append_tool_results(self, response, outcomes: dict[str, dict]) -> None:
        """Append a ToolMessage for every tool_call of ``response`` (the AIMessage is
        already at the tail of the conversation), plus the image/denied harness notes.
        The ToolMessage block stays contiguous: providers require every tool_call's
        result in the immediately-following turn, so notes come after the whole block.
        An aborted or un-run tool records ``(interrupted)`` so every call still gets a
        ToolMessage and the conversation stays valid."""
        denied_command_notes: list[str] = []
        image_followup_notes: list[dict[str, str]] = []
        for tool_call_data in cast(list[dict], response.tool_calls):
            tool_call_identifier = tool_call_data["id"]
            outcome = outcomes.get(tool_call_identifier, {})
            content = outcome.get("content", "")
            if not content:
                content = "(interrupted)" if self._abort_event.is_set() else ""
            result_status, result_code = _model_result_status(
                content,
                ok=outcome.get("ok", True),
                backgrounded=bool(outcome.get("background_job_id")),
            )
            model_visible_content = _model_visible_tool_result(
                content,
                outcome.get("metadata") or _tool_timing_metadata(
                    tool_name=tool_call_data.get("name", ""),
                    tool_call_identifier=tool_call_identifier,
                    started_at=datetime.now(timezone.utc),
                    completed_at=datetime.now(timezone.utc),
                    duration_milliseconds=0,
                ),
                result_status,
                result_code,
            )
            self._conversation.append(
                ToolMessage(content=model_visible_content, tool_call_id=tool_call_identifier)
            )
            background_job_id = outcome.get("background_job_id")
            if background_job_id:
                self._background.bind_tool_call(
                    background_job_id, tool_call_identifier,
                )
            denied_commands = outcome.get("denied_commands", [])
            if denied_commands:
                commands_list = ", ".join(f"'{command}'" for command in denied_commands)
                denied_command_notes.append(
                    self._prompt_loader.load("command_denied", {"commands": commands_list})
                )
            image_followup_notes.extend(outcome.get("image_followups") or [])
        # Images read this round attach right after the tool block, as image-bearing
        # harness notes — the append-only, every-provider way for a vision model to see
        # pixels a tool produced.
        for followup in image_followup_notes:
            note_text = self._prompt_loader.load("image_read_note", {"path": followup.get("path", "")})
            self._conversation.append(self._harness_note_message(
                note_text,
                image_blocks=[{"type": "image_url", "image_url": {"url": followup["data_uri"]}}],
            ))
        for denied_message in denied_command_notes:
            self._conversation.append(self._harness_note_message(denied_message))

        # Malformed tool calls serialized alongside valid ones: correct them with a
        # harness note (not a ToolMessage — invalid calls aren't in the serialized
        # tool_calls, so a ToolMessage would be orphaned and rejected by strict
        # providers). Model-facing; not surfaced to the user.
        for invalid in response.invalid_tool_calls:
            self._conversation.append(self._harness_note_message(
                self._invalid_tool_call_content(cast(dict, invalid)),
            ))

    async def _through_pipeline(self, tool_name: str, tool_arguments: dict, invoke) -> Any:
        """Run one tool call through the middleware the caller supplied.

        Wraps the invocation rather than `_execute_tool`, which is an async generator of
        events: middleware asks a plain question — did this call happen, and what came back —
        and a generator cannot answer it without the middleware learning the event protocol.

        A raising middleware is *not* absorbed. It is in the call path and decides whether the
        call happens, so swallowing here would turn a retry layer's bug into a tool that
        silently never ran.
        """
        if self._pipeline.empty:
            return await invoke(tool_arguments)
        call = _ToolCall(name=tool_name, arguments=tool_arguments)
        return await self._pipeline.run(call, lambda made: invoke(made.arguments))

    async def _execute_tool(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision,
    ) -> AsyncIterator[TurnEvent]:
        """Execute a single tool call, yielding events. The caller collects results from
        TOOL_RESULT, ERROR, and BACKGROUND_STARTED events.

        Permission is already resolved: ``decision`` says whether this call may run, was
        denied (with the exact error the gate would have produced), or — for ``ask_user``
        — carries the answers. This path never prompts and never awaits a decision future;
        the preflight pass made every human decision before the batch began."""
        # A tool the preflight denied never runs: surface the recorded denial (and the
        # denied-command injection where the inline gate would have), then stop.
        if decision.denial is not None:
            error_kwargs: dict[str, Any] = {"id": tool_call_identifier, "tool": tool_name, "message": decision.denial.get("message", "")}
            if decision.denial.get("code"):
                error_kwargs["code"] = decision.denial["code"]
            yield Error(**error_kwargs)
            if decision.denial.get("denied_injection"):
                yield DeniedInjection(id=tool_call_identifier,
                    command=decision.denial.get("raw_command", ""),
                )
            return

        # Thread this agent's live context window into the tool call, so every window-scaled tool
        # cap (a page's element listing, a read window, a truncation ceiling) is sized for the
        # model actually running. Each tool call runs in its own asyncio task (see
        # _drain_tools_concurrently), so this contextvar is isolated per call and copied into the
        # worker threads the automation surfaces run on; it stays 0 (the turn-0 seed) until the
        # first model call reports usage.
        current_context_window.set(self._context_window)

        # The session-shaped state the module-level tools read at call time: confinement,
        # capability clients, and this session's view of its peers. Bound per call rather than
        # installed per process, so two turns open at once — a compaction alongside a user's —
        # cannot see each other's.
        tool_context.bind(self._tool_context)

        # Coerce any list/dict argument the model passed as a JSON string into its native
        # value up front, so validation and dispatch both see the real container.
        schema = self._tool_schemas.get(tool_name)
        if schema is not None:
            tool_arguments = _coerce_structured_arguments(schema, tool_arguments)

        try:
            self._permissions.check_tool(tool_name, **tool_arguments)
        except PermissionDenied as exception:
            yield Error(id=tool_call_identifier, message=str(exception), tool=tool_name)
            return

        validation_error = self._validate_tool_call(
            tool_name,
            tool_arguments,
        )
        if validation_error:
            error_code, error_message = validation_error
            yield Error(id=tool_call_identifier, code=error_code, message=error_message, tool=tool_name)
            return

        # Filesystem/shell tools run against a specific project location. Resolve it
        # here and derive this call's execution policy as a value — the location's
        # executor carries the IO (local subprocess or multiplexed SSH) through one
        # shared code path. Nothing location-specific is written to runtime state, so
        # tool calls running concurrently against different locations cannot cross
        # policies or working directories.
        resolved_location: ResolvedLocation | None = None
        if tool_name in _LOCATION_TOOLS:
            tool_arguments = dict(tool_arguments)
            location_value = tool_arguments.pop("location", None) or None
            try:
                resolved_location = self._resolve_location(location_value)
            except ToolLocationError as exception:
                yield Error(id=tool_call_identifier, code="invalid_location", message=str(exception), tool=tool_name)
                return
        policy = self._call_policy(resolved_location)

        handler_name = self._TOOL_HANDLERS.get(tool_name)
        if handler_name is None:
            # Not one of ours. A caller-supplied tool arrives here, and it reaches this point
            # having gone through the identical preamble every built-in does — permission
            # resolved, location resolved, policy applied — because the extension point is the
            # *handler*, not the pipeline.
            if tool_name in self._extra_tools:
                async for event in self._tool_supplied(
                    tool_name, tool_arguments, tool_call_identifier,
                ):
                    yield event
                return
            yield Error(id=tool_call_identifier,
                message=f"Unknown tool '{tool_name}'", tool=tool_name,
            )
            return
        async for event in getattr(self, handler_name)(
            tool_name, tool_arguments, tool_call_identifier, decision, policy, resolved_location,
        ):
            yield event

    async def _tool_supplied(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
    ):
        """Run a tool the caller brought, through LangChain's own invocation.

        `ainvoke` rather than an interface of ours: `BaseTool` already defines how a tool is
        called, what it may raise, and how it reports, so every tool written for that ecosystem
        works here unchanged. A tool that raises becomes a tool *error* rather than a turn
        error — the model sees what went wrong and can adapt, which is what it does with every
        built-in failure too."""
        tool = self._extra_tools[tool_name]
        try:
            result = await self._through_pipeline(tool_name, tool_arguments, tool.ainvoke)
        except Exception as error:  # noqa: BLE001 — a caller's tool failing is a tool result
            logger.debug("Supplied tool %s raised", tool_name, exc_info=True)
            yield Error(
                id=tool_call_identifier,
                message=f"{type(error).__name__}: {error}",
                tool=tool_name,
            )
            return
        yield ToolResult(
            id=tool_call_identifier,
            tool=tool_name,
            display=result if isinstance(result, str) else compact(result),
            status=ToolStatus.OK,
        )

    async def _tool_bash(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        raw_command = tool_arguments.get("command", "")
        if policy.is_remote:
            # A remote command runs as a local `ssh …` invocation over the
            # location's multiplexed connection, so the ordinary bash machinery
            # below — sync ceiling, backgrounding, output capping + overflow
            # file, cancellation — drives it unchanged. All permission analysis
            # (static read-only classification, allow rules, prompts) runs on
            # the raw remote command, never on the ssh wrapper.
            from frank.locations.executor import SshExecutor

            assert resolved_location is not None
            executor = resolved_location.executor
            # A remote policy always resolves to the ssh-backed executor.
            assert isinstance(executor, SshExecutor)
            tool_arguments = dict(tool_arguments)
            tool_arguments["command"] = shlex.join(
                executor.ssh_argv(raw_command, resolved_location.base_directory)
            )
        else:
            directory = policy.working_directory
            if directory:
                directory_path = Path(directory).expanduser()
                if not directory_path.is_absolute():
                    yield Error(id=tool_call_identifier,
                        code="invalid_working_directory",
                        message=f"Working directory must be an absolute path: {directory}",
                        tool=tool_name,
                    )
                    return
                if not directory_path.is_dir():
                    yield Error(id=tool_call_identifier,
                        code="invalid_working_directory",
                        message=f"Working directory does not exist: {directory}",
                        tool=tool_name,
                    )
                    return
                # The directory reaches the command as the process's own `cwd`, set through the
                # confinement recipe. It used to be prepended as `cd <dir> && …`, which made the
                # working directory shell text inside a command the model wrote — and therefore
                # something the same command could `cd` straight back out of.
                # A derived context, not a mutation: this used to rewrite the process-wide
                # confinement profile, so a `bash` call naming its own directory narrowed the
                # sandbox of every other turn open in the same worker.
                tool_context.bind(self._tool_context.for_directory(str(directory_path)))
        read_only = tool_arguments.get("read_only", False)
        if isinstance(read_only, str):
            read_only = read_only.lower() == "true"

        # An agent may be forbidden from backgrounding work — a long-lived shell subtree
        # outlives the turn that started it, which is exactly what some agents should not be
        # able to do. The check existed and had no caller, so the setting has never done
        # anything; refusing here turns the model's request into a tool error it can adapt to,
        # rather than silently running the command in the foreground it did not ask for.
        wants_background = tool_arguments.get("background", False)
        if isinstance(wants_background, str):
            wants_background = wants_background.lower() == "true"
        if wants_background:
            try:
                self._permissions.check_bash_background()
            except PermissionDenied as denial:
                yield Error(
                    id=tool_call_identifier, code="background_not_allowed",
                    message=str(denial), tool=tool_name,
                )
                return

        # Permission (sandbox reads, read-only enforcement, risk approval) was
        # resolved by the preflight pass and applied above; an approved bash call
        # reaches here and runs.
        lease_token = ""
        # Filesystem leases guard this machine's working trees; a remote command
        # mutates the remote host, so no local lease applies.
        if not read_only and not policy.is_remote:
            try:
                lease_token = await self._acquire_filesystem_lease(
                    scope="worktree",
                    path=self._canonical_working_directory(policy.working_directory),
                    description=f"mutating bash: {raw_command[:160]}",
                    working_directory=policy.working_directory,
                )
            except FileLeaseConflict as exception:
                yield Error(id=tool_call_identifier,
                    code="filesystem_lease_conflict",
                    message=str(exception),
                    tool=tool_name,
                )
                return

        try:
            background_token = bind_background_jobs(self._background)
            tool_call_token = bind_tool_call_id(tool_call_identifier)
            try:
                result = await bash_tool.ainvoke(tool_arguments)
            finally:
                unbind_tool_call_id(tool_call_token)
                unbind_background_jobs(background_token)
            result_data = _maybe_json(result)
            yield ToolResult(id=tool_call_identifier, name=tool_name, result=result_data)
            if isinstance(result_data, dict) and result_data.get("code") == "bash_started":
                job_id = result_data.get("job_id", "")
                if job_id:
                    self._record_event("background_bash_started", {"job_id": job_id, "command": raw_command})
                    if lease_token and self._background.add_done_callback(
                        job_id,
                        lambda _identifier, token=lease_token: self._release_filesystem_lease(token),
                    ):
                        lease_token = ""
        finally:
            self._release_filesystem_lease(lease_token)


    async def _tool_read_file(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        assert resolved_location is not None
        file_path = str(tool_arguments.get("file_path", ""))
        # Image files are ingested natively: the tool result carries structured
        # metadata (mime, dimensions, size), and when the model has vision the
        # pixels ride along as a data URI on the event under `model_image` —
        # a model-facing side channel _run_one_tool strips before the event
        # reaches the UI, then attaches to the conversation after the tool
        # block as an image-bearing harness note.
        if Path(file_path).suffix.lower() in file_tools.IMAGE_FILE_SUFFIXES:
            result, image_data_uri = await asyncio.to_thread(
                file_tools.read_image_file,
                resolved_location.executor,
                resolved_location.base_directory,
                file_path,
                attach_pixels=self._model_supports_vision(),
            )
            extra = {"model_image": image_data_uri} if image_data_uri else {}
            yield ToolResult(id=tool_call_identifier, name=tool_name, result=_maybe_json(result), extra=extra,
            )
            return
        offset = tool_arguments.get("offset", 1) or 1
        limit_raw = tool_arguments.get("limit")
        limit = int(limit_raw) if limit_raw not in (None, "") else None
        result = await asyncio.to_thread(
            file_tools.read_file,
            resolved_location.executor,
            resolved_location.base_directory,
            file_path,
            int(offset),
            limit,
        )
        result_data = _maybe_json(result)
        # Record the resolved path and hash — keyed by location so the same
        # path on two hosts never collides — so edit_file/write_file can
        # reject stale edits.
        if isinstance(result_data, dict):
            sha256 = result_data.get("sha256")
            resolved_path = result_data.get("path")
            if isinstance(sha256, str) and isinstance(resolved_path, str):
                self._read_files[(resolved_location.uri, resolved_path)] = sha256
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result_data,
        )


    async def _tool_search_code(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        from frank.runtime.tools.code_search import search_code

        query = str(tool_arguments.get("query", ""))
        top_k = int(tool_arguments.get("top_k", 10) or 10)
        reindex = bool(tool_arguments.get("reindex", False))
        # search_code indexes a local directory; a remote location has no local root to index, so
        # it reports that rather than pretending. The default (local) location is the working tree.
        root = resolved_location.base_directory if resolved_location is not None else "."
        if resolved_location is not None and resolved_location.is_remote:
            result: dict = {"ok": False, "error": "search_code runs only on the local codebase; use bash with ripgrep on a remote location."}
        else:
            result = await asyncio.to_thread(search_code, query, root, top_k=top_k, reindex=reindex)
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result)


    async def _run_backgroundable_tool(
        self, tool_name: str, tool_call_identifier: str, coroutine, *, started_code: str,
        sync_window: float, background: bool,
    ) -> AsyncIterator[TurnEvent]:
        """Run an expectedly-slow tool's work as a background job with a synchronous window
        (the proven bash/web_search pattern). A call that finishes within ``sync_window``
        seconds returns its result inline — the common, fast case; one still running past it
        (or ``background=True``, which skips the wait entirely) backgrounds and its result is
        delivered later via the resume pump, so the turn is never blocked. ``sync_window`` is
        the model's ``timeout`` tool parameter — a non-killing inline-wait window, the same
        meaning bash gives ``timeout`` — scaled by the tuning knob here. The coroutine must
        return the tool-result payload as a string (JSON or plain text)."""
        job_id = self._background.spawn(
            tool_name, coroutine, tool_call_identifier=tool_call_identifier,
        )
        settled = None
        if not background:
            settled = await self._background.settle_inline(
                job_id, active_tuning().scale_timeout(sync_window)
            )
        if settled is not None:
            yield ToolResult(id=tool_call_identifier, name=tool_name, result=_maybe_json(settled.result))
        else:
            yield ToolResult(id=tool_call_identifier, name=tool_name, result={
                "code": started_code, "status": "running", "job_id": job_id,
            })

    async def _tool_fetch_url(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        url = str(tool_arguments.get("url", ""))
        fmt = str(tool_arguments.get("format", "markdown") or "markdown")
        sync_window = float(tool_arguments.get("timeout", Tunable.slow_tool_sync_window_seconds.default) or Tunable.slow_tool_sync_window_seconds.default)
        hard_deadline = int(tool_arguments.get("hard_deadline", 30) or 30)
        background = bool(tool_arguments.get("background", False))
        async for event in self._run_backgroundable_tool(
            tool_name, tool_call_identifier, file_tools.fetch_url(url, fmt, hard_deadline),
            started_code="fetch_url_started", sync_window=sync_window, background=background,
        ):
            yield event


    async def _tool_download_file(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        assert resolved_location is not None
        if policy.read_only:
            deny_message = self._prompt_loader.load("read_only_denied", {"violation": "a file download"})
            yield Error(id=tool_call_identifier, message=deny_message, tool=tool_name,
            )
            return
        executor = resolved_location.executor
        url = str(tool_arguments.get("url", ""))
        destination = str(tool_arguments.get("path", ""))
        sync_window = float(tool_arguments.get("timeout", Tunable.slow_tool_sync_window_seconds.default) or Tunable.slow_tool_sync_window_seconds.default)
        hard_deadline = int(tool_arguments.get("hard_deadline", 120) or 120)
        background = bool(tool_arguments.get("background", False))
        resolved = await asyncio.to_thread(executor.resolve, resolved_location.base_directory, destination)
        # A download is a tracked-tree write, so it takes the same filesystem lease as an
        # edit — even when it backgrounds. The lease is held until the write completes: on an
        # inline finish the `finally` releases it; on a background finish it is transferred to
        # the job's done-callback (the local token is cleared so `finally` does not double
        # release). A remote destination mutates the remote host, so no local lease applies.
        lease_token = ""
        if not policy.is_remote:
            try:
                lease_token = await self._acquire_filesystem_lease(
                    scope="file", path=resolved,
                    description=f"{tool_name}: {resolved}",
                    working_directory=policy.working_directory,
                )
            except FileLeaseConflict as exception:
                yield Error(id=tool_call_identifier, code="filesystem_lease_conflict",
                    message=str(exception), tool=tool_name)
                return
        try:
            backgrounded_job_id = ""
            async for event in self._run_backgroundable_tool(
                tool_name, tool_call_identifier, file_tools.download_file(executor, url, resolved, hard_deadline),
                started_code="download_file_started", sync_window=sync_window, background=background,
            ):
                if (
                    isinstance(event, ToolResult) and isinstance(event.result, dict)
                    and event.result.get("code") == "download_file_started"
                ):
                    backgrounded_job_id = str(event.result.get("job_id", ""))
                yield event
            if lease_token and backgrounded_job_id and self._background.add_done_callback(
                backgrounded_job_id,
                lambda _identifier, token=lease_token: self._release_filesystem_lease(token),
            ):
                lease_token = ""
        finally:
            self._release_filesystem_lease(lease_token)


    async def _tool_edit_or_write(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        assert resolved_location is not None
        if policy.read_only:
            deny_message = self._prompt_loader.load("read_only_denied", {"violation": "a file modification"})
            yield Error(id=tool_call_identifier, message=deny_message, tool=tool_name,
            )
            return
        executor = resolved_location.executor
        file_path = str(tool_arguments.get("file_path", ""))
        resolved = await asyncio.to_thread(executor.resolve, resolved_location.base_directory, file_path)
        file_key = (resolved_location.uri, resolved)
        lease_token = ""
        # Filesystem leases guard this machine's files; a remote edit mutates
        # the remote host, so no local lease applies there.
        if not policy.is_remote:
            try:
                lease_token = await self._acquire_filesystem_lease(
                    scope="file",
                    path=resolved,
                    description=f"{tool_name}: {resolved}",
                    working_directory=policy.working_directory,
                )
            except FileLeaseConflict as exception:
                yield Error(id=tool_call_identifier,
                    code="filesystem_lease_conflict",
                    message=str(exception),
                    tool=tool_name,
                )
                return
        try:
            expected_sha256 = self._read_files.get(file_key)
            if tool_name == "edit_file":
                find = str(tool_arguments.get("find", ""))
                replace_with = str(tool_arguments.get("replace_with", ""))
                replace_all = bool(tool_arguments.get("replace_all", False))
                result = await asyncio.to_thread(
                    file_tools.edit_file,
                    executor,
                    resolved_location.base_directory,
                    file_path,
                    find,
                    replace_with,
                    expected_sha256=expected_sha256,
                    replace_all=replace_all,
                )
            else:
                content = tool_arguments.get("content", "")
                if not isinstance(content, str):
                    content = compact(content)
                result = await asyncio.to_thread(
                    file_tools.write_file,
                    executor,
                    resolved_location.base_directory,
                    file_path,
                    content,
                    expected_sha256=expected_sha256,
                )
            result_data = _maybe_json(result)
            if isinstance(result_data, dict):
                result_code = result_data.get("code", "")
                if result_code == "edit_completed":
                    sha256 = result_data.get("sha256")
                    if isinstance(sha256, str):
                        self._read_files[file_key] = sha256
                    else:
                        self._read_files.pop(file_key, None)
                elif result_code == "write_completed":
                    content = tool_arguments.get("content", "")
                    if isinstance(content, str):
                        self._read_files[file_key] = file_tools.content_sha256(content)
                else:
                    # edit_failed_validation or other non-commit codes:
                    # discard stale hash so model must re-read before next edit
                    self._read_files.pop(file_key, None)
            yield ToolResult(id=tool_call_identifier, name=tool_name, result=result_data)
        finally:
            self._release_filesystem_lease(lease_token)


    async def _tool_load_skill(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        skill_name = str(tool_arguments.get("name", ""))
        all_skills = enabled_skills(
            load_skills(self._global_configuration.skill_directories_for(self._project_directory))
        )
        match = next((skill for skill in all_skills if skill.identifier == skill_name), None)
        if match is None:
            yield Error(id=tool_call_identifier,
                message=f"No enabled skill named '{skill_name}'.",
                tool=tool_name,
            )
            return
        result = compact({
            "code": "skill_loaded",
            "name": match.identifier,
            "title": match.display_title,
            "path": match.path,
            "content": match.body,
        })
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=_maybe_json(result),
        )


    async def _tool_wait_for(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        """A cancellable inline wait: the model's own polling primitive. It waits up to
        ``seconds`` but wakes the instant the turn is asked to stop, so a Stop is never
        blocked. No model round-trip happens during the wait, so polling is cheap.

        Non-blocking by construction: it awaits *this runtime's own* ``_abort_event`` with a
        timeout — a cooperative suspension that yields the event loop back to every other
        session and background job. It must never become a blocking ``time.sleep`` (which
        would freeze the whole server) or a threaded sleep: only this one session's turn
        pauses; the harness and all other sessions keep running."""
        raw_seconds = tool_arguments.get("seconds", 0)
        try:
            seconds = max(0.0, float(raw_seconds))
        except (TypeError, ValueError):
            yield ToolResult(id=tool_call_identifier, name=tool_name, result={
                "code": "invalid_arguments",
                "status": ToolStatus.ERROR.value,
                "message": "'seconds' must be a number.",
            })
            return
        interrupted = False
        if seconds > 0:
            try:
                await asyncio.wait_for(self._abort_event.wait(), timeout=seconds)
                interrupted = True  # a Stop fired before the wait elapsed
            except asyncio.TimeoutError:
                interrupted = False  # the full wait elapsed normally
        yield ToolResult(id=tool_call_identifier, name=tool_name, result={
            "code": "interrupted" if interrupted else "waited",
            "seconds": seconds,
            "message": (
                "Wait interrupted by a stop request."
                if interrupted else f"Waited {seconds:g}s; continue."
            ),
        })


    async def _tool_ask_user(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        # ask_user's answers are the preflight "decision" — the question was
        # surfaced as a gate before the batch ran, and the human answered it (or
        # declined) as an input-required response.
        answers = decision.answers
        # The user dismissed the whole prompt without answering (the decline
        # sentinel from resolve_question). Report it to the model and end the
        # turn cleanly — do not proceed on a guess. Setting the abort event lets
        # the tool finish recording its result first, then the stream loop stops
        # the turn; background work the user chose to keep running is untouched.
        if isinstance(answers, dict) and answers.get("__declined__"):
            result = compact({
                "code": "user_declined",
                "message": (
                    "The user dismissed the question without answering and chose to stop here. "
                    "Do not re-ask or proceed on a guess; wait for further direction."
                ),
            })
            yield ToolResult(id=tool_call_identifier, name=tool_name, result=_maybe_json(result),
            )
            self._abort_event.set()
            return
        result = compact({"code": "user_answered", "answers": answers})
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=_maybe_json(result),
        )


    async def _tool_call_mcp_tool(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        # Read-only enforcement and risk approval were resolved by the preflight
        # pass and applied above; an approved MCP call reaches here and runs.
        event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def on_mcp_event(event: dict[str, Any]) -> None:
            await event_queue.put(event)

        call_task = asyncio.create_task(call_mcp_tool_with_events(
            str(tool_arguments.get("server", "")),
            str(tool_arguments.get("tool_name", "")),
            _coerce_mcp_arguments(tool_arguments.get("arguments")),
            on_mcp_event,
        ))
        try:
            while True:
                # Once the MCP call has finished, flush any events still buffered
                # on the queue and stop. Draining synchronously with get_nowait()
                # — rather than racing a fresh getter against the already-done
                # call_task — is what avoids a busy-spin: asyncio.wait returns
                # instantly on the completed call_task, so a queued getter would be
                # cancelled before it could drain the item, leaving the queue
                # non-empty and the loop re-arming a getter forever, pinning a core.
                if call_task.done():
                    while not event_queue.empty():
                        yield Mcp(id=tool_call_identifier,
                            name="call_mcp_tool",
                            server=tool_arguments.get("server", ""),
                            tool=tool_arguments.get("tool_name", ""),
                            event=event_queue.get_nowait(),
                        )
                    break
                get_task = asyncio.create_task(event_queue.get())
                done, pending = await asyncio.wait(
                    {call_task, get_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if get_task in done:
                    yield Mcp(id=tool_call_identifier,
                        name="call_mcp_tool",
                        server=tool_arguments.get("server", ""),
                        tool=tool_arguments.get("tool_name", ""),
                        event=get_task.result(),
                    )
                else:
                    # get_task is still pending (call_task completed first); cancel
                    # only the getter — never call_task, which the loop re-checks.
                    get_task.cancel()
            result_data = await call_task
        except Exception as exception:
            yield Error(id=tool_call_identifier, message=str(exception), tool=tool_name)
            return
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result_data)


    async def _tool_mcp_query(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        tool_map = {
            "list_mcp_tools": list_mcp_tools_tool,
            "list_mcp_resources": list_mcp_resources_tool,
            "read_mcp_resource": read_mcp_resource_tool,
        }
        result = await tool_map[tool_name].ainvoke(tool_arguments)
        result_data = _maybe_json(result)
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result_data)






    async def _tool_set_tasks(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        task_definitions = tool_arguments.get("tasks", [])
        identifiers = self._task_manager.add_tasks(task_definitions)
        self._session_dirty = True
        result_message = f"Created {len(identifiers)} task{'s' if len(identifiers) != 1 else ''}."
        # A normal tool_result — the task list is the model's own bookkeeping, so it
        # completes through the one universal completion path like any other tool. The
        # full task snapshot goes to both the model (it should see the authoritative
        # plan inline) and the UI task panel.
        yield ToolResult(id=tool_call_identifier,
            name=tool_name,
            result={
                "code": "tasks_updated",
                "message": result_message,
                "tasks": self._task_manager.to_dict_list(),
            },
        )


    async def _tool_update_tasks(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        updates = tool_arguments.get("updates", [])
        updated_ids = self._task_manager.update_tasks(updates)
        if updated_ids:
            self._session_dirty = True
            result_message = f"Updated {len(updated_ids)} task{'s' if len(updated_ids) != 1 else ''}."
        else:
            result_message = "No matching tasks found."
        yield ToolResult(id=tool_call_identifier,
            name=tool_name,
            result={
                "code": "tasks_updated",
                "message": result_message,
                "tasks": self._task_manager.to_dict_list(),
            },
        )


    async def _tool_update_goal(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        status = tool_arguments.get("status", "active")
        goal = str(tool_arguments.get("goal", "")).strip()
        if status == "active":
            if not goal:
                result = {
                    "code": "goal_update_error",
                    "status": ToolStatus.ERROR.value,
                    "message": "A non-empty goal is required when status is 'active'.",
                }
            else:
                self._active_goal = goal
                self._session_dirty = True
                result = {
                    "code": "goal_active",
                    "goal": self._active_goal,
                }
                self._record_event("goal_updated", result)
        elif status in ("satisfied", "cleared"):
            previous_goal = self._active_goal
            self._active_goal = ""
            self._session_dirty = True
            result = {
                "code": f"goal_{status}",
                "previous_goal": previous_goal,
            }
            self._record_event("goal_updated", result)
        else:
            result = {
                "code": "goal_update_error",
                "status": ToolStatus.ERROR.value,
                "message": "Status must be one of 'active', 'satisfied', or 'cleared'.",
            }
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result)


    async def _tool_session(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        """Every peer-session and remote-agent verb.

        One handler because they differ only in which call they make: there is no permission
        gate to resolve and no location to apply. A peer cannot hold authority this session
        lacks — the daemon clamps a child against its parent — so creating one grants nothing
        that asking for it would not, and gating it would be ceremony rather than control."""
        from frank.runtime.tools import sessions

        # The create tool is built per-runtime, with the installed profiles baked into its
        # schema, so it is found among this runtime's tools rather than imported.
        create_tool = next((tool for tool in self._tools if tool.name == "create_session"), None)
        background_token = bind_background_jobs(self._background)
        try:
            result = await sessions.invoke(tool_name, tool_arguments, create_tool)
        finally:
            unbind_background_jobs(background_token)
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=_maybe_json(result))


    async def _tool_search_web(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        background_token = bind_background_jobs(self._background)
        try:
            result = await search_web_tool.ainvoke(tool_arguments)
        finally:
            unbind_background_jobs(background_token)
        result_data = _maybe_json(result)
        if isinstance(result_data, dict) and result_data.get("code") == "web_search_started":
            # Attach the "don't poll/read_turn this" guidance from a prompt
            # template, keeping user-facing wording out of the tool code.
            result_data["note"] = self._prompt_loader.load("web_search_started_note", {})
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result_data)


    async def _tool_read_turn(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        requested_turn_id = tool_arguments.get("turn_id", "")
        # A web_search/background-bash handle ("search-…"/"bg-…") is not an A2A
        # task — its results are delivered automatically, never read. Catch the
        # mistake with a redirect instead of a bare "task_not_found" that just
        # invites the model to retry the same wrong poll.
        background_kind = _background_handle_kind(requested_turn_id)
        if self._turn_reader is None:
            result = {"code": "read_turn_unavailable", "message": "Reading turns is not available in this session."}
        elif background_kind is not None:
            message = self._prompt_loader.load(
                "read_turn_background_handle",
                {"job_id": requested_turn_id, "kind": background_kind},
            )
            result = {"code": "not_a_readable_turn", "turn_id": requested_turn_id, "message": message}
        else:
            task = await self._turn_reader(requested_turn_id)
            if task is None:
                result = {"code": "turn_not_found", "turn_id": requested_turn_id}
            else:
                result = task
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result)


    @staticmethod
    def _surface_for(surface_name: str):
        """The live surface a screen tool names: the native macOS tree, or the user's Chrome."""
        from frank.computer import engine as native_surface, web as web_surface

        return native_surface.SURFACE if surface_name == "computer" else web_surface.SURFACE

    async def _tool_control_screen(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        # Run the model's Python in the killable sandbox, bridging each primitive to a trusted action
        # on the chosen surface. Reading and acting are one program: find_one/find_many rank the live
        # surface into elements, and an acting primitive targets an element by the id a find returned
        # or by a fresh query resolved the same way. The surface does its own OS-permission preflight;
        # danger is gated at the tool call by the permission classifier, not here.
        from frank.computer import control, retrieval, surface as surface_module
        from frank.computer.surface import message_loader

        from frank.computer import targets as target_registry

        script = str(tool_arguments.get("script", ""))
        if not script.strip():
            yield ToolResult(id=tool_call_identifier, name=tool_name, result={"ok": False, "error": "control_screen needs a script to run."})
            return
        # A target is a window or a tab, named by the identifier its platform minted. It replaces
        # the old `surface` + `app` pair: `surface` made the model choose an implementation, and an
        # application's display name is not an identity — with two copies open it resolved to
        # whichever was found first, which is a bug the model could not even describe.
        target_id = str(tool_arguments.get("target", "") or "").strip()
        if not target_id:
            yield ToolResult(id=tool_call_identifier, name=tool_name, result={
                "ok": False,
                "error": "control_screen needs a target — the window or tab to act in.",
                "targets": {"current": target_registry.describe_all()},
            })
            return
        target = target_registry.find_target(target_id)
        if target is None:
            # What is *not* said here matters. This used to assert "it was closed, or it never
            # opened" about any target it could not find — and the enumeration behind it only saw
            # the current Space, so on an ordinary machine fifty-six of seventy-four open windows
            # were pronounced dead. A cause nobody checked is worse than no cause: the model
            # passed it on to the user as fact.
            yield ToolResult(id=tool_call_identifier, name=tool_name, result={
                "ok": False,
                "error": f"Target {target_id!r} is not among the windows and tabs I can see.",
                "targets": {"missing": [target_id], "current": target_registry.describe_all()},
            })
            return
        surface_name = target.surface
        surface = self._surface_for(surface_name)
        gate = surface.preflight("documents")
        if gate is not None:
            yield ToolResult(id=tool_call_identifier, name=tool_name, result=gate)
            return

        control_message = message_loader("control")
        known_ids: dict[str, dict[str, str]] = {}   # id -> {id, name, role, context}, from find_* results
        changed: list[dict[str, Any]] = []
        read_failures: list[dict[str, Any]] = []    # structured payloads a failed read produced
        # One place, three questions, so they cannot disagree. They used to be three literals, and
        # they did disagree: `caret` was listed as needing focus and never listed as watched, so
        # the branch that would have focused it was unreachable from the day it was written.
        # Verbs that take an element as their first argument and change it. `evaluate`, `navigate`
        # and the tab verbs change state without naming an element, so they are not here.
        element_mutating_verbs = frozenset({"click", "type", "choose", "upload", "drag"})
        # Verbs whose first argument is an element to resolve, changed or not.
        targeting_verbs = element_mutating_verbs | frozenset({"read", "hover", "scroll", "caret", "select", "focus"})
        # Verbs worth bracketing with a glance, because they can change what is on screen.
        watched_verbs = element_mutating_verbs | frozenset({"press", "navigate", "caret", "select"})
        # Verbs that replace the document wholesale, whose diff is a summary rather than a list.
        navigating_verbs = frozenset({"navigate"})

        def _facets(clickable: Any, name: str, context: str) -> dict:
            """The narrowing a caller asked for. Omitted means *no opinion*, never `False`.

            `clickable` is deliberately tri-state. `True` keeps only what can be activated,
            `False` keeps only what cannot, and leaving it out searches everything — which is the
            right thing to do when you are unsure, and is why it defaults to `None` rather than to
            either boolean."""
            facets: dict = {}
            if clickable is not None:
                facets["clickable"] = bool(clickable)
            if name:
                facets["name"] = name
            if context:
                facets["context"] = context
            return facets

        def _matching(documents: list, facets: dict) -> list:
            """The documents a facet admits, narrowed before anything is ranked.

            Narrowing first rather than filtering a shortlist afterwards is the whole point.
            Ranking inside the facet is worth 12.9% of top-1 accuracy on the browser surface
            (95% interval [12.0%, 13.7%]) against not narrowing at all, and 2.0% [1.7%, 2.4%]
            against narrowing a shortlist — which could report "no match" on a page of six
            hundred buttons whenever none of them reached the top eight.

            The embedding cannot do this itself. Same-role elements sit 0.134 closer in cosine
            than different-role ones — real, and far too weak to isolate one kind of control from
            the 222 others a median browser element shares its role with. Hence a set operation
            rather than words appended to the query.

            There used to be a `role` facet here, and it was withdrawn on evidence. It compared
            against the platform's raw spelling (`AXRadioButton`) while everything the model is
            *shown* says "tab" — so a caller who wrote `role="tab"`, having read exactly that off
            a previous result, matched nothing. In one recorded session six consecutive faceted
            lookups failed that way, and the model, given only "Nothing on the current surface
            matched", concluded that the application's accessibility labels were unstable.

            `clickable` replaces it. It asks about the caller's own intent — am I after something
            I can press, or after text — rather than about a vocabulary they were never given,
            and it is a fact the platform reports rather than a taxonomy anyone invented. Measured
            live across six applications and 338 queries it is worth +4.7% [+2.7%, +7.1%], which
            is 84% of what an oracle-perfect role facet achieves, from a single yes or no.
            """
            if not facets:
                return documents

            def admits(document) -> bool:
                for field, wanted in facets.items():
                    if field == "clickable":
                        if bool(document.payload.get("clickable", False)) is not bool(wanted):
                            return False
                    elif field == "context":
                        # A containment test: `context` names a region and a caller knows part of it.
                        if str(wanted) not in str(document.payload.get(field, "") or ""):
                            return False
                    elif str(document.payload.get(field, "") or "") != str(wanted):
                        # `name` is exact, because a caller quoting one has read it off a result.
                        return False
                return True

            return [document for document in documents if admits(document)]

        def _rank(query: str, limit: int, everything: bool, facets: dict | None = None) -> list:
            # One call, one meaning, both surfaces: the target says where to read.
            raw = surface.documents(target_id)
            if not raw.get("ok"):
                # The script sees a raisable error, and the *structure* survives to the result.
                # This used to be `RuntimeError(raw["error"])`, which threw away everything but
                # the sentence — including, when a window had gone, the current target list that
                # was the one thing the model needed in order to recover.
                read_failures.append({key: value for key, value in raw.items() if key != "ok"})
                raise RuntimeError(raw.get("error", "Could not read the screen."))
            documents = raw.get("documents", [])
            candidates = _matching(documents, facets or {})
            # A facet that admits nothing falls back to the whole surface. Narrowing is a
            # preference, not a precondition: the alternative is what the withdrawn `role` facet
            # did, which was to answer "nothing matched" about a surface that plainly held the
            # thing being asked for, and to do it four times in a row.
            if not candidates and documents:
                logger.info("screen find: facets %r admitted nothing; ranking the whole surface", facets)
                candidates = documents
            hits = retrieval.Index(candidates).search(query, top_k=limit, everything=everything)
            # What the model actually asks for, recorded so the index can be tuned against real
            # queries instead of invented ones. Every encoding decision in
            # ``the-input-is-the-ceiling`` rests on a guess about how often a query names a
            # visible label, a fragment of one, or where a link goes — and that guess is the one
            # assumption in the whole investigation with no measurement behind it. The winning
            # element's own words are logged beside the query, because a query is only
            # interpretable next to what it found.
            logger.info(
                "screen find: surface=%s query=%r results=%d top=%r",
                surface_name, query, len(hits), (hits[0].payload.get("name", "") if hits else ""),
            )
            return hits

        # How much of what appeared is spelled out. Everything new is the most useful thing a
        # model can learn, and "everything new, in full" is a whole document when the action
        # happened to navigate — one Enter key returned an entire help page, truncated mid-payload.
        # The count is always exact; this is only how many are described.
        appeared_detail_limit = 12

        def _hydrate(ids: frozenset[str]) -> dict:
            """Newly-present elements as a count plus a readable sample, never as a wall."""
            if not ids:
                return {}
            known = [known_ids[identifier] for identifier in ids if identifier in known_ids]
            sample = known[:appeared_detail_limit] or [{"id": identifier} for identifier in sorted(ids)[:appeared_detail_limit]]
            report: dict[str, Any] = {"appeared": sample}
            if len(ids) > len(sample):
                report["appeared_total"] = len(ids)
            return report

        async def _record_change(name: str, args: list, before: surface_module.Glance):
            """What one action changed. It observes and it does not act.

            It used to do both. An action that reported no change was silently retried — after
            force-activating the application, stealing the user's screen — with `keywords` dropped
            on the floor, so a retried `type(mode="insert")` became a *replace* and a retried
            `press` lost its modifiers. Then it deleted the `changed: []` it had just written,
            whether or not the retry accomplished anything, because the test was `isinstance(
            retried, dict)` and `perform` always returns a dict. A failed fallback reported as
            success. Focus now lives in the primitive that needs it, where it can be honest about
            what it did, and this function's only job is to say what moved."""
            after = await asyncio.to_thread(surface.glance, target_id)
            moved = surface_module.changes_between(before.facts, after.facts)
            appeared = surface_module.appeared_between(before, after)
            record: dict[str, Any] = {"action": name}
            if args and isinstance(args[0], str):
                record.update(known_ids.get(args[0], {"id": args[0]}))
            record.update(moved)
            navigated = name in navigating_verbs or "url" in moved
            if navigated:
                # The document was replaced, so "everything new" is the whole of it. Reporting the
                # new place and its size is what a person would say; listing every element in it
                # is what the general rule would do, and it is absurd here.
                record["navigated"] = {
                    "title": after.facts.get("title", ""),
                    "url": after.facts.get("url", ""),
                    "elements": len(after.ids),
                }
                record.pop("appeared", None)
            elif appeared:
                record.update(_hydrate(appeared))
            if not moved and not appeared:
                # The one honest answer when nothing observable happened, and the signal the model
                # needs: the click missed, or the pane had not loaded — rather than the element
                # being named something else.
                record["changed"] = []
            if not target.visible:
                # Acting off-screen works (every event is posted to the process, not the screen),
                # but a person deserves to be told their other desktop just moved.
                record["visible"] = False
            return record

        def _record(hit: Any) -> dict:
            return {"id": hit.id, **hit.payload}

        def _register(record: dict) -> None:
            known_ids[record["id"]] = {
                "id": record["id"], "name": record.get("name", ""),
                "role": record.get("role", ""), "context": record.get("context", ""),
            }

        def _identity(record: dict) -> tuple:
            return (record.get("name", ""), record.get("role", ""), record.get("context", ""))

        def _candidates(records: list) -> str:
            """The competing elements, as data rather than as a sentence about data.

            This used to hand-build `  - id=e12, name='Save', role='button'` — a format invented
            here and nowhere else, which the model then had to parse back into fields by reading
            punctuation. Everything else it receives from this tool is JSON through `compact`,
            including the very `find_*` results these records came from, so the one place that
            described elements in prose was the one place it could not simply read them.

            Same key order and same field names as a `find_*` hit, so the answer to "which of
            these did you mean" is written in the vocabulary the question arrived in."""
            return compact([
                {field: record.get(field) for field in ("id", "name", "role", "context")
                 if record.get(field)}
                for record in records
            ])

        def find_many(query: Any, limit: int = 8, all: bool = False, clickable: Any = None,
                      name: str = "", context: str = "", **_: Any) -> list:
            facets = _facets(clickable, name, context)
            records = [_record(hit) for hit in _rank(str(query), int(limit), bool(all), facets)]
            for record in records:
                _register(record)
            return records

        def find_one(query: Any, clickable: Any = None, name: str = "", context: str = "",
                     **_: Any) -> dict:
            facets = _facets(clickable, name, context)
            scored = [(_record(hit), float(hit.score or 0.0)) for hit in _rank(str(query), 8, False, facets)]
            if not scored:
                raise RuntimeError(control_message("no_match", query=str(query)))
            top, top_score = scored[0]
            # Score-competitive: within the top five and at least 90% of the top score. Among those,
            # a twin of the top by (name, role, context) means the query cannot pick one — raise.
            competitive = [record for record, score in scored[:5] if top_score <= 0 or score >= 0.9 * top_score]
            twins = [record for record in competitive[1:] if _identity(record) == _identity(top)]
            if twins:
                raise RuntimeError(control_message("ambiguous_match", query=str(query), candidates=_candidates([top, *twins])))
            _register(top)
            return top

        def _resolve_target(verb: str, args: list) -> list:
            if not args:
                return args
            target = args[0]
            if isinstance(target, dict) and "id" in target:
                return [target["id"], *args[1:]]
            if not isinstance(target, str) or target in known_ids:
                return args
            if verb in element_mutating_verbs:
                resolved = find_one(target)["id"]  # unique-or-raise
            else:  # read / hover / scroll: a wrong non-mutating target is self-correcting, so top-1
                hits = _rank(target, 1, False)
                if not hits:
                    raise RuntimeError(control_message("no_match", query=target))
                record = _record(hits[0])
                _register(record)
                resolved = record["id"]
            return [resolved, *args[1:]]

        async def dispatch(name: str, args: list, keywords: dict) -> Any:
            if name == "find_many":
                return await asyncio.to_thread(find_many, *args, **keywords)
            if name == "find_one":
                return await asyncio.to_thread(find_one, *args, **keywords)
            if name in targeting_verbs:
                args = await asyncio.to_thread(_resolve_target, name, list(args))
            # An action is bracketed by two cheap observations, so the result can say what
            # *changed* rather than only what was touched. `acted_on` answered "what did I aim at",
            # which is the one thing a script already knew; it never answered "did anything
            # happen", and a script that clicks a tab and then cannot find the field inside it
            # could not tell a failed click from a slow pane from a differently-named field.
            watched = name in watched_verbs
            # One glance on each side, not two full reads. Bracketing an action used to cost four
            # accessibility walks, two of which went through `documents` — which rebuilds the
            # id→element map, so merely observing re-pointed every id the script was holding.
            before = await asyncio.to_thread(surface.glance, target_id) if watched else surface_module.Glance()
            outcome = await asyncio.to_thread(surface.perform, target_id, name, list(args), keywords)
            if isinstance(outcome, dict):
                if outcome.get("ok") is False:
                    # Surface a primitive failure into the script as a raised error it can try/except.
                    raise RuntimeError(outcome.get("error", f"{name} failed"))
                if watched:
                    record = await _record_change(name, args, before)
                    if record is not None:
                        changed.append(record)
                # Hand the script the useful value directly: evaluate's result or read's text is the
                # value itself (structured and queryable), an action is its confirmation minus `ok`.
                if "result" in outcome:
                    return outcome["result"]
                if "lines" in outcome:
                    return outcome["lines"]
                return {key: value for key, value in outcome.items() if key != "ok"}
            return outcome

        active = tool_context.current()
        # The baseline the target diff is reported against, taken before the script runs so the
        # model is told what its own actions did to the world rather than a fresh full listing.
        targets_before = target_registry.list_targets()
        result = await control.run_control_script(
            script, dispatch, profile=active.sandbox, workspace=active.workspace,
            # Only what this surface implements, so an absent primitive is an unbound name at the
            # line that used it rather than a runtime payload after the plan was committed to.
            primitives=surface.primitives(),
        )
        if isinstance(result, dict):
            moved = target_registry.difference(targets_before, target_registry.list_targets())
            if moved:
                result.setdefault("targets", moved)
            # Whatever a failed read knew, carried out alongside the message the script saw. The
            # last one wins: it is the state the script actually ended in.
            for failure in read_failures[-1:]:
                for key, value in failure.items():
                    result.setdefault(key, value)
        if changed and isinstance(result, dict):
            result.setdefault("changed", changed)
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result)
