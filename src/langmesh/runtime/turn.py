"""The turn loop: the `stream()` driver, its phases, and how a turn's messages are assembled."""
from __future__ import annotations

import json
import logging
import os
from contextlib import suppress
from datetime import datetime, timezone
from langmesh.base import telemetry as _telemetry
from langmesh.base.identifiers import new_id
from langmesh.runtime.internals import (
    _CONTINUE,
    _detect_workspace,
    _ModelCallOutcome,
    _StepOutcome,
    _PreflightGate,
    _STOP,
    _STREAM_EXHAUSTED,
    _stream_next,
    _ToolPlan,
    _maybe_json,
    conversation_tokens,
)
from langmesh.runtime.prompt.environment import probe_local_environment, probe_user_context
from langmesh.protocol.events import TurnContext
from langmesh.base.instructions import instructions_payload
from langmesh.base.memories import memories_payload
from langmesh.base.message_content import message_content_deltas, message_text
from langmesh.base.model_errors import ContextWindowExceeded, over_context_window
from langchain_core.utils.json import parse_partial_json
from langmesh.base.skills import enabled_skills, skills_for_agent, skills_payload
from langmesh.base.confinement import Denial
from langmesh.runtime.turn_events import (
    Checkpoint,
    Done,
    RetryRequested,
    Steering,
    Suspended,
    SuspensionGate,
    Status,
    TextChunk,
    Thinking,
    ThinkingDone,
    ToolCall,
    TurnEvent,
)
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import add_ai_message_chunks
from pathlib import Path
from typing import Any, AsyncIterator, cast, Optional
import asyncio
import platform
import time
import uuid
from langmesh.base.serialization import compact


logger = logging.getLogger(__name__)

def _settled_arguments(parsed: dict, raw: str) -> dict:
    """The arguments whose values are final. A partial object's last key is the one still being written."""
    try:
        json.loads(raw)
    except ValueError:
        # Not closed yet, so its final key is mid-write: a value drawn now would be redrawn as it grows.
        return dict(list(parsed.items())[:-1])
    return parsed


