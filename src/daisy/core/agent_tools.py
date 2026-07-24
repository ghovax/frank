"""The AgentRuntime tool-execution concern (a mixin composed into AgentRuntime).

The ``_tool_*`` handlers, the ``_execute_tool`` permission/location/dispatch pipeline, concurrent
batch draining, argument validation, and the backgroundable-tool runner. Imports its dependencies
from the leaf ``agent_internals`` module and stable modules, so the graph stays a clean DAG."""
from __future__ import annotations

from a2a.types import TaskState
from dataclasses import replace
from datetime import datetime
from datetime import timezone
from daisy.core import telemetry as _telemetry
from daisy.core.agent_internals import _ResolvedToolDecision
from daisy.core.agent_internals import _background_handle_kind
from daisy.core.agent_internals import _cap_model_result_payload
from daisy.core.agent_internals import _coerce_mcp_arguments
from daisy.core.agent_internals import _coerce_structured_arguments
from daisy.core.agent_internals import _maybe_json
from daisy.core.agent_internals import _model_result_status
from daisy.core.agent_internals import _model_visible_tool_result
from daisy.core.agent_internals import _spawned_agent_report
from daisy.core.agent_internals import _tool_timing_metadata
from daisy.core.agent_internals import _utc_timestamp
from daisy.core.agent_runner import AgentRunner
from daisy.core.background import bind_background_jobs
from daisy.core.background import bind_tool_call_id
from daisy.core.background import unbind_background_jobs
from daisy.core.background import unbind_tool_call_id
from daisy.core.events import ToolStatus
from daisy.core.file_leases import FileLeaseConflict
from daisy.core.skills import enabled_skills
from daisy.core.skills import load_skills
from daisy.core.tool_policy import CallExecutionPolicy
from daisy.core.tool_policy import ResolvedLocation
from daisy.core.tool_policy import ToolLocationError
from daisy.core.tool_policy import _LOCATION_TOOLS
from daisy.core.tuning import Limit
from daisy.core.tuning import active_tuning
from daisy.core.tuning import current_context_window
from daisy.core.turn_events import DelegateDone
from daisy.core.turn_events import DelegateStarted
from daisy.core.turn_events import DelegateUsage
from daisy.core.turn_events import DeniedInjection
from daisy.core.turn_events import Done
from daisy.core.turn_events import Error
from daisy.core.turn_events import GroupStarted
from daisy.core.turn_events import Mcp
from daisy.core.turn_events import ToolCall
from daisy.core.turn_events import ToolResult
from daisy.core.turn_events import TurnEvent
from daisy.identifiers import new_id
from daisy.tools import file_tools
from daisy.tools.tools import artifact_kind_for
from daisy.tools.tools import bash as bash_tool
from daisy.tools.tools import build_open_artifact_result
from daisy.tools.tools import call_mcp_tool_with_events
from daisy.tools.tools import list_mcp_resources as list_mcp_resources_tool
from daisy.tools.tools import list_mcp_tools as list_mcp_tools_tool
from daisy.tools.tools import read_mcp_resource as read_mcp_resource_tool
from daisy.tools.tools import search_web as search_web_tool
from langchain_core.messages import ToolMessage
from pathlib import Path
from pydantic import ValidationError
from typing import Any
from typing import AsyncIterator
from typing import cast
import asyncio
import json
import shlex
import time




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
        background_task_identifier: str | None = None
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
                        raw_task_identifier = result_str.get("task_identifier")
                        background_task_identifier = (
                            raw_task_identifier if isinstance(raw_task_identifier, str) else None
                        )
                        result_content = json.dumps({
                            "code": "background_task_scheduled",
                            "task_identifier": background_task_identifier,
                        })
                        turn_tool_results_log.append({"name": tool_name, "result": json.dumps(result_str)})
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
                            result_str = json.dumps(result_str, ensure_ascii=False, separators=(",", ":"))
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
            background_task_identifier=background_task_identifier,
        )

        outcomes[tool_call_identifier] = {
            "content": result_content,
            "ok": not tool_failed,
            "background_task_identifier": background_task_identifier,
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
        when work is parallel; they run concurrently so multiple spawned agents
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
        """Best-effort static path boundary check for bash commands.

        The shell remains too dynamic to prove every access, so this intentionally
        catches explicit path arguments that leave the session working directory:
        absolute paths, home paths, and parent-directory traversal.
        """
        if not self._global_configuration.sandbox.enabled:
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
                backgrounded=bool(outcome.get("background_task_identifier")),
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
            background_task_identifier = outcome.get("background_task_identifier")
            if background_task_identifier:
                self._background.bind_tool_call(
                    background_task_identifier, tool_call_identifier,
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
            self._conversation.append(self._daisy_note_message(
                note_text,
                image_blocks=[{"type": "image_url", "image_url": {"url": followup["data_uri"]}}],
            ))
        for denied_message in denied_command_notes:
            self._conversation.append(self._daisy_note_message(denied_message))

        # Malformed tool calls serialized alongside valid ones: correct them with a
        # harness note (not a ToolMessage — invalid calls aren't in the serialized
        # tool_calls, so a ToolMessage would be orphaned and rejected by strict
        # providers). Model-facing; not surfaced to the user.
        for invalid in response.invalid_tool_calls:
            self._conversation.append(self._daisy_note_message(
                self._invalid_tool_call_content(cast(dict, invalid)),
            ))

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

        # Coerce any list/dict argument the model passed as a JSON string into its native
        # value up front, so validation and dispatch both see the real container.
        schema = self._tool_schemas.get(tool_name)
        if schema is not None:
            tool_arguments = _coerce_structured_arguments(schema, tool_arguments)

        try:
            self._permissions.check_tool(tool_name, **tool_arguments)
        except PermissionError as exception:
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
            yield Error(id=tool_call_identifier,
                message=f"Unknown tool '{tool_name}'", tool=tool_name,
            )
            return
        async for event in getattr(self, handler_name)(
            tool_name, tool_arguments, tool_call_identifier, decision, policy, resolved_location,
        ):
            yield event

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
            from daisy.locations.executor import SshExecutor

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
                tool_arguments = dict(tool_arguments)
                tool_arguments["command"] = f"cd {shlex.quote(str(directory_path))} && {raw_command}"
        read_only = tool_arguments.get("read_only", False)
        if isinstance(read_only, str):
            read_only = read_only.lower() == "true"

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
            is_background_command = isinstance(result_data, dict) and result_data.get("code") == "bash_started"
            if resolved_location is not None and not read_only and not is_background_command:
                # A completed mutating command may have regenerated a file we already
                # track — restage only the tracked set (cheap; never surveys the folder).
                self._capture_written_artifacts(
                    resolved_location, changed_absolute_paths=None, mode="recheck",
                    tool_call_id=tool_call_identifier, message=f"bash: {raw_command[:80]}",
                )
            if isinstance(result_data, dict) and result_data.get("code") == "bash_started":
                task_identifier = result_data.get("task_identifier", "")
                if task_identifier:
                    self._record_event("background_bash_started", {"task_identifier": task_identifier, "command": raw_command})
                    if lease_token and self._background.add_done_callback(
                        task_identifier,
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
        from daisy.tools.code_search import search_code

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
        task_identifier = self._background.spawn(
            tool_name, coroutine, tool_call_identifier=tool_call_identifier,
        )
        settled = None
        if not background:
            settled = await self._background.settle_inline(
                task_identifier, active_tuning().scale_timeout(sync_window)
            )
        if settled is not None:
            yield ToolResult(id=tool_call_identifier, name=tool_name, result=_maybe_json(settled.result))
        else:
            yield ToolResult(id=tool_call_identifier, name=tool_name, result={
                "code": started_code, "status": "running", "task_identifier": task_identifier,
            })

    async def _tool_fetch_url(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        url = str(tool_arguments.get("url", ""))
        fmt = str(tool_arguments.get("format", "markdown") or "markdown")
        sync_window = float(tool_arguments.get("timeout", Limit.SLOW_TOOL_SYNC_WINDOW_SECONDS.baseline) or Limit.SLOW_TOOL_SYNC_WINDOW_SECONDS.baseline)
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
        sync_window = float(tool_arguments.get("timeout", Limit.SLOW_TOOL_SYNC_WINDOW_SECONDS.baseline) or Limit.SLOW_TOOL_SYNC_WINDOW_SECONDS.baseline)
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
            backgrounded_task_id = ""
            async for event in self._run_backgroundable_tool(
                tool_name, tool_call_identifier, file_tools.download_file(executor, url, resolved, hard_deadline),
                started_code="download_file_started", sync_window=sync_window, background=background,
            ):
                if (
                    isinstance(event, ToolResult) and isinstance(event.result, dict)
                    and event.result.get("code") == "download_file_started"
                ):
                    backgrounded_task_id = str(event.result.get("task_identifier", ""))
                yield event
            if lease_token and backgrounded_task_id and self._background.add_done_callback(
                backgrounded_task_id,
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
                    content = json.dumps(content)
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
                if result_code in ("edit_completed", "write_completed"):
                    # Version exactly this file. For an edit of a pre-existing file, pass
                    # its pre-edit bytes (the tool's "before") so the first version we
                    # keep is the original — the file on disk is already the edited copy.
                    original_contents = None
                    before_content = result_data.get("before")
                    if not result_data.get("created") and isinstance(before_content, str):
                        original_contents = {resolved: before_content}
                    self._capture_written_artifacts(
                        resolved_location, changed_absolute_paths=[resolved],
                        original_contents=original_contents,
                        tool_call_id=tool_call_identifier, message=f"{tool_name} {Path(resolved).name}",
                    )
            yield ToolResult(id=tool_call_identifier, name=tool_name, result=result_data,
            )
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
        result = json.dumps({
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
            result = json.dumps({
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
        result = json.dumps({"code": "user_answered", "answers": answers})
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


    async def _tool_ask_agent(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        task_identifier = str(tool_arguments.get("task_identifier", "")).strip()
        question = str(tool_arguments.get("question", "")).strip()
        if self._ask_agent is None or not self._a2a_task_id:
            result = {
                "code": "agent_messaging_unavailable",
                "message": "Agent messaging is not available in this execution context.",
            }
        elif not task_identifier:
            result = {
                "code": "invalid_agent_identifier",
                "message": "Pass an exact identifier from active_agents or spawn_agent.",
            }
        elif not question:
            result = {
                "code": "empty_agent_question",
                "message": "Provide the question to send.",
            }
        else:
            result = self._ask_agent(self._a2a_task_id, task_identifier, question)
            if result.get("code") == "agent_question_queued":
                self._outstanding_agent_questions.add(str(result["message_identifier"]))
        yield ToolResult(id=tool_call_identifier,
            name=tool_name,
            result=result,
        )


    async def _tool_respond_agent(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        message_identifier = str(tool_arguments.get("message_identifier", "")).strip()
        response_text = str(tool_arguments.get("response", "")).strip()
        if self._respond_agent is None or not self._a2a_task_id:
            result = {
                "code": "agent_messaging_unavailable",
                "message": "Agent messaging is not available in this execution context.",
            }
        elif not response_text:
            result = {
                "code": "empty_agent_response",
                "message": "Provide the response to send.",
            }
        else:
            result = self._respond_agent(
                self._a2a_task_id,
                message_identifier,
                response_text,
            )
            if result.get("code") in {
                "agent_response_delivered",
                "agent_question_withdrawn",
            }:
                self._pending_agent_questions.discard(message_identifier)
        yield ToolResult(id=tool_call_identifier,
            name=tool_name,
            result=result,
        )


    async def _tool_spawn_or_remote(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        # No delegation-depth ceiling: a spawned agent runs its own turn loop, governed by the
        # model's own judgment of when it is done and the user's ability to interrupt, and
        # genuine deep delegation that keeps producing deliverables is legitimate. The child's
        # depth is still tracked, for context and telemetry.
        child_depth = self._delegation_depth + 1

        raw_agent_prompt = tool_arguments.get("prompt", "")
        # The agents panel heading is the model's user-facing justification (a
        # concise "why this agent" clause), never the whole prompt.
        agent_title = str(tool_arguments.get("justification", "")).strip()
        agent_name = tool_arguments.get("agent") or self._global_configuration.default_agent
        agent_read_only = tool_arguments.get("read_only", None)
        if isinstance(agent_read_only, str):
            agent_read_only = agent_read_only.lower() == "true"
        # The caller's approval grant for this delegated agent; bypass is not accepted here,
        # and the executor combines it with the delegated agent card and clamps it.
        agent_permission_mode = str(tool_arguments.get("permission_mode", "") or "")
        if agent_permission_mode not in ("default", "auto", "read_only"):
            agent_permission_mode = ""
        agent_prompt = self._build_agent_prompt(raw_agent_prompt, agent_read_only)
        spawn_step_id = new_id("agent")

        # spawn_agent is local-only and call_remote_agent is remote-only — the two are
        # distinct concepts, so a name for the wrong one is rejected with a pointer to
        # the right tool.
        is_remote_agent = self._is_remote_agent(agent_name)
        if tool_name == "call_remote_agent" and not is_remote_agent:
            yield Error(id=tool_call_identifier, tool=tool_name,
                message=f"{agent_name!r} is not a known remote agent. Use spawn_agent for a local agent.",
            )
            return
        if tool_name == "spawn_agent" and is_remote_agent:
            yield Error(id=tool_call_identifier, tool=tool_name,
                message=f"{agent_name!r} is a remote agent. Use call_remote_agent to reach it.",
            )
            return
        # A remote agent lives on another server, so there is no on-disk config to
        # load or validate — it is resolved over the wire by the delegate. A local
        # agent must resolve to a real config file, or the call is rejected here.
        sub_configuration = None
        if not is_remote_agent:
            try:
                sub_configuration = self._load_agent(agent_name)
            except FileNotFoundError as exception:
                yield Error(id=tool_call_identifier, message=str(exception), tool=tool_name)
                return

        # Contacting a remote agent sends the prompt and attachments off this machine.
        # That egress consent was resolved by the preflight pass (asked once per agent
        # per session; an "always allow" was applied to the approved set above), so an
        # approved call reaches here and runs.

        # Spawning is non-blocking: the agent runs as a background job (a
        # related A2A task) and the parent continues immediately. The agent's
        # activity streams live into the agents panel via the shared event queue,
        # and its structured deliverable (the child task) is injected into the
        # conversation and wakes the parent when it finishes — the same
        # inject-and-wake path as a background bash command. So the parent never
        # blocks on an agent: it can spawn several in parallel, keep working,
        # and pick up each result as it lands.
        group_id = f"agents-{self._a2a_task_id or self._session_id}"
        yield GroupStarted(group_id=group_id,
            step_id=spawn_step_id,
            agent_name=agent_name,
            title=agent_title,
            tool_call_id=tool_call_identifier,
        )
        self._record_event("agent_spawned", {"task_identifier": spawn_step_id, "agent": agent_name, "prompt": raw_agent_prompt})
        delegate = self._delegate
        if delegate is not None:
            if self._reserve_agent is not None:
                self._reserve_agent(spawn_step_id, self._session_id, agent_name)

            async def _run_spawned_agent() -> str:
                child_task = None
                child_task_id = ""
                try:
                    async for delegated in delegate(
                        agent_name,
                        agent_prompt,
                        self._a2a_task_id,
                        agent_read_only,
                        child_depth,
                        self._working_directory,
                        self._project_directory,
                        group_id,
                        spawn_step_id,
                        self._pending_attachments,
                        agent_permission_mode,
                    ):
                        # The agent's streamed progress goes to the panel only,
                        # never into the parent's model context.
                        if isinstance(delegated, DelegateStarted):
                            child_task_id = delegated.child_task_id
                            continue
                        if isinstance(delegated, DelegateUsage):
                            usage = delegated.event
                            self._add_agent_usage(
                                int(usage.get("input_tokens", 0) or 0),
                                int(usage.get("output_tokens", 0) or 0),
                            )
                            continue
                        event = self._relay_child_event(delegated, group_id, spawn_step_id)
                        if event is not None:
                            await self._publish_spawned_agent_event(event)
                        if isinstance(delegated, DelegateDone):
                            child_task = delegated.task
                            # The child's turn is a control signal, not a relayed panel
                            # event; synthesize a `done` at this step's path so the panel
                            # marks it terminal.
                            child_state = str(((child_task or {}).get("status") or {}).get("state") or "completed")
                            await self._settle_agent_lane(group_id, spawn_step_id, child_state)
                    # Inject ONLY the agent's final report (its deliverable
                    # artifact) into the parent — not the whole task object, and not
                    # any of the intermediate progress. The parent asked for a result,
                    # not a transcript.
                    return _spawned_agent_report(child_task, agent_name)
                except asyncio.CancelledError:
                    if child_task_id and self._cancel_delegated is not None:
                        await asyncio.shield(self._cancel_delegated(agent_name, child_task_id))
                    await asyncio.shield(self._settle_agent_lane(
                        group_id,
                        spawn_step_id,
                        TaskState.canceled.value,
                    ))
                    raise
                finally:
                    if self._release_reserved_agent is not None:
                        self._release_reserved_agent(spawn_step_id)

            # Spawned agents own an independent lifecycle: stopping the parent turn
            # leaves them running, while targeted cancellation still cancels this
            # driver and, through its CancelledError handler, the child A2A task.
            spawned_task_id = self._background.spawn(
                "spawn_agent",
                _run_spawned_agent(),
                identifier=spawn_step_id,
                tool_call_identifier=tool_call_identifier,
                arguments={
                    "agent": agent_name,
                    "prompt": raw_agent_prompt,
                    "read_only": agent_read_only,
                    "justification": agent_title,
                },
                detached=True,
            )
            yield ToolResult(id=tool_call_identifier, name=tool_name,
                result={
                    "code": "agent_started",
                    "status": ToolStatus.RUNNING.value,
                    "task_identifier": spawned_task_id,
                    "agent": agent_name,
                    "message": self._prompt_loader.load("agent_started_note", {"agent": agent_name}).strip(),
                },
            )
        elif is_remote_agent:
            # A remote agent can only be reached through the delegate (the wire path).
            # With no delegate installed there is nothing to run locally.
            yield Error(id=tool_call_identifier, tool=tool_name,
                message=f"Remote agent {agent_name!r} requires the delegation runtime, which is not available here.",
            )
            return
        else:
            runner = AgentRunner(
                agent_configuration=sub_configuration,
                global_configuration=self._global_configuration,
                task_identifier=spawn_step_id,
                prompt=agent_prompt,
                stream_progress=self._agent_configuration.stream_agent_progress,
                read_only_override=agent_read_only,
                working_directory=self._working_directory,
                project_directory=self._project_directory,
            )

            # The standalone fallback uses the same background runner and public
            # handle contract as an A2A delegation, so it can be canceled too.
            async def _run_background() -> str:
                final_text = ""
                try:
                    async for event in runner.run_stream(always_yield_text=True):
                        if isinstance(event, Done):
                            final_text = event.text or final_text
                    await self._settle_agent_lane(group_id, spawn_step_id, TaskState.completed.value)
                    return final_text
                except asyncio.CancelledError:
                    await asyncio.shield(self._settle_agent_lane(
                        group_id,
                        spawn_step_id,
                        TaskState.canceled.value,
                    ))
                    raise

            self._background.spawn(
                "spawn_agent",
                _run_background(),
                identifier=spawn_step_id,
                tool_call_identifier=tool_call_identifier,
                arguments={
                    "agent": agent_name,
                    "prompt": raw_agent_prompt,
                    "read_only": agent_read_only,
                    "justification": agent_title,
                },
                detached=True,
            )
            yield ToolResult(id=tool_call_identifier, name=tool_name,
                result={
                    "code": "agent_started",
                    "status": ToolStatus.RUNNING.value,
                    "task_identifier": spawn_step_id,
                    "agent": agent_name,
                    "message": self._prompt_loader.load("agent_started_note", {"agent": agent_name}).strip(),
                },
            )


    async def _tool_cancel_agent(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        task_identifier = str(tool_arguments.get("task_identifier", "")).strip()
        if not task_identifier.startswith("agent-"):
            result = {
                "code": "invalid_agent_handle",
                "task_identifier": task_identifier,
                "message": "Pass the agent-... task identifier returned by spawn_agent.",
            }
        elif self._background.cancel_by_identifier(task_identifier, kind="spawn_agent"):
            result = {
                "code": "agent_cancellation_requested",
                "task_identifier": task_identifier,
                "message": "The spawned agent was canceled.",
            }
            self._record_event("agent_cancelled", {"task_identifier": task_identifier})
        else:
            result = {
                "code": "agent_not_running",
                "task_identifier": task_identifier,
                "message": "No running spawned agent has that identifier.",
            }
        yield ToolResult(id=tool_call_identifier,
            name=tool_name,
            result=result,
        )


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


    async def _tool_open_artifact(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        if self._is_agent:
            yield Error(id=tool_call_identifier,
                tool=tool_name,
                code="agent_artifact_denied",
                message="Agents cannot open artifacts. Return findings only as text for the parent agent.",
            )
            return
        raw_target = str(tool_arguments.get("url", "")).strip()
        if not raw_target:
            yield Error(id=tool_call_identifier, tool=tool_name,
                code="empty_artifact", message="A URL or file path is required to open an artifact.",
            )
            return
        requested_artifact_id = str(tool_arguments.get("artifact_id", "")).strip()
        requested_height = tool_arguments.get("height", 0)
        lowered = raw_target.lower()

        if lowered.startswith(("http://", "https://")):
            # An external URL renders as a live iframe with no version history — only
            # surface it (no capture). A stable id derived from the URL reuses one tab.
            artifact_id = requested_artifact_id or self._artifact_surface_id(raw_target)
            self._capture_written_artifacts(
                self._resolve_location(None), changed_absolute_paths=[],
                tool_call_id=tool_call_identifier, message="open_artifact",
                surface={"surface_id": artifact_id, "kind": "iframe", "title": raw_target, "source": raw_target, "absolute_path": ""},
            )
            result = build_open_artifact_result(
                artifact_id=artifact_id, kind="iframe", title=raw_target, source=raw_target,
                url=raw_target, height=requested_height,
            )
            yield ToolResult(id=tool_call_identifier, name=tool_name, result=result)
            return

        # A local (or remote) file path, resolved against a location. Capture the file
        # as a version and surface it as a tab; its history is served from the shadow repo.
        location_value = tool_arguments.get("location", None) or None
        try:
            resolved_location = self._resolve_location(location_value)
        except ToolLocationError as exception:
            yield Error(id=tool_call_identifier, tool=tool_name, code="invalid_location", message=str(exception))
            return
        candidate = raw_target[len("file://"):] if lowered.startswith("file://") else raw_target
        resolved_path = await asyncio.to_thread(resolved_location.executor.resolve, resolved_location.base_directory, candidate)
        if not await asyncio.to_thread(resolved_location.executor.exists, resolved_path):
            yield Error(id=tool_call_identifier, tool=tool_name,
                code="artifact_file_not_found",
                message=f"No file to open at {resolved_path}. Write the file first, then open it.",
            )
            return
        kind = artifact_kind_for(resolved_path)
        if kind == "file":
            # A code or text file has no visual form, so opening it as an artifact would just
            # show an empty panel. The artifacts panel is a preview surface; keep it for
            # things that render.
            yield Error(id=tool_call_identifier, tool=tool_name,
                code="artifact_not_previewable",
                message=self._prompt_loader.load(
                    "artifact_not_previewable", {"file_name": Path(resolved_path).name},
                ).strip(),
            )
            return
        display_title = Path(resolved_path).name or resolved_path
        artifact_id = requested_artifact_id or self._artifact_surface_id(f"{resolved_location.uri}:{resolved_path}")
        self._capture_written_artifacts(
            resolved_location, changed_absolute_paths=[resolved_path],
            tool_call_id=tool_call_identifier, message=f"open_artifact {display_title}",
            surface={"surface_id": artifact_id, "kind": kind, "title": display_title, "source": resolved_path, "absolute_path": resolved_path},
        )
        result = build_open_artifact_result(
            artifact_id=artifact_id, kind=kind, title=display_title, source=resolved_path,
            location_uri=resolved_location.uri, absolute_path=resolved_path,
            height=requested_height,
        )
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result)


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
            # Attach the "don't poll/read_task this" guidance from a prompt
            # template, keeping user-facing wording out of the tool code.
            result_data["note"] = self._prompt_loader.load("web_search_started_note", {})
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result_data)


    async def _tool_read_task(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
        decision: _ResolvedToolDecision, policy: CallExecutionPolicy,
        resolved_location: ResolvedLocation | None,
    ) -> AsyncIterator[TurnEvent]:
        requested_task_id = tool_arguments.get("task_id", "")
        # A web_search/background-bash handle ("search-…"/"bg-…") is not an A2A
        # task — its results are delivered automatically, never read. Catch the
        # mistake with a redirect instead of a bare "task_not_found" that just
        # invites the model to retry the same wrong poll.
        background_kind = _background_handle_kind(requested_task_id)
        if self._task_reader is None:
            result = {"code": "read_task_unavailable", "message": "Reading tasks is not available in this context."}
        elif background_kind is not None:
            message = self._prompt_loader.load(
                "read_task_background_handle",
                {"task_id": requested_task_id, "kind": background_kind},
            )
            result = {"code": "not_a_readable_task", "task_id": requested_task_id, "message": message}
        else:
            task = await self._task_reader(requested_task_id)
            if task is None:
                result = {"code": "task_not_found", "task_id": requested_task_id}
            else:
                result = task
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result)


    @staticmethod
    def _surface_for(surface_name: str):
        """The live surface a screen tool names: the native macOS tree, or the user's Chrome."""
        from daisy.computer import engine as native_surface, web as web_surface

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
        from daisy.computer import control, retrieval
        from daisy.computer.surface import message_loader

        surface_name = str(tool_arguments.get("surface", "browser") or "browser")
        surface = self._surface_for(surface_name)
        app = str(tool_arguments.get("app", ""))
        script = str(tool_arguments.get("script", ""))
        if not script.strip():
            yield ToolResult(id=tool_call_identifier, name=tool_name, result={"ok": False, "error": "control_screen needs a script to run."})
            return
        gate = surface.preflight("documents")
        if gate is not None:
            yield ToolResult(id=tool_call_identifier, name=tool_name, result=gate)
            return

        control_message = message_loader("control")
        known_ids: dict[str, dict[str, str]] = {}   # id -> {id, name, role, context}, from find_* results
        acted_on: list[dict[str, str]] = []
        mutating_verbs = frozenset({"click", "type", "choose", "upload", "drag"})
        targeting_verbs = mutating_verbs | frozenset({"read", "hover", "scroll"})

        def _rank(query: str, limit: int, everything: bool) -> list:
            raw = surface.documents(app) if surface_name == "computer" else surface.documents()
            if not raw.get("ok"):
                raise RuntimeError(raw.get("error", "Could not read the screen."))
            return retrieval.Index(raw.get("documents", [])).search(query, top_k=limit, everything=everything)

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
            lines = []
            for record in records:
                parts = [f"id={record.get('id')}"]
                for field in ("name", "role", "context"):
                    if record.get(field):
                        parts.append(f"{field}={record[field]!r}")
                lines.append("  - " + ", ".join(parts))
            return "\n".join(lines)

        def find_many(query: Any, limit: int = 8, all: bool = False, **_: Any) -> list:
            records = [_record(hit) for hit in _rank(str(query), int(limit), bool(all))]
            for record in records:
                _register(record)
            return records

        def find_one(query: Any, role: str = "", name: str = "", context: str = "", **_: Any) -> dict:
            scored = [(_record(hit), float(hit.score or 0.0)) for hit in _rank(str(query), 8, False)]
            if role or name or context:
                scored = [
                    (record, score) for record, score in scored
                    if (not role or record.get("role", "") == role)
                    and (not name or record.get("name", "") == name)
                    and (not context or context in (record.get("context", "") or ""))
                ]
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
            if verb in mutating_verbs:
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
            outcome = await asyncio.to_thread(surface.perform, name, list(args), keywords)
            if isinstance(outcome, dict):
                if outcome.get("ok") is False:
                    # Surface a primitive failure into the script as a raised error it can try/except.
                    raise RuntimeError(outcome.get("error", f"{name} failed"))
                if name in mutating_verbs and args and isinstance(args[0], str):
                    acted_on.append({"action": name, **known_ids.get(args[0], {"id": args[0]})})
                # Hand the script the useful value directly: evaluate's result or read's text is the
                # value itself (structured and queryable), an action is its confirmation minus `ok`.
                if "result" in outcome:
                    return outcome["result"]
                if "text" in outcome:
                    return outcome["text"]
                return {key: value for key, value in outcome.items() if key != "ok"}
            return outcome

        result = await control.run_control_script(script, dispatch)
        if acted_on and isinstance(result, dict):
            result.setdefault("acted_on", acted_on)
        yield ToolResult(id=tool_call_identifier, name=tool_name, result=result)
