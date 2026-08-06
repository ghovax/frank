"""Executing a tool call: the handlers, the dispatch pipeline, batch draining, and the background runner."""
from __future__ import annotations

import logging
import re
import statistics

from dataclasses import replace
from datetime import datetime, timezone
from frank.base import telemetry as _telemetry
from frank.base import confinement as _confinement
from frank.base.confinement import parse_access_request
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
from frank.runtime.goal import Goal
from frank.base.file_leases import FileLeaseConflict
from frank.base.skills import enabled_skills, load_skills
from frank.runtime.locations import (
    _LOCATION_TOOLS,
    CallExecutionPolicy,
    ResolvedLocation,
    ToolLocationError,
)
from frank.base.tuning import active_tuning, current_context_window, Tunable
from frank.runtime.turn_events import (
    DeniedInjection, Error, Mcp, RetryRequested, ToolCall, ToolResult, TurnEvent,
)
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
from frank.base.errors import summary

logger = logging.getLogger(__name__)

# What an element id looks like on both surfaces, so one can be told from a description of an element.
_ELEMENT_ID = re.compile(r"(?:f\d+)?e\d+|req\d+|ws\d+|\d+(?:\.\d+)+")


def _goal_lines(value: Any) -> list[str]:
    """A goal's requirements as a list of lines, however the model spelled them: a list, or one string."""
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, (list, tuple)):
        return []
    return [line for line in (str(entry).strip() for entry in value) if line]


def _with_schema_defaults(schema: Any, arguments: dict) -> dict:
    """The arguments with every omitted key filled from the schema. Only omitted: an explicit ``None`` stands."""
    fields = getattr(schema, "model_fields", None)
    if not fields:
        return arguments
    filled = dict(arguments)
    for name, field in fields.items():
        if name in filled:
            continue
        default = getattr(field, "default", None)
        if default is not None and default is not Ellipsis and repr(default) != "PydanticUndefined":
            filled[name] = default
    return filled