class _RunsTurns:
    """The turn itself: what the model is told, what comes back, and when it is over."""

    #: The checklist for this turn, cleared as soon as the one request that carries it is built.
    _pending_checklist: Optional[HumanMessage] = None

    def _locations_summary(self) -> list[dict]:
        """The locations as the model sees them: the URI to pass, and enough to choose the right one."""
        return [
            {
                "location": resolved.uri,
                "name": resolved.name,
                "kind": resolved.kind,
                "base_directory": resolved.base_directory,
                "writable": resolved.is_remote or self.writes_anywhere,
            }
            for resolved in self._locations.values()
        ]

    def _build_static_system_prompt(self) -> str:
        """The static half of the system prompt, cached across calls, so every session shares one baseline."""
        if self._cached_system_prompt is None:
            all_skills = enabled_skills(list(self._catalogue.skills()))
            agent_skills = skills_for_agent(all_skills, self._agent_configuration.skills)
            memories = list(self._catalogue.memories())
            worktree_root, is_git_repo = _detect_workspace(self._working_directory)
            context_json = compact({
                "session": self._session_id,
                # Present only where another session created this one: somebody is waiting for an answer.
                **({"parent_session": self._parent_session} if self._parent_session else {}),
                "working_directory": self._working_directory,
                "project_directory": self._project_directory,
                "worktree_root": worktree_root,
                "is_git_repo": is_git_repo,
                "session_worktree_strategy": self._global_configuration.workspace.strategy,
                "platform": platform.system(),
                "today_date": datetime.now().strftime("%Y-%m-%d"),
                # `locations` is absent: it can change mid-session, and anything changeable rewrites every request.
            })
            # Conditional, since it asserts "you are running as a session", which a library runtime is not.
            parent_report = (
                self._prompt_loader.load("parent_report", {"parent": self._parent_session})
                if self._parent_session else ""
            )
            agent_context = (
                self._prompt_loader.load("agent_context", {"parent_report": parent_report})
                if "message_session" in {tool.name for tool in self._tools} else ""
            )
            # The user-context section is its own template, so it is simply absent when the setting is off.
            user_environment = ""
            if self._user_context_enabled():
                user_environment = self._prompt_loader.load("user_context", {})
            # The screen tools are opt-in, so their guidance enters the prompt only when they do.
            computer_control_guidance = ""
            if self._global_configuration.computer_control.enabled:
                computer_control_guidance = self._prompt_loader.load("computer_control_guidance", {})
            # Guidance for tools this session lacks is guidance to call something that is not there.
            toolbox = (
                self._prompt_loader.load("toolbox", {})
                if self._tool_context.toolbox is not None else ""
            )
            available = {tool.name for tool in self._tools}
            peer_sessions = (
                self._prompt_loader.load("peer_sessions", {})
                if "create_session" in available or "message_session" in available else ""
            )
            mcp_servers = (
                self._prompt_loader.load("mcp_servers", {})
                if "call_mcp_tool" in available else ""
            )
            # Whole documents, each laid out by its own template: metadata as JSON, the document as itself.
            instruction_files = "".join(
                self._prompt_loader.load("instruction_file", {
                    # Whatever the payload carries and nothing invented: `scope` is absent for a non-file document.
                    "metadata": compact({key: entry[key] for key in ("source", "scope") if key in entry}),
                    "content": entry["content"].strip(),
                })
                for entry in instructions_payload(self._catalogue.instructions())
            ).strip()
            instructions = (
                self._prompt_loader.load("instructions", {"files": instruction_files})
                if instruction_files else ""
            )
            # One statement of how to think, rendered into this prompt and the reviewer's, so they cannot drift.
            thinking_language = self._prompt_loader.load("thinking_language", {}).strip()
            self._cached_system_prompt = self._prompt_loader.load("system_prompt", {
                "system_prompt": self._system_prompt,
                "thinking_language": thinking_language,
                "context": context_json,
                "user_environment": user_environment,
                "instructions": instructions,
                "skills": compact(skills_payload(agent_skills)),
                "memories": compact(memories_payload(memories)),
                "agent_context": agent_context,
                "computer_control_guidance": computer_control_guidance,
                "toolbox": toolbox,
                "peer_sessions": peer_sessions,
                "mcp_servers": mcp_servers,
            }).strip()
        return self._cached_system_prompt

    def _user_context_enabled(self) -> bool:
        user_context = getattr(self._global_configuration, "user_context", None)
        return user_context is not None and bool(user_context.enabled)

    def _ensure_environment_note(self) -> None:
        """Put the machine snapshot in once, at the first message, since a machine does not change mid-conversation."""
        if any(message.additional_kwargs.get("environment_note") for message in self._conversation):
            return
        # Described with the `PATH` a tool child is given, not this process's, which no command runs in.
        snapshot = _maybe_json(probe_local_environment(self._child_path()))
        payload: dict[str, Any] = {"machine": snapshot if isinstance(snapshot, dict) else {}}
        if self._user_context_enabled():
            user_context = _maybe_json(probe_user_context(self._global_configuration.user_context.refresh_hours))
            if isinstance(user_context, dict) and user_context:
                payload["user_context"] = user_context
        note = self._reminder_message(compact(payload))
        note.additional_kwargs["environment_note"] = True
        self._conversation.append(note)


    def _append_turn_context(self) -> None:
        """Append this turn's context when it says something new, so the cached prefix keeps extending."""
        context = json.loads(self._build_dynamic_context())
        previous: dict[str, Any] = {}
        for message in reversed(self._conversation):
            recorded = message.additional_kwargs.get("turn_context")
            if isinstance(recorded, dict):
                previous = recorded
                break
        def without_the_clock(picture: dict[str, Any]) -> dict[str, Any]:
            return {key: value for key, value in picture.items() if key != "now"}

        if previous and without_the_clock(previous) == without_the_clock(context):
            return
        note = self._reminder_message(compact(context))
        note.additional_kwargs["turn_context"] = context
        self._conversation.append(note)

    def _build_dynamic_context(self) -> str:
        """The per-turn context: the time, the place, the goal, the tasks, the background work, the locations."""
        context = TurnContext(
            now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            pwd=self._working_directory or str(Path.cwd()),
            # The goal as the agent stated it, without the bookkeeping it would start pacing itself against.
            goal=self._goal.for_model() if self._goal is not None else {},
            tasks=self._task_manager.to_dict_list(),
            background={
                "running": self._background.active_by_context_key(),
                "active_count": self._background.active_count(),
                "recent_events": self._execution_history[-20:],
            },
            screen=self._screen_context(),
            locations=self._locations_summary(),
            confinement=self._confinement_summary(),
        )
        return context.model_dump_json(exclude_defaults=True)

    def _child_path(self) -> list[str]:
        """The `PATH` a tool child is actually given, split into entries."""
        from langmesh.base.confinement import child_environment

        environment = child_environment(self._sandbox, workspace=self._working_directory or "")
        environment.update(self._tool_context.child_environment(environment))
        return [entry for entry in environment.get("PATH", "").split(os.pathsep) if entry]

    def _confinement_summary(self) -> dict:
        """The boundary the operating system will enforce: the configured profile, plus the grants so far, apart."""
        profile = getattr(self._tool_context, "sandbox", None)
        if profile is None or self._global_configuration.sandbox.enforce == "off":
            return {}
        summary = profile.describe(workspace=self._working_directory or "")
        if self._access_grants:
            summary["granted"] = [grant.as_dict() for grant in self._access_grants]
        return summary

    def _screen_context(self) -> dict:
        """Every place a screen script can be pointed at, and what may be called in each."""
        if not self._global_configuration.computer_control.enabled:
            return {}
        try:
            from langmesh.computer import targets as target_registry
            from langmesh.computer.surface import message_loader

            # Skipped rather than waited on: enumerating costs ~1.8s the first time a process asks.
            if not target_registry.warm():
                return {"reading": message_loader("computer")("screen_warming")}

            from langmesh.computer import workflows

            # Every primitive is listed: what a script may call is decided per call, from what it asks to do.
            block = target_registry.context_block()
            # Saved workflows, read off the files without importing them, since this runs every turn.
            saved = workflows.available(self._project_directory or self._working_directory or "")
            if saved:
                block["workflows"] = saved
            return block
        except Exception:  # noqa: BLE001 — context is an aid, never the thing that fails a turn
            logger.debug("could not enumerate screen targets for the turn context", exc_info=True)
            return {}

    def _record_turn(self, user_message: str, tool_calls: list, tool_results: list, final_response: str):
        self._record_message("human", user_message)
        for tool_call_entry in tool_calls:
            self._record_message("tool", compact(tool_call_entry.get("args", {})), tool_call_entry.get("name", ""))
        for tool_result_entry in tool_results:
            self._record_message("tool", str(tool_result_entry.get("result", "")), tool_result_entry.get("name", ""))
        if final_response:
            self._record_event("assistant_response_completed", {
                "content_characters": len(final_response),
                "tool_call_count": len(tool_calls),
                "tool_result_count": len(tool_results),
            })
        self._record_message("ai", final_response)

    async def _record_transcript_turn(
        self, request: str, response: str, outcome: str, tool_calls: list, error: str = "",
    ) -> None:
        """Hand one completed turn to the caller's transcript: one entry per turn, not per message."""
        if self._transcript is None:
            return
        from langmesh.base.ports import TurnSummary

        usage = self._token_usage
        try:
            await self._transcript.record(TurnSummary(
                session_id=self._session_id,
                turn_id=new_id("turn"),
                started_at=self._turn_started_at or datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
                request=request,
                response=response,
                outcome=outcome,
                tools_called=tuple(entry.get("name", "") for entry in tool_calls),
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                error=error,
            ))
        except Exception:  # noqa: BLE001 — a record that cannot be written must not lose the turn
            logger.warning("the transcript raised while recording a turn", exc_info=True)

    async def _drain_steering_messages(self) -> list[TurnEvent]:
        events: list[TurnEvent] = []
        while not self._steering_messages.empty():
            message, message_id, peer_sender = self._steering_messages.get_nowait()
            self._conversation.append(HumanMessage(content=message))
            events.append(Steering(text=message, message_id=message_id, peer_sender=peer_sender))
        if self._steering_messages.empty():
            self._steering_available.clear()
        return events

    async def _answer_gates(self, gates: list[SuspensionGate]) -> dict[str, Any]:
        """Ask the caller's `Approvals` about each gate. Without one, every gate suspends, as it always has."""
        if self._approvals is None or not gates:
            return {}
        answers: dict[str, Any] = {}
        for gate in gates:
            try:
                verdict = await self._approvals.decide(gate)
            except Exception:  # noqa: BLE001 — a failing approver escalates, it does not allow
                logger.warning("the approver raised on %s; escalating the gate", gate.kind, exc_info=True)
                continue
            if verdict is None:
                continue
            if gate.kind == "question":
                answers[gate.request_id] = verdict.answers if verdict.allow else None
            else:
                answers[gate.request_id] = "allow" if verdict.allow else "deny"
        return answers

    def _reminder_message(
        self, content: str, image_blocks: list[dict] | None = None, transient: bool = False,
        marks: dict[str, Any] | None = None,
    ) -> HumanMessage:
        """Something neither party said, as a user-role message, which keeps the conversation append-only."""
        text = self._prompt_loader.load("reminder", {"content": content.strip()}).strip()
        # `transient` marks a note assembled for one request and never appended, so no cache breakpoint sits on it.
        tags = {"reminder": True, **({"transient": True} if transient else {}), **(marks or {})}
        if image_blocks:
            return HumanMessage(content=[{"type": "text", "text": text}, *image_blocks], additional_kwargs=tags)
        return HumanMessage(content=text, additional_kwargs=tags)

    def _invalid_tool_call_content(self, invalid: dict) -> str:
        """The message for a malformed tool call, from a template, so the wording stays out of the code."""
        return self._prompt_loader.load("invalid_tool_call", {
            "name": invalid.get("name") or "unknown",
            "error": invalid.get("error") or "arguments could not be parsed",
        })

    def _close_dangling_tool_calls(self) -> None:
        """Close tool calls left dangling by a suspended turn that a new message superseded, so history stays valid."""
        if not self._conversation:
            return
        last = self._conversation[-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            for tool_call in last.tool_calls:
                self._conversation.append(ToolMessage(
                    content="(superseded: a new message was sent before this was answered)",
                    tool_call_id=tool_call["id"],
                ))

    async def resume_stream(
        self, plans: dict[str, dict], answers: dict[str, Any]
    ) -> AsyncIterator[TurnEvent]:
        """Resume a suspended turn: run its pending batch with the recorded answers, then continue normally."""
        async for event in self.stream("", resume_plans=plans, resume_answers=answers):
            yield event

    async def stream(
        self, user_message: str | list, as_system_note: bool = False,
        resume_plans: Optional[dict[str, dict]] = None,
        resume_answers: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[TurnEvent]:
        self._abort_event.clear()
        # A turn runs until the model is done or the user interrupts: an unfinished goal outlives this loop.
        turn_tool_calls_log: list[dict] = []
        turn_tool_results_log: list[dict] = []

        if resume_plans is not None:
            # Resume: the checkpoint is already at the tail, so run its batch and fall into the loop.
            recorded_user_message = ""
            response = self._conversation[-1] if self._conversation else None
            if response is None or not getattr(response, "tool_calls", None):
                yield Done(text="", stop_reason="completed")
                return
            resolved = self._resolve_tool_decisions(
                {tool_call_id: _ToolPlan.from_dict(plan) for tool_call_id, plan in resume_plans.items()},
                resume_answers or {},
            )
            resume_outcomes: dict[str, dict] = {}
            async for event in self._drain_tools_concurrently(
                cast(list[dict], response.tool_calls), turn_tool_calls_log, turn_tool_results_log, resume_outcomes, resolved,
            ):
                yield event
            self._append_tool_results(response, resume_outcomes)
            yield Checkpoint()
        else:
            # A superseded suspension leaves dangling calls; close them so appending this turn stays valid.
            self._ensure_environment_note()
            self._close_dangling_tool_calls()
            # Usually prose, but an attachment turn carries a content list so a vision model sees the pixels.
            turn_message = (
                self._reminder_message(user_message)
                if as_system_note and isinstance(user_message, str)
                else HumanMessage(content=user_message)
            )
            self._conversation.append(turn_message)
            # Only when the user speaks: a tool-result hop is the same instruction, and repeating it buys nothing.
            self._pending_checklist = self._reminder_message(
                self._prompt_loader.load("turn_checklist", {}), transient=True,
            )
            # The event-log recorder only wants prose from LangChain's standard blocks.
            recorded_user_message = message_text(turn_message)
        # After the turn's message, so the freshest picture is read last, and once per turn rather than per hop.
        self._append_turn_context()
        self._turn_started_at = datetime.now(timezone.utc)

        while True:
            if self._abort_event.is_set():
                if self._has_queued_steering():
                    self._abort_event.clear()
                    for steering_event in await self._drain_steering_messages():
                        yield steering_event
                    continue
                yield Done(text="", stop_reason="cancelled")
                return

            background_events = self._background_result_events()
            if background_events:
                for background_event in background_events:
                    yield background_event
                continue

            # Background work no longer holds the turn open: the resume pump wakes the agent when it lands.
            for steering_event in await self._drain_steering_messages():
                yield steering_event

            # Folding is the whole of keeping inside the window: an unbounded head becomes a fixed-size log.
            if self._should_compact():
                async for compaction_event in self.compact(reason="auto"):
                    yield compaction_event

            messages = self._build_turn_messages()

            # The model call: yields the stream and hands back the assembled response, or a terminal condition.
            call = _ModelCallOutcome()
            try:
                async for event in self._stream_model_call(messages, call):
                    yield event
            except ContextWindowExceeded as overflow:
                # A refusal is itself a measurement, so the indicator stops reporting the last successful call's reading.
                window = overflow.context_window or self._context_window
                if window > 0:
                    self._context_window = window
                    self._latest_context_tokens = max(self._latest_context_tokens,
                                                      overflow.tokens or window)
                raise
            if call.cancelled:
                return
            if call.aborted_for_steering:
                for steering_event in await self._drain_steering_messages():
                    yield steering_event
                continue
            response = call.response

            usage_event = self._accumulate_usage(response)
            if usage_event is not None:
                yield usage_event

            # LangChain serializes invalid tool calls as `tool_calls`, so each must still get a tool message.
            for invalid in response.invalid_tool_calls:
                if not invalid.get("id"):
                    invalid["id"] = f"call_invalid_{uuid.uuid4().hex[:24]}"

            # Nothing to call: retry a malformed batch, answer agents, or finish the turn.
            if not response.tool_calls:
                step = _StepOutcome()
                async for event in self._finalize_no_tool_calls(
                    response, recorded_user_message, turn_tool_calls_log, turn_tool_results_log, step,
                ):
                    yield event
                if step.directive == _STOP:
                    return
                continue

            # Calls to make: run the batch behind its checkpoint, then honour a Stop that landed during it.
            step = _StepOutcome()
            async for event in self._run_tool_batch(
                response, recorded_user_message, turn_tool_calls_log, turn_tool_results_log, step,
            ):
                yield event
            if step.directive == _STOP:
                return
            if step.directive == _CONTINUE:
                continue

            for steering_event in await self._drain_steering_messages():
                yield steering_event

    def _build_turn_messages(self) -> list:
        """This iteration's messages: the static prompt, the conversation, and the turn context appended once."""
        messages = [SystemMessage(content=self._build_static_system_prompt())] + self._conversation
        if self._pending_checklist is None:
            return messages
        # Last, and only ever last: it never joins the conversation, so anywhere else its absence next time rewrites the prefix.
        checklist = self._pending_checklist
        self._pending_checklist = None
        return messages + [checklist]

    def _refuse_if_over_window(self, messages: list) -> None:
        """Refuse a request that cannot fit before sending it, with numbers, since the harness knows the window."""
        window = self._context_window
        if window <= 0:
            return                              # the catalogue is cold; it says nothing about room
        tokens = conversation_tokens(messages)
        if not over_context_window(tokens, window):
            return
        # Recorded, so the indicator agrees with the refusal rather than the last call that succeeded.
        self._latest_context_tokens = tokens
        raise ContextWindowExceeded(
            "The assembled request is larger than this model's context window.",
            model=self.effective_model_identifier, context_window=window, tokens=tokens,
        )

    async def _stream_model_call(
        self, messages: list, outcome: _ModelCallOutcome
    ) -> AsyncIterator[TurnEvent]:
        """One streamed model call, writing the assembled response or a terminal condition into ``outcome``."""
        yield Thinking()
        thinking_started_at = time.monotonic()
        thinking_done_emitted = False
        response_chunks: list[AIMessageChunk] = []
        # Calls already announced from the stream, so the completed message does not announce them twice.
        announced_tool_calls: set[str] = set()
        # What each call looks like so far: its id by stream index, its name, its raw argument text, and what was shown.
        streaming_call_ids: dict[Any, str] = {}
        streaming_call_names: dict[str, str] = {}
        streaming_call_args: dict[str, str] = {}
        streaming_call_shown: dict[str, dict] = {}
        # When each call last redrew, so a fast provider cannot redraw a row faster than a screen can show it.
        streaming_call_drawn_at: dict[str, float] = {}
        aborted_for_steering = False
        # A generation span, started rather than made current so it is safe to hold across yields.
        generation_span = _telemetry.start_span(
            "gen_ai.generation", {"gen_ai.request.model": self.effective_model_identifier}
        )
        # A hook may read the conversation about to leave the process, and may change it.
        if not self._hooks.empty:
            messages = await self._hooks.before_model(messages)
        self._refuse_if_over_window(messages)
        # Said out loud, because the wait that follows is the provider's and the turn would otherwise look idle.
        yield Status(code="awaiting_model")
        model_stream = self._bound_model.astream(messages)
        abort_waiter = asyncio.ensure_future(self._abort_event.wait())
        try:
            while True:
                chunk_future = asyncio.ensure_future(_stream_next(model_stream))
                await asyncio.wait(
                    {chunk_future, abort_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                if self._abort_event.is_set():
                    # Stop won the race: drop the pending read and stop consuming the stream.
                    chunk_future.cancel()
                    with suppress(BaseException):
                        await chunk_future
                    if self._has_queued_steering():
                        self._abort_event.clear()
                        aborted_for_steering = True
                        break
                    yield Done(text="", stop_reason="cancelled")
                    outcome.cancelled = True
                    return
                chunk = chunk_future.result()
                if chunk is _STREAM_EXHAUSTED:
                    break
                response_chunks.append(chunk)
                # A call is announced the moment the model names it, and again as each argument finishes:
                # writing a large one is seconds, and the turn would otherwise show nothing for all of them.
                for named in getattr(chunk, "tool_call_chunks", None) or []:
                    identifier = (named or {}).get("id") or streaming_call_ids.get((named or {}).get("index"))
                    name = (named or {}).get("name") or streaming_call_names.get(identifier, "")
                    if not identifier:
                        continue
                    streaming_call_ids[(named or {}).get("index")] = identifier
                    if name:
                        streaming_call_names[identifier] = name
                    if name and identifier not in announced_tool_calls:
                        announced_tool_calls.add(identifier)
                        if not thinking_done_emitted:
                            thinking_done_emitted = True
                            yield ThinkingDone(duration_ms=int((time.monotonic() - thinking_started_at) * 1000))
                        yield ToolCall(name=name, arguments=None, id=identifier)
                    fragment = (named or {}).get("args") or ""
                    if not fragment or identifier not in announced_tool_calls:
                        continue
                    streaming_call_args[identifier] = streaming_call_args.get(identifier, "") + fragment
                    settled = parse_partial_json(streaming_call_args[identifier]) or {}
                    if isinstance(settled, dict):
                        # Only what has finished arriving, so no argument is ever drawn while it is still growing.
                        settled = _settled_arguments(settled, streaming_call_args[identifier])
                    if isinstance(settled, dict) and settled and settled != streaming_call_shown.get(identifier):
                        # Redrawn on the same clock the text uses, since both end as one row on one screen.
                        drawn = time.monotonic()
                        if drawn - streaming_call_drawn_at.get(identifier, 0.0) < 0.016667:
                            continue
                        streaming_call_drawn_at[identifier] = drawn
                        streaming_call_shown[identifier] = settled
                        yield ToolCall(name=streaming_call_names.get(identifier, ""), arguments=settled, id=identifier)
                for content_delta in message_content_deltas(chunk):
                    if content_delta.kind == "text":
                        if not thinking_done_emitted:
                            thinking_done_emitted = True
                            yield ThinkingDone(duration_ms=int((time.monotonic() - thinking_started_at) * 1000),
                            )
                        yield TextChunk(text=content_delta.text,
                            block_id=content_delta.block_identifier,
                        )
                    else:
                        yield Thinking(text=content_delta.text,
                            block_id=content_delta.block_identifier,
                        )
        finally:
            _telemetry.end_span(generation_span)
            abort_waiter.cancel()
            # Close the HTTP stream, so an aborted turn never leaks a provider connection.
            with suppress(BaseException):
                stream_closer = getattr(model_stream, "aclose", None)
                if stream_closer is not None:
                    await stream_closer()
        # A tool-only turn produces no answer text, so close the phase here.
        if not thinking_done_emitted:
            yield ThinkingDone(duration_ms=int((time.monotonic() - thinking_started_at) * 1000),
            )
        if aborted_for_steering:
            outcome.aborted_for_steering = True
            return
        outcome.response = add_ai_message_chunks(response_chunks[0], *response_chunks[1:]) if response_chunks else AIMessageChunk(content="")

    async def _finalize_no_tool_calls(
        self, response: AIMessageChunk, recorded_user_message: str,
        turn_tool_calls_log: list[dict], turn_tool_results_log: list[dict], step: _StepOutcome,
    ) -> AsyncIterator[TurnEvent]:
        """Handle a response with no tool calls: retry a malformed batch, deliver agent messages, or finish."""
        if response.invalid_tool_calls:
            # Only malformed calls: a ToolMessage would be orphaned, so they are corrected by a reminder.
            if response.content:
                self._conversation.append(response)
            for invalid in response.invalid_tool_calls:
                self._conversation.append(self._reminder_message(
                    self._invalid_tool_call_content(cast(dict, invalid)),
                ))
            step.directive = _CONTINUE
            return

        # No tool calls, so the turn ends: the resume pump wakes the agent when the next result lands.
        final_text = message_text(response)
        self._conversation.append(response)
        steering_events = await self._drain_steering_messages()
        if steering_events:
            for steering_event in steering_events:
                yield steering_event
            step.directive = _CONTINUE
            return
        # An unfinished goal does not hold the turn open: the goal is durable, and a later turn picks it up.
        self._record_turn(
            recorded_user_message, turn_tool_calls_log,
            turn_tool_results_log, final_text,
        )
        await self._record_transcript_turn(
            recorded_user_message, final_text, "completed", turn_tool_calls_log,
        )
        yield Done(text=final_text, stop_reason="completed")
        step.directive = _STOP

    async def _rerun_answered(
        self, tool_calls, turn_tool_calls_log, turn_tool_results_log, outcomes, decisions,
    ):
        """Run the calls a retry answer settled, once the rest of the batch has finished."""
        pending = [
            call for call in tool_calls
            if (decision := decisions.get(call["id"])) is not None and decision.completed is None
        ]
        if not pending:
            return
        async for event in self._drain_tools_concurrently(
            pending, turn_tool_calls_log, turn_tool_results_log, outcomes, decisions,
        ):
            yield event

    async def _run_tool_batch(
        self, response: AIMessageChunk, recorded_user_message: str,
        turn_tool_calls_log: list[dict], turn_tool_results_log: list[dict], step: _StepOutcome,
    ) -> AsyncIterator[TurnEvent]:
        """Run the tool batch behind a durable checkpoint, with every permission resolved before any tool runs."""
        self._conversation.append(response)
        tool_calls = cast(list[dict], response.tool_calls)
        outcomes: dict[str, dict] = {}
        if not self._abort_event.is_set():
            plans, pending = await self._preflight_permissions(tool_calls)
            gates = [SuspensionGate(**gate.to_dict()) for gate in pending]
            answered = await self._answer_gates(gates)
            if gates and len(answered) < len(gates):
                # One suspend event per turn, and a durable pause, so a session waiting on a person survives a restart.
                yield Suspended(
                    interactions=[gate for gate in gates if gate.request_id not in answered],
                    plans={tool_call_id: plan.to_dict() for tool_call_id, plan in plans.items()},
                )
                step.directive = _STOP
                return
            else:
                decisions = self._resolve_tool_decisions(plans, answered)
            # After the barrier: a hook sees only what the rules approved, so it can drop calls but never add one.
            if not self._hooks.empty:
                tool_calls = await self._hooks.before_tools(tool_calls)
            retries: list[_PreflightGate] = []
            async for event in self._drain_tools_concurrently(
                tool_calls, turn_tool_calls_log, turn_tool_results_log, outcomes, decisions,
            ):
                if isinstance(event, RetryRequested):
                    # Never yielded onward: a client draws its prompt from the suspension below.
                    gate = self.retry_gate(
                        tool_call_id=event.id, command=event.command,
                        denial=Denial(kind=event.denial_kind, evidence=event.denial_evidence),
                        explanation=event.explanation,
                    )
                    gate.refused_result = event.result
                    gate.tool_name = "bash"
                    gate.arguments = {"command": event.command, "explanation": event.explanation}
                    retries.append(gate)
                    continue
                yield event
            if retries:
                # Some of the batch ran: completed calls carry their result so the resumed batch replays rather than re-runs.
                for tool_call_id, plan in plans.items():
                    plan.gates = []
                    plan.refusal = None
                    plan.completed = (
                        {"result": outcomes[tool_call_id].get("content")}
                        if tool_call_id in outcomes else None
                    )
                for gate in retries:
                    plan = plans[gate.tool_call_id]
                    plan.completed = None
                    plan.gates = [gate]
                gates = [SuspensionGate(**gate.to_dict()) for gate in retries]
                answered = await self._answer_gates(gates)
                if len(answered) < len(gates):
                    yield Suspended(
                        interactions=[gate for gate in gates if gate.request_id not in answered],
                        plans={tool_call_id: plan.to_dict() for tool_call_id, plan in plans.items()},
                    )
                    step.directive = _STOP
                    return
                async for event in self._rerun_answered(
                    tool_calls, turn_tool_calls_log, turn_tool_results_log, outcomes,
                    self._resolve_tool_decisions(plans, answered),
                ):
                    yield event
        self._append_tool_results(response, outcomes)
        yield Checkpoint()

        if self._abort_event.is_set():
            if self._has_queued_steering():
                self._abort_event.clear()
                for steering_event in await self._drain_steering_messages():
                    yield steering_event
                step.directive = _CONTINUE
                return
            self._record_turn(recorded_user_message, turn_tool_calls_log, turn_tool_results_log, "")
            yield Done(text="", stop_reason="cancelled")
            step.directive = _STOP