class _DispatchesTools:
    """Everything the runtime does with a tool call: resolve it, gate it, run it, report it."""

    async def _run_one_tool(
        self,
        tool_call_data: dict,
        turn_tool_calls_log: list[dict],
        turn_tool_results_log: list[dict],
        outcomes: dict[str, dict],
        decision: _ResolvedToolDecision,
    ) -> AsyncIterator[TurnEvent]:
        """Run one call and record its outcome. Self-contained, so it can run concurrently with other tools."""
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
                # An image read carries its pixels on a side channel; strip it so the base64 never reaches the UI.
                if isinstance(event, ToolResult) and "model_image" in event.extra:
                    result_payload = event.result
                    image_followups.append({
                        "path": str((result_payload or {}).get("path", "")) if isinstance(result_payload, dict) else "",
                        "data_uri": str(event.extra["model_image"]),
                    })
                    # Strip the model-facing image before the event goes downstream.
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
                            # A result reporting an error status marks the call failed, for the model and the UI alike.
                            if event.status == ToolStatus.ERROR.value:
                                tool_failed = True
                            # Minified for the model, non-ASCII verbatim. The UI gets the dict on a separate path.
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
        """Run independent calls concurrently, yielding events as they arrive, with every decision already in hand."""
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
                # Race the next event against the abort, so a Stop is honoured even when every tool is parked.
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
        # Models often emit an MCP call's `arguments` as a JSON string; coerce before validating it.
            if tool_name == "call_mcp_tool" and isinstance(arguments.get("arguments"), str):
                arguments = {**arguments, "arguments": _coerce_mcp_arguments(arguments.get("arguments"))}
            try:
                schema_validator = getattr(schema, "model_validate", None)
                if schema_validator is not None:
                    schema_validator(arguments)
            except ValidationError as exception:
                return ("invalid_tool_arguments", str(exception))
            # The validated model is kept — see `_with_schema_defaults` — so a documented default actually applies.
        # Validated once here, so a malformed request fails as a tool error rather than becoming a grant.
        if "access_request" in arguments:
            _, complaint = parse_access_request(arguments.get("access_request"))
            if complaint:
                return ("invalid_access_request", complaint)
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
        """Reads this command names outside the working directory. A signal about intent, not a boundary."""
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
        """A ToolMessage per tool_call, contiguous, since providers require every result in the block that follows."""
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
        # Images attach right after the tool block, the append-only way a vision model sees them.
        for followup in image_followup_notes:
            note_text = self._prompt_loader.load("image_read_note", {"path": followup.get("path", "")})
            self._conversation.append(self._reminder_message(
                note_text,
                image_blocks=[{"type": "image_url", "image_url": {"url": followup["data_uri"]}}],
            ))
        for denied_message in denied_command_notes:
            self._conversation.append(self._reminder_message(denied_message))

        # Malformed calls are corrected by a reminder: a ToolMessage would be orphaned and rejected.
        for invalid in response.invalid_tool_calls:
            self._conversation.append(self._reminder_message(
                self._invalid_tool_call_content(cast(dict, invalid)),
            ))

    async def _through_pipeline(self, tool_name: str, tool_arguments: dict, invoke) -> Any:
        """Run one call through the caller's middleware, which asks a plain question a generator cannot answer."""
        if self._pipeline.empty:
            return await invoke(tool_arguments)
        call = _ToolCall(name=tool_name, arguments=tool_arguments)
        return await self._pipeline.run(call, lambda made: invoke(made.arguments))

    async def _execute_tool(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision,
    ) -> AsyncIterator[TurnEvent]:
        """Execute one call, yielding events. Permission is already resolved, so this path never prompts."""
        # A call that already ran is replayed: whatever side effects it had, it had once.
        if decision.completed is not None:
            yield ToolResult(
                id=tool_call_identifier, name=tool_name, result=decision.completed.get("result"),
            )
            return

        # A refused call never runs: surface the recorded refusal and stop.
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

        # This agent's live window, so every window-scaled cap is sized for the model actually running.
        current_context_window.set(self._context_window)

        # The session state tools read at call time, bound per call so two open turns cannot see each other's.
        tool_context.bind(
            self._tool_context.with_grants(self._access_grants).with_attachments(self._attached_files)
        )

        # Coerce JSON-string arguments up front, so validation and dispatch see the real container.
        schema = self._tool_schemas.get(tool_name)
        if schema is not None:
            tool_arguments = _coerce_structured_arguments(schema, tool_arguments)
            # And fill the schema's defaults, so a documented default is the one that applies.
            tool_arguments = _with_schema_defaults(schema, tool_arguments)

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

        # Resolve the location and derive the policy as a value, so concurrent calls cannot cross them.
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
            # A supplied tool reaches this point through the identical preamble: the handler is the extension point.
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
        """Run a caller's tool through LangChain's own invocation, so anything written for it works here."""
        tool = self._extra_tools[tool_name]
        try:
            result = await self._through_pipeline(tool_name, tool_arguments, tool.ainvoke)
        except Exception as error:  # noqa: BLE001 — a caller's tool failing is a tool result
            logger.debug("supplied tool %s raised", tool_name, exc_info=True)
            yield Error(
                id=tool_call_identifier,
                message=summary(error),
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
            # A remote command is a local `ssh …`, so the bash machinery drives it unchanged; permission read the raw one.
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
                # The directory is the process's own `cwd`, not shell text a command could `cd` out of.
                tool_context.bind(
                    self._tool_context.for_directory(str(directory_path))
                    .with_grants(self._access_grants).with_attachments(self._attached_files)
                )
        requested, _ = parse_access_request(tool_arguments.get("access_request"))
        # No claim is treated as mutating, which is what the lease below depends on.
        declared_read_only = requested is not None and requested.mutates is False

        # Backgrounding can be forbidden: a shell subtree outlives the turn that started it.
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

        # Permission is resolved; an approved call runs inside the confinement the session holds.
        lease_token = ""
        # Leases guard this machine's trees, so a remote command takes none.
        if not declared_read_only and not policy.is_remote:
            try:
                lease_token = await self._acquire_filesystem_lease(
                    scope="worktree",
                    path=self._canonical_working_directory(policy.working_directory),
                    # Whole, since this is what another session is shown when it collides with the lease.
                    description=f"mutating bash: {raw_command}",
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
            if decision.retry_grant is not None:
                # An approved second run goes out with the widening and is not offered again.
                result_data = await self._run_bash(
                    tool_arguments, tool_call_identifier, retry_grant=decision.retry_grant,
                )
                yield ToolResult(id=tool_call_identifier, name=tool_name, result=result_data)
                return
            result_data = await self._run_bash(tool_arguments, tool_call_identifier)
            # A refused command is not a finished one, and its first run was confined, so a retry is safe to offer.
            denial = self._sandbox_denial(result_data, policy)
            if denial is not None:
                verdict, grant = await self.decide_retry(self.retry_gate(
                    tool_call_id=tool_call_identifier, command=raw_command, denial=denial,
                    explanation=str(tool_arguments.get("explanation", "") or ""),
                ))
                if verdict == "run" and grant is not None:
                    result_data = await self._run_bash(
                        tool_arguments, tool_call_identifier, retry_grant=grant,
                    )
                elif verdict == "ask":
                    yield RetryRequested(
                        id=tool_call_identifier, command=raw_command, denial_kind=denial.kind,
                        denial_evidence=denial.evidence,
                        explanation=str(tool_arguments.get("explanation", "") or ""),
                        result=result_data,
                    )
                    return
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


    async def _run_bash(
        self, tool_arguments: dict, tool_call_identifier: str, *, retry_grant=None,
    ) -> Any:
        """One run of the bash tool, inside whatever confinement this attempt carries."""
        if retry_grant is not None:
            tool_context.bind(tool_context.current().for_retry(retry_grant))
        background_token = bind_background_jobs(self._background)
        tool_call_token = bind_tool_call_id(tool_call_identifier)
        try:
            result = await bash_tool.ainvoke(tool_arguments)
        finally:
            unbind_tool_call_id(tool_call_token)
            unbind_background_jobs(background_token)
        return _maybe_json(result)

    def _sandbox_denial(self, result_data: Any, policy: CallExecutionPolicy):
        """Whether a finished command looks like the operating system stopped it. Never remote, never backgrounded."""
        if policy.is_remote or not isinstance(result_data, dict):
            return None
        if result_data.get("code") == "bash_started":
            return None
        exit_code = result_data.get("exit_code")
        if not isinstance(exit_code, int):
            return None
        output = " ".join(
            str(result_data.get(key, "")) for key in ("stdout", "stderr", "output", "error")
        )
        return _confinement.denial_in(
            exit_code=exit_code, output=output,
            attempt=_confinement.first_attempt(
                tool_context.current().sandbox, workspace=policy.working_directory,
            ),
        )

    def _confinement_refusal(
        self, resolved: str, policy: CallExecutionPolicy, *, writing: bool,
    ) -> str:
        """Why the confinement refuses this path. The file tools run in-process, so the profile is applied by hand."""
        if policy.is_remote or not resolved:
            return ""
        profile = tool_context.current().sandbox
        if profile is None:
            return ""
        workspace = policy.working_directory
        permitted = (
            profile.may_write(resolved, workspace=workspace) if writing
            else profile.may_read(resolved, workspace=workspace)
        )
        if permitted:
            return ""
        return self._prompt_loader.load("outside_confinement", {
            "path": resolved,
            "action": "write" if writing else "read",
        })

    async def _tool_read_file(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        assert resolved_location is not None
        file_path = str(tool_arguments.get("file_path", ""))
        resolved_target = await asyncio.to_thread(
            resolved_location.executor.resolve, resolved_location.base_directory, file_path,
        )
        refusal = self._confinement_refusal(resolved_target, policy, writing=False)
        if refusal:
            yield Error(id=tool_call_identifier, code="outside_confinement", message=refusal, tool=tool_name)
            return
        # Images are ingested natively, their pixels riding a side channel the UI never sees.
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
        # Record path and hash, keyed by location, so an edit against a stale read is rejected.
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
        # search_code indexes a local directory, so a remote location reports that rather than pretending.
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
        """Run a slow tool as a background job with a synchronous window, so the fast case returns inline."""
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
        executor = resolved_location.executor
        url = str(tool_arguments.get("url", ""))
        destination = str(tool_arguments.get("path", ""))
        sync_window = float(tool_arguments.get("timeout", Tunable.slow_tool_sync_window_seconds.default) or Tunable.slow_tool_sync_window_seconds.default)
        hard_deadline = int(tool_arguments.get("hard_deadline", 120) or 120)
        background = bool(tool_arguments.get("background", False))
        resolved = await asyncio.to_thread(executor.resolve, resolved_location.base_directory, destination)
        refusal = self._confinement_refusal(resolved, policy, writing=True)
        if refusal:
            yield Error(id=tool_call_identifier, code="outside_confinement", message=refusal, tool=tool_name)
            return
        # A download is a tracked-tree write, so it holds the lease until it completes, background or not.
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
        executor = resolved_location.executor
        file_path = str(tool_arguments.get("file_path", ""))
        resolved = await asyncio.to_thread(executor.resolve, resolved_location.base_directory, file_path)
        refusal = self._confinement_refusal(resolved, policy, writing=True)
        if refusal:
            yield Error(id=tool_call_identifier, code="outside_confinement", message=refusal, tool=tool_name)
            return
        file_key = (resolved_location.uri, resolved)
        lease_token = ""
        # Leases guard this machine's files, so a remote edit takes none.
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
            # Not a commit: discard the stale hash so the model must re-read before editing again.
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
        """A cancellable inline wait: the model's polling primitive, which wakes the instant a Stop arrives."""
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
        # ask_user's answers are its preflight decision: the question was gated before the batch ran.
        answers = decision.answers
        # Dismissed without answering: tell the model and end the turn rather than proceed on a guess.
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
        # Permission is resolved; an approved MCP call runs.
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
                # Once the call is done, drain what is buffered with `get_nowait` rather than racing a fresh getter.
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
                # Cancel only the getter; the loop re-checks `call_task`.
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
        # An ordinary tool result: the task list is the model's own bookkeeping, and both sides see it.
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
        updated_ids, complaints = self._task_manager.update_tasks(updates)
        if updated_ids:
            self._session_dirty = True
            result_message = f"Updated {len(updated_ids)} task{'s' if len(updated_ids) != 1 else ''}."
        else:
            result_message = "Nothing was updated."
        result: dict[str, Any] = {
            "code": "tasks_updated",
            "message": result_message,
            "tasks": self._task_manager.to_dict_list(),
        }
        # What went wrong per update, so a bad key is distinguishable from a task that does not exist.
        if complaints:
            result["rejected"] = complaints
            result["status"] = ToolStatus.ERROR.value if not updated_ids else result.get("status", "")
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result)


    async def _tool_update_goal(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        status = str(tool_arguments.get("status", "active"))
        goal = str(tool_arguments.get("goal", "")).strip()
        requirements = _goal_lines(tool_arguments.get("requirements"))
        evidence = _goal_lines(tool_arguments.get("evidence"))
        blocker = str(tool_arguments.get("blocker", "")).strip()
        current = self.goal

        def refuse(message: str) -> dict:
            return {"code": "goal_update_error", "status": ToolStatus.ERROR.value, "message": message}

        if status == "active":
        # Both halves demanded at once: this is the only moment the goal meets a fresh reading of the request.
            if not goal:
                result = refuse("Say what the goal is: the end state, in one sentence.")
            elif not requirements:
                result = refuse(
                    "A goal needs requirements: the conditions that must hold for it to be met, "
                    "each one something you can check."
                )
            else:
        # The allowance carries across a replacement, or restating the goal would buy an unbounded run.
                self.write_goal(Goal(
                    text=goal,
                    requirements=requirements,
                    continuations=current.continuations if current is not None else 0,
                ))
                result = {"code": "goal_active", "goal": goal, "requirements": requirements}
                self._record_event("goal_updated", result)
        elif status == "satisfied":
            if current is None:
                result = refuse("There is no goal to satisfy.")
            elif not evidence:
        # The audit made structural, so "satisfied" cannot be asserted on memory alone.
                result = refuse(
                    "Say what proves it: for each requirement, what you looked at and what it showed."
                )
            else:
                self.write_goal(None)
                result = {"code": "goal_satisfied", "previous_goal": current.text, "evidence": evidence}
                self._record_event("goal_updated", result)
        elif status == "blocked":
            if current is None:
                result = refuse("There is no goal to report blocked.")
            elif not blocker:
                result = refuse("Say what is in the way, and what would clear it.")
            else:
                self.write_goal(current.model_copy(update={"status": Goal.BLOCKED, "blocker": blocker}))
                result = {"code": "goal_blocked", "goal": current.text, "blocker": blocker}
                self._record_event("goal_updated", result)
        elif status == "cleared":
            if current is None:
                result = refuse("There is no goal to clear.")
            else:
                self.write_goal(None)
                result = {"code": "goal_cleared", "previous_goal": current.text}
                self._record_event("goal_updated", result)
        else:
            result = refuse("Status must be one of 'active', 'satisfied', 'blocked', or 'cleared'.")
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result)


    async def _tool_session(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        """Every peer-session verb, in one handler: they differ only in which call they make."""
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
        # A search or background-bash handle is not an A2A task, so redirect rather than answer task_not_found.
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
        # Run the model's Python in the killable sandbox, bridging each primitive to the chosen surface.
        from frank.computer import control, retrieval, surface as surface_module
        from frank.computer.surface import message_loader

        from frank.computer import targets as target_registry, workflows as workflow_registry

        script = str(tool_arguments.get("script", ""))
        if not script.strip():
            yield ToolResult(id=tool_call_identifier, name=tool_name, result={"ok": False, "error": "control_screen needs a script to run."})
            return
        # A target is a window or tab by the identifier its platform minted, since a display name is not an identity.
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
            # Say what is known rather than assert a cause: an unfound target may simply be on another Space.
            listing = target_registry.list_targets()
            same_app = [place for place in listing if place.app.lower() == target_id.strip().lower()]
            if same_app:
                described = target_registry.describe_all(sorted(same_app, key=target_registry._worth_naming))
                error = (f"{target_id!r} is an application, not a window — an application has no single "
                         f"place to act in. Its windows are listed under 'candidates', likeliest first.")
                payload = {"ok": False, "error": error, "targets": {"candidates": described}}
            else:
                error = f"Target {target_id!r} is not among the windows and tabs I can see."
                payload = {"ok": False, "error": error,
                           "targets": {"missing": [target_id], "current": target_registry.describe_all(listing)}}
            yield ToolResult(id=tool_call_identifier, name=tool_name, result=payload)
            return
        surface_name = target.surface
        surface = self._surface_for(surface_name)
        gate = surface.preflight("documents")
        if gate is not None:
            yield ToolResult(id=tool_call_identifier, name=tool_name, result=gate)
            return

        control_message = message_loader("control")
        # What this call may use: the surface's primitives, less the mutating half unless it was approved.
        from frank.runtime.permissions import MUTATING_SCREEN_PRIMITIVES

        permitted_primitives = set(surface.primitives())
        if not decision.screen_mutations:
            permitted_primitives -= MUTATING_SCREEN_PRIMITIVES
        known_ids: dict[str, dict[str, str]] = {}   # id -> {id, name, role, context}, from find_* results
        changed: list[dict[str, Any]] = []
        read_failures: list[dict[str, Any]] = []    # structured payloads a failed read produced
        # Everything the script did, in order, so a failure on line four says which of the first three happened.
        ran: list[dict[str, Any]] = []
        # One place, three questions, so the verb sets cannot disagree about what needs focus or watching.
        element_mutating_verbs = frozenset({"click", "type", "choose", "upload", "drag"})
        # Verbs whose first argument is an element to resolve, changed or not.
        targeting_verbs = element_mutating_verbs | frozenset({"read", "hover", "scroll", "caret", "select", "focus"})
        # Verbs worth bracketing with a glance. The tab verbs and `evaluate` report themselves, so they stay out.
        watched_verbs = element_mutating_verbs | frozenset({"press", "navigate", "caret", "select"})
        # Verbs that replace the document wholesale, whose diff is a summary rather than a list.
        navigating_verbs = frozenset({"navigate"})

        def _facets(clickable: Any, name: str, context: str) -> dict:
            """The narrowing a caller asked for. `clickable` is tri-state: omitted means no opinion, never `False`."""
            facets: dict = {}
            if clickable is not None:
                facets["clickable"] = bool(clickable)
            if name:
                facets["name"] = name
            if context:
                facets["context"] = context
            return facets

        def _matching(documents: list, facets: dict) -> list:
            """The documents a facet admits, narrowed before ranking, which is worth 12.9% of top-1 accuracy."""
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

        # What has been asked of the screen and what it turned up, kept on the runtime since this spans calls.
        asked: list[tuple[Any, str]] = getattr(self, "_screen_queries_asked", [])
        setattr(self, "_screen_queries_asked", asked)
        rephrased: list[str] = []

        def _note_if_rephrasing(query: str, hits: list) -> None:
            """Notice a search going in circles and say so once, since rephrasing the same query is not retrieval failing."""
            top = hits[0].id if hits else ""
            if not top or rephrased:
                return
            vector = retrieval.intent(query)
            if vector is None:
                return
            alike = active_tuning().ratio(Tunable.find_rephrasing_similarity)
            for earlier, found in asked:
                if found == top and float(earlier @ vector) >= alike:
                    rephrased.append(control_message("rephrasing", query=query))
                    return
            asked.append((vector, top))
            del asked[:-12]

        def _rank(query: str, limit: int, floor: float = 0.0, facets: dict | None = None,
                  near: str = "") -> list:
            # One call, one meaning, both surfaces: the target says where to read.
            raw = surface.documents(target_id)
            if not raw.get("ok"):
            # The script sees a raisable error and the structure survives to the result, target list included.
                read_failures.append({key: value for key, value in raw.items() if key != "ok"})
                raise RuntimeError(raw.get("error", "Could not read the screen."))
            documents = raw.get("documents", [])
            candidates = _matching(documents, facets or {})
            # A facet admitting nothing falls back to the whole surface: narrowing is a preference, not a precondition.
            if not candidates and documents:
                logger.info("screen find: facets %r admitted nothing; ranking the whole surface", facets)
                candidates = documents
            index = retrieval.Index(candidates)
            if near:
                tuning = active_tuning()
                try:
                    hits = index.anchored(
                        query, near, top_k=limit,
                        weight=tuning.ratio(Tunable.find_near_weight),
                        anchor_margin=tuning.ratio(Tunable.find_anchor_margin),
                    )
                except retrieval.WeakAnchor as weak:
                    raise RuntimeError(control_message(
                        "weak_anchor", query=str(query), anchor=weak.anchor,
                    )) from None
            else:
                hits = index.search(query, top_k=limit, floor=floor)
            # What the model actually asks for, so the index can be tuned against real queries rather than invented ones.
            logger.info(
                "screen find: surface=%s query=%r results=%d top=%r",
                surface_name, query, len(hits), (hits[0].payload.get("name", "") if hits else ""),
            )
            _note_if_rephrasing(query, hits)
            return hits

        # How much of what appeared is spelled out, since a navigation makes "everything new" a whole document.
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
            """What one action changed. It observes and does not act, so nothing is retried behind the caller's back."""
            after = await asyncio.to_thread(surface.glance, target_id)
            moved = surface_module.changes_between(before.facts, after.facts)
            appeared = surface_module.appeared_between(before, after)
            record: dict[str, Any] = {"action": name}
            if args and isinstance(args[0], str):
                record.update(known_ids.get(args[0], {"id": args[0]}))
            record.update(moved)
            navigated = name in navigating_verbs or "url" in moved
            if navigated:
                # The document was replaced, so the new place and its size is what a person would say.
                record["navigated"] = {
                    "title": after.facts.get("title", ""),
                    "url": after.facts.get("url", ""),
                    "elements": len(after.ids),
                }
                record.pop("appeared", None)
            elif appeared:
                record.update(_hydrate(appeared))
            if not moved and not appeared:
            # The honest answer when nothing observable happened: the click missed, or the pane had not loaded.
                record["changed"] = []
            if not target.visible:
            # Acting off-screen works, but a person deserves to be told their other desktop just moved.
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
            """The competing elements as data rather than as a sentence about data, so nothing is parsed back out."""
            return compact([
                {field: record.get(field) for field in ("id", "name", "role", "context", "parent", "bounds")
                 if record.get(field)}
                for record in records
            ])

        def find_many(query: Any, limit: int = 8, clickable: Any = None,
                      near: str = "", name: str = "", context: str = "", **_: Any) -> list:
            # The caller's limit, bounded by what any caller may ask for.
            tuning = active_tuning()
            wanted = max(1, min(int(limit), tuning.amount(Tunable.find_many_ceiling)))
            floor = tuning.ratio(Tunable.find_relevance_floor)
            facets = _facets(clickable, name, context)
            records = [_record(hit) for hit in _rank(str(query), wanted, floor, facets, str(near))]
            for record in records:
                _register(record)
            # Recorded whether or not the script keeps the value, so nothing has to be parsed back out of stdout.
            ran.append({"find_many": str(query), "matched": len(records),
                        "ids": [record["id"] for record in records]})
            return records

        def find_one(query: Any, clickable: Any = None, near: str = "", name: str = "",
                     context: str = "", **_: Any) -> dict:
            facets = _facets(clickable, name, context)
            # No floor: the abstention below is a margin fitted over the full ranking, which a floor would change.
            scored = [(_record(hit), float(hit.score or 0.0))
                      for hit in _rank(str(query), 8, 0.0, facets, str(near))]
            if not scored:
                raise RuntimeError(control_message("no_match", query=str(query)))
            top, top_score = scored[0]
            # How many rivals the top match is weighed against, and how many come back when it cannot be chosen.
            shortlist = active_tuning().amount(Tunable.find_candidates)
            # Score-competitive: within the shortlist and at least 90% of the top. A twin there means raise.
            competitive = [record for record, score in scored[:shortlist] if top_score <= 0 or score >= 0.9 * top_score]
            twins = [record for record in competitive[1:] if _identity(record) == _identity(top)]
            if twins:
                raise RuntimeError(control_message("ambiguous_match", query=str(query), candidates=_candidates([top, *twins])))
            # Where the ranker had no real preference: the gap to second, as a fraction of the spread, abstains.
            runner_up = scored[1][1] if len(scored) > 1 else 0.0
            spread = statistics.pstdev([score for _record, score in scored]) if len(scored) > 1 else 0.0
            margin = (top_score - runner_up) / spread if spread > 1e-9 else 1.0
            if margin < active_tuning().ratio(Tunable.find_one_margin):
                raise RuntimeError(control_message(
                    "unsure_match", query=str(query),
                    candidates=_candidates([record for record, _ in scored[:shortlist]]),
                ))
            _register(top)
            # The element it settled on, named rather than reproduced: one record can run to tens of thousands of characters.
            ran.append({"find_one": str(query),
                        "matched": {key: top.get(key) for key in ("id", "role", "name") if top.get(key)}})
            return top

        async def wait_for(query: Any, seconds: float = 5.0, clickable: Any = None, near: str = "",
                           name: str = "", context: str = "", **_: Any) -> dict:
            """Poll a find until it matches and return the element, since a screen builds and animates."""
            deadline = time.monotonic() + max(0.0, float(seconds))
            interval = active_tuning().settle_poll()
            while True:
                # By keyword: the facet order is `find_many`'s business, and positional would re-aim every call.
                hits = await asyncio.to_thread(find_many, query, 1, clickable=clickable,
                                               near=near, name=name, context=context)
                if hits:
                    return hits[0]
                if time.monotonic() >= deadline:
                    raise RuntimeError(control_message("waited_in_vain", query=str(query), seconds=f"{seconds:g}"))
                await asyncio.sleep(interval)

        def _resolve_target(verb: str, args: list) -> list:
            if not args:
                return args
            target = args[0]
            # The element a find returned, passed whole — the most reliable way to act on a result.
            if isinstance(target, dict) and "id" in target:
                return [target["id"], *args[1:]]
            if not isinstance(target, str) or target in known_ids:
                return args
            # An id is an id even unseen here, so it goes to the surface, which is what resolves ids.
            if _ELEMENT_ID.fullmatch(target):
                return args
            if verb in element_mutating_verbs:
                resolved = find_one(target)["id"]  # unique-or-raise
            else:  # read / hover / scroll: a wrong non-mutating target is self-correcting, so top-1
                hits = _rank(target, 1, 0.0)
                if not hits:
                    raise RuntimeError(control_message("no_match", query=target))
                record = _record(hits[0])
                _register(record)
                resolved = record["id"]
            return [resolved, *args[1:]]

        async def dispatch(name: str, args: list, keywords: dict) -> Any:
            # What this session may do is enforced by `run_control_script`, which refuses before this is reached.
            if name == "find_many":
                return await asyncio.to_thread(find_many, *args, **keywords)
            if name == "find_one":
                return await asyncio.to_thread(find_one, *args, **keywords)
            if name == "wait_for":
                return await wait_for(*args, **keywords)
            if name in targeting_verbs:
                args = await asyncio.to_thread(_resolve_target, name, list(args))
            # An action is bracketed by two cheap observations, so the result says what changed, not what was touched.
            watched = name in watched_verbs
            # A glance on each side, not a full read: reading rebuilds the id map and re-points every held id.
            before = await asyncio.to_thread(surface.glance, target_id) if watched else surface_module.Glance()
            outcome = await asyncio.to_thread(surface.perform, target_id, name, list(args), keywords)
            if isinstance(outcome, dict):
                if outcome.get("ok") is False:
                    ran.append({name: args[0] if args else "", "failed": outcome.get("error", "")})
                    # Surface a primitive failure into the script as a raised error it can try/except.
                    raise RuntimeError(outcome.get("error", f"{name} failed"))
                step: dict[str, Any] = {name: args[0] if args and isinstance(args[0], str) else ""}
                if watched:
                    record = await _record_change(name, args, before)
                    if record is not None:
                        changed.append(record)
                        step.update({key: value for key, value in record.items() if key != "action"})
                ran.append(step)
            # Hand the script the useful value: a result or text directly, an action its confirmation.
                if "result" in outcome:
                    return outcome["result"]
                if "lines" in outcome:
                    return outcome["lines"]
                return {key: value for key, value in outcome.items() if key != "ok"}
            return outcome

        active = tool_context.current()
        # The baseline the target diff is reported against, so the model is told what its own actions did.
        targets_before = target_registry.list_targets()
        result = await control.run_control_script(
            script, dispatch, profile=active.sandbox, workspace=active.workspace,
            # Only what this surface implements and this session may run, so a missing name fails against the surface.
            primitives=tuple(sorted(permitted_primitives)),
            # The place the script drives, so the child binds a `screen` already pointed at it.
            target=target_id,
            # Where saved workflows live, so `from workflows.x import y` reaches them.
            import_roots=workflow_registry.import_roots(self._project_directory or active.workspace or ""),
            # And what those skills installed into, so a package with dependencies works.
            dependency_roots=workflow_registry.dependency_roots(self._project_directory or active.workspace or ""),
            # And where those dependencies keep the shared libraries they were built against.
            library_roots=workflow_registry.library_roots(self._project_directory or active.workspace or ""),
        )
        if isinstance(result, dict):
            moved = target_registry.difference(targets_before, target_registry.list_targets())
            if moved:
                result.setdefault("targets", moved)
        # Whatever a failed read knew, carried alongside the message. The last one wins: it is where the script ended.
            for failure in read_failures[-1:]:
                for key, value in failure.items():
                    result.setdefault(key, value)
        if changed and isinstance(result, dict):
            result.setdefault("changed", changed)
        # What ran, in order, finished or not: on a failure this is the difference from starting over.
        if ran and isinstance(result, dict):
            result.setdefault("ran", ran)
        # Said once per call, and beside what the script returned: this is an observation, not a failure.
        if rephrased and isinstance(result, dict):
            result.setdefault("note", rephrased[0])
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result)
