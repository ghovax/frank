"""The AgentRuntime turn-loop concern (a mixin composed into AgentRuntime).

The ``stream()`` driver and its phases — the model call, the no-tool-calls finalize (goal nudge,
agent messaging, completion), and the tool batch — plus turn-message assembly, the static/dynamic
system prompt, steering drain, and turn recording."""
from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from datetime import timezone
from xeac.base import telemetry as _telemetry
from xeac.runtime.internals import _CONTINUE
from xeac.runtime.internals import _ModelCallOutcome
from xeac.runtime.internals import _PhaseStep
from xeac.runtime.internals import _STOP
from xeac.runtime.internals import _STREAM_EXHAUSTED
from xeac.runtime.internals import _ToolPlan
from xeac.runtime.internals import _detect_workspace
from xeac.runtime.internals import _stream_next
from xeac.runtime.prompt.environment import probe_local_environment
from xeac.runtime.prompt.environment import probe_user_context
from xeac.protocol.events import TurnContext
from xeac.runtime.prompt.instructions import load_instructions
from xeac.runtime.prompt.memories import load_memories
from xeac.runtime.prompt.memories import memories_payload
from xeac.base.message_content import message_content_deltas
from xeac.base.message_content import message_text
from xeac.base.skills import enabled_skills
from xeac.base.skills import load_skills
from xeac.base.skills import skills_for_agent
from xeac.base.skills import skills_payload
from xeac.runtime.turn_events import Checkpoint
from xeac.runtime.turn_events import Done
from xeac.runtime.turn_events import Status
from xeac.runtime.turn_events import Steering
from xeac.runtime.turn_events import Suspended
from xeac.runtime.turn_events import SuspensionGate
from xeac.runtime.turn_events import TextChunk
from xeac.runtime.turn_events import Thinking
from xeac.runtime.turn_events import ThinkingDone
from xeac.runtime.turn_events import TurnEvent
from langchain_core.messages import AIMessage
from langchain_core.messages import AIMessageChunk
from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from langchain_core.messages import ToolMessage
from langchain_core.messages.ai import add_ai_message_chunks
from pathlib import Path
from typing import Any
from typing import AsyncIterator
from typing import Optional
from typing import cast
import asyncio
import platform
import time
import uuid
from xeac.base.serialization import compact




class _TurnLoopMixin:

    def _locations_summary(self) -> list[dict]:
        """The project's locations as the model sees them: the `location` URI to pass,
        plus name/kind/base_directory/permission so it can choose the right one per tool call."""
        return [
            {
                "location": resolved.uri,
                "name": resolved.name,
                "kind": resolved.kind,
                "base_directory": resolved.base_directory,
                "permission_mode": resolved.permission_mode,
            }
            for resolved in self._locations.values()
        ]

    def _build_static_system_prompt(self) -> str:
        """Build the static portion of the system prompt (cached across calls).

        Every session is built through this same path, so they share the baseline prompt, the
        working-directory context, and the awareness of their own skills.

        What a session is deliberately *not* told is which other agent profiles exist. An
        agent is independent: it is defined by its own profile and nothing else, and a roster
        of its siblings would both couple them together and invite it to hand work to one it
        was never asked to involve. A caller that wants a peer names the profile.
        """
        if self._cached_system_prompt is None:
            all_skills = enabled_skills(load_skills(self._global_configuration.skill_directories_for(self._project_directory)))
            agent_skills = skills_for_agent(all_skills, self._agent_configuration.skills)
            memories = load_memories(self._global_configuration.memory_directories_for(self._project_directory))
            workspace_root, is_git_repo = _detect_workspace(self._working_directory)
            context_json = compact({
                "working_directory": self._working_directory,
                "project_directory": self._project_directory,
                "workspace_root": workspace_root,
                "is_git_repo": is_git_repo,
                "session_workspace_strategy": self._global_configuration.workspace.strategy,
                "platform": platform.system(),
                "today_date": datetime.now().strftime("%Y-%m-%d"),
                # The project's locations. Filesystem/shell tools take a `location` (its
                # URI); it is required when there is more than one, optional when one.
                "locations": self._locations_summary(),
            })
            agent_context = self._prompt_loader.load("agent_context", {})
            # The opt-in user-context section is its own template, rendered into the prompt's
            # `user_environment` slot only when enabled and the probe found something — so the
            # section (heading and all) simply is not there when off.
            user_environment = ""
            user_context = getattr(self._global_configuration, "user_context", None)
            if user_context is not None and user_context.enabled:
                user_context_snapshot = probe_user_context()
                if user_context_snapshot not in ("", "{}"):
                    user_environment = self._prompt_loader.load(
                        "user_context", {"user_context_snapshot": user_context_snapshot}
                    )
            # The computer/browser tools are opt-in, so their guidance (what each is for, and
            # to pick the right one rather than force one) only enters the prompt when they do.
            computer_control_guidance = ""
            if self._global_configuration.computer_control.enabled:
                computer_control_guidance = self._prompt_loader.load("computer_control_guidance", {})
            self._cached_system_prompt = self._prompt_loader.load("system_prompt", {
                "system_prompt": self._system_prompt,
                "context": context_json,
                "system_environment": probe_local_environment(),
                "user_environment": user_environment,
                "instructions": load_instructions(self._project_directory),
                "skills": compact(skills_payload(agent_skills)),
                "memories": compact(memories_payload(memories)),
                "agent_context": agent_context,
                "computer_control_guidance": computer_control_guidance,
            })
        return self._cached_system_prompt

    def _build_dynamic_context(self) -> str:
        """The structured per-turn context injected at the end of the message list: the current
        time, where the agent is, its goal, its tasks, and its background work. Empty goal/tasks
        are omitted so the model isn't fed noise. Standing behavioural guidance lives once in the
        system prompt, not re-injected here."""
        context = TurnContext(
            now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            pwd=self._working_directory or str(Path.cwd()),
            active_goal=self._active_goal,
            tasks=self._task_manager.to_dict_list(),
            background={
                "running": self._background.active_by_context_key(),
                "active_count": self._background.active_count(),
                "recent_events": self._execution_history[-20:],
            },
        )
        return context.model_dump_json(exclude_defaults=True)

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

    async def _drain_steering_messages(self) -> list[TurnEvent]:
        events: list[TurnEvent] = []
        while not self._steering_messages.empty():
            message = self._steering_messages.get_nowait()
            self._conversation.append(HumanMessage(content=message))
            events.append(Steering(text=message))
        if self._steering_messages.empty():
            self._steering_available.clear()
        return events

    def _harness_note_message(self, content: str, image_blocks: list[dict] | None = None) -> HumanMessage:
        """Wrap a harness-injected note in a user-role message carrying a
        ``<systemReminder>`` block.

        The role is deliberate — this is what keeps the conversation strictly
        append-only for the provider's prompt cache. A mid-conversation
        ``role:"system"`` message is HOISTED by LiteLLM into Anthropic's top-level
        ``system`` parameter, which renders before the entire message history; every
        such note therefore rewrites the prompt prefix and invalidates the cache for
        the whole conversation. A user-role note stays exactly where it was appended,
        so the prefix never changes — only grows — on every provider. The wrapper
        itself lives in the ``harness_note`` prompt template (wording stays in
        files, not code); it tells the model this is authoritative harness
        guidance, not user input (see the Harness Guidance section of the system
        prompt). The ``harness_note`` marker keeps these notes from counting as
        user turns in the compaction boundary. ``image_blocks`` (OpenAI-shaped
        ``image_url`` blocks) turn the note multimodal — the user role is the one
        role every provider accepts images on, which is how a read image reaches
        a vision model."""
        text = self._prompt_loader.load("harness_note", {"content": content.strip()}).strip()
        if image_blocks:
            return HumanMessage(
                content=[{"type": "text", "text": text}, *image_blocks],
                additional_kwargs={"harness_note": True},
            )
        return HumanMessage(content=text, additional_kwargs={"harness_note": True})

    def _invalid_tool_call_content(self, invalid: dict) -> str:
        """Build the message for a malformed tool call — used both as the tool
        result the model sees and the error surfaced to the user. The wording
        lives in the loaded ``invalid_tool_call`` prompt template so it stays
        out of code; a missing template degrades to an empty string, which still
        pairs the tool_call_id with a (blank) tool message and keeps the
        conversation valid."""
        return self._prompt_loader.load("invalid_tool_call", {
            "name": invalid.get("name") or "unknown",
            "error": invalid.get("error") or "arguments could not be parsed",
        })

    def artifact_render_error_note(self, payload: str) -> str:
        """Frame an artifact render failure as a behind-the-scenes self-realization
        note (injected as a harness note, not user input) the model repairs. The
        raw error rides along as its JSON payload, intact."""
        return self._prompt_loader.load("artifact_render_error", {"payload": payload})

    def _close_dangling_tool_calls(self) -> None:
        """If the conversation ends with a tool-call AIMessage that has no ToolMessages —
        a turn that suspended at input-required and was superseded by a new message rather
        than answered — append a ToolMessage for each call so the history stays valid.
        A later answer for that superseded pause then finds no checkpoint and is a no-op."""
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
        """Resume a durably-suspended turn. The conversation was rebuilt from the DB and
        ends with the pending tool-call AIMessage (the checkpoint); ``plans`` are the
        persisted preflight plans and ``answers`` the human decisions keyed by request id.
        Runs the pending batch with those decisions, then continues the turn normally —
        into the next model call, or a fresh suspension if it needs another decision."""
        async for event in self.stream("", resume_plans=plans, resume_answers=answers):
            yield event

    async def stream(
        self, user_message: str | list, as_system_note: bool = False,
        resume_plans: Optional[dict[str, dict]] = None,
        resume_answers: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[TurnEvent]:
        self._abort_event.clear()
        # The turn runs until the model is done or the user interrupts it — there is no
        # iteration count and no stuck-detector. The goal reconsideration flag lets an active
        # goal nudge the model once each time it stops, without a nudge counter; it is instance
        # state so the no-tool-calls phase can advance it across iterations. Dynamic per-turn
        # context is injected on the first model call only, tracked by a local below.
        self._awaiting_goal_reconsideration = False
        first_turn_message = True

        turn_tool_calls_log: list[dict] = []
        turn_tool_results_log: list[dict] = []

        if resume_plans is not None:
            # Resume: the checkpoint AIMessage is already at the tail of the rebuilt
            # conversation. Run its batch with the resolved decisions, then fall into the
            # loop for the next model call. No new user message is appended.
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
            # A prior turn may have suspended at input-required and been superseded by
            # this new message instead of answered. Close its dangling tool calls (an
            # AIMessage carrying tool_calls with no ToolMessages) so appending this turn
            # keeps the conversation valid for the provider.
            self._close_dangling_tool_calls()
            # A turn's input is usually plain text, but an attachment turn carries a
            # multimodal content list (a text block plus one image_url block per
            # attached image) so a vision model actually sees the pixels. LangChain's
            # HumanMessage accepts either, and the model adapter passes the content
            # straight through to the provider. A self-realization note (e.g. an artifact
            # render error) enters as a <systemReminder> harness note so the model
            # treats it as its own observation, not as something the user said — in a
            # user-role message so the append stays cache-safe (_harness_note_message).
            turn_message = (
                self._harness_note_message(user_message)
                if as_system_note and isinstance(user_message, str)
                else HumanMessage(content=user_message)
            )
            self._conversation.append(turn_message)
            # The event-log recorder only wants prose from LangChain's standard blocks.
            recorded_user_message = message_text(turn_message)

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

            # In-flight background work no longer holds the turn open. Completed
            # results are drained above and delivered mid-turn while the model is
            # still working; if the model goes idle with work still pending, the turn
            # simply ends and the executor's resume pump wakes the agent with an
            # autonomous turn the moment the next result lands.
            for steering_event in await self._drain_steering_messages():
                yield steering_event

            # Auto-compaction: if the last call left the context near the window,
            # summarize the older history before making another call that could
            # overflow. The reserved buffer guarantees room to run the compaction
            # itself; compact() resets the occupancy so this cannot re-fire in a loop.
            if self._should_compact():
                async for compaction_event in self.compact(reason="auto"):
                    yield compaction_event

            messages = self._build_turn_messages(first_turn_message)
            first_turn_message = False

            # Phase 1 — the model call. Yields the thinking/answer stream and hands back
            # the assembled response, or a terminal (cancelled) / steering condition.
            call = _ModelCallOutcome()
            async for event in self._stream_model_call(messages, call):
                yield event
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

            # Malformed tool calls (arguments that failed JSON parsing) land in
            # `invalid_tool_calls` while `tool_calls` may be empty. LangChain still
            # serializes invalid_tool_calls into the API payload as `tool_calls`, so each
            # one MUST be followed by a tool message — otherwise the next provider call
            # fails with "insufficient tool messages following tool_calls". Ensure every
            # invalid call carries an id that matches the ToolMessage appended for it.
            for invalid in response.invalid_tool_calls:
                if not invalid.get("id"):
                    invalid["id"] = f"call_invalid_{uuid.uuid4().hex[:24]}"

            # Phase 2 — no tool calls: retry a malformed batch, answer or await agents,
            # nudge an active goal, or finish the turn. Always ends the iteration
            # (_CONTINUE to loop again, _STOP once a terminal event was yielded).
            if not response.tool_calls:
                step = _PhaseStep()
                async for event in self._finalize_no_tool_calls(
                    response, recorded_user_message, turn_tool_calls_log, turn_tool_results_log, step,
                ):
                    yield event
                if step.directive == _STOP:
                    return
                continue

            # A tool batch means the model is acting again, so a prior goal nudge is
            # answered — the next time it stops, it will be re-nudged fresh.
            self._awaiting_goal_reconsideration = False

            # Phase 3 — run the tool batch (append the checkpoint AIMessage, preflight the
            # whole batch's permissions, suspend if a human is needed, drain the tools,
            # checkpoint), then honor a Stop that landed during it.
            step = _PhaseStep()
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

    def _build_turn_messages(self, first_iteration: bool) -> list:
        """The message list for this iteration's model call: the static system prompt,
        the conversation, and — only on the turn's first iteration — the dynamic context.

        Dynamic context (time, pwd, active goal, tasks, background) is injected only on
        the first iteration of a turn, when the user just sent a message; subsequent
        iterations (after tool calls) skip it to avoid re-sending the same per-turn
        metadata on every LLM call within the turn. It rides as a transient user-role
        harness note at the very tail of the request — never as a system message (LiteLLM
        would hoist it into Anthropic's top-level system param, whose fresh timestamp
        would then invalidate the ENTIRE conversation cache on every turn). As a tail
        note, everything before it still prefix-matches the provider cache."""
        dynamic_parts = (
            [self._harness_note_message(self._build_dynamic_context())]
            if first_iteration else []
        )
        return (
            [SystemMessage(content=self._build_static_system_prompt())]
            + self._conversation
            + dynamic_parts
        )

    async def _stream_model_call(
        self, messages: list, outcome: _ModelCallOutcome
    ) -> AsyncIterator[TurnEvent]:
        """One streamed model call. Yields the thinking/answer events and writes the
        assembled response into ``outcome`` — or a terminal condition instead: ``cancelled``
        (a Stop with nothing queued; a ``Done`` was already yielded) or
        ``aborted_for_steering`` (a Stop that found queued steering, so the driver drains
        it and iterates again).

        Opens a thinking step for the iteration: one channel (THINKING) drives the
        indicator — this bare ping marks "reasoning started" and reasoning_content fills
        the body — and a matching THINKING_DONE fires the moment reasoning ends (the first
        answer token, or, for a tool-only turn, when the stream closes), timed server-side
        as wall-clock so "Thought for Ns" is correct live and on replay. Each read races
        the abort event so a Stop interrupts *immediately*, even while parked awaiting the
        next token from a slow or stalled provider — checking the flag only between chunks
        let a provider that had gone quiet swallow the cancel until it happened to emit
        again, which is why Stop "sometimes" appeared to do nothing."""
        yield Thinking()
        thinking_started_at = time.monotonic()
        thinking_done_emitted = False
        response_chunks: list[AIMessageChunk] = []
        aborted_for_steering = False
        # A generation span for this model call. Started (not made "current") so it is
        # safe to hold open across this generator's yields; ended in the finally below.
        generation_span = _telemetry.start_span(
            "gen_ai.generation", {"gen_ai.request.model": self.effective_model_identifier}
        )
        model_stream = self._bound_llm.astream(messages)
        abort_waiter = asyncio.ensure_future(self._abort_event.wait())
        try:
            while True:
                chunk_future = asyncio.ensure_future(_stream_next(model_stream))
                await asyncio.wait(
                    {chunk_future, abort_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                if self._abort_event.is_set():
                    # Stop won the race (or landed between chunks): drop the pending
                    # read and stop consuming the stream (the `finally` closes it).
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
            # Close the underlying HTTP stream so an aborted (or exhausted) turn
            # never leaks a provider connection.
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
        turn_tool_calls_log: list[dict], turn_tool_results_log: list[dict], step: _PhaseStep,
    ) -> AsyncIterator[TurnEvent]:
        """Handle a model response that made no tool calls. Retries a malformed-only
        batch, delivers or awaits agent messages, nudges an active goal once to keep
        working, or finishes the turn — advancing the loop bookkeeping and setting ``step``
        to ``_CONTINUE`` (iterate again) or ``_STOP`` (a terminal ``Done`` was yielded)."""
        if response.invalid_tool_calls:
            # A response carrying only malformed tool calls (arguments that failed to
            # parse). These are NOT valid tool_calls — the LiteLLM model serializes only
            # message.tool_calls, never invalid_tool_calls — so a ToolMessage response
            # would be orphaned, and strict providers (e.g. DeepSeek) reject that with
            # "Messages with role 'tool' must follow a tool_calls message". Correct the
            # model with a harness note and let it retry. Model-facing; not surfaced.
            if response.content:
                self._conversation.append(response)
            for invalid in response.invalid_tool_calls:
                self._conversation.append(self._harness_note_message(
                    self._invalid_tool_call_content(cast(dict, invalid)),
                ))
            step.directive = _CONTINUE
            return

        # The model produced no tool calls. Any still-running background work does not
        # hold the turn open: it ends here, and the executor's resume pump wakes the agent
        # with an autonomous turn once the next result lands. Results that already
        # completed were drained at the top of the loop, so nothing in hand is lost.
        final_text = message_text(response)
        self._conversation.append(response)
        steering_events = await self._drain_steering_messages()
        if steering_events:
            for steering_event in steering_events:
                yield steering_event
            step.directive = _CONTINUE
            return
        if self._active_goal and not self._awaiting_goal_reconsideration:
            # The model stopped while a goal is active. Nudge it once to reconsider — but
            # only once per stop: if it produces no tool calls again (it reaffirms it is
            # done), the turn completes below. Any tool call in between clears the flag, so
            # a model that keeps working is nudged fresh each time it next stops, with no
            # nudge counter and no ceiling.
            self._awaiting_goal_reconsideration = True
            goal_continuation = self._prompt_loader.load("goal_continuation", {"goal": self._active_goal})
            self._conversation.append(self._harness_note_message(goal_continuation))
            yield Status(code="goal_check",
            )
            step.directive = _CONTINUE
            return
        self._record_turn(
            recorded_user_message, turn_tool_calls_log,
            turn_tool_results_log, final_text,
        )
        yield Done(text=final_text, stop_reason="completed")
        step.directive = _STOP

    async def _run_tool_batch(
        self, response: AIMessageChunk, recorded_user_message: str,
        turn_tool_calls_log: list[dict], turn_tool_results_log: list[dict], step: _PhaseStep,
    ) -> AsyncIterator[TurnEvent]:
        """Run the response's tool batch and checkpoint it. Appends the initiating
        AIMessage first — an AIMessage carrying tool_calls with no ToolMessages yet is the
        durable resume checkpoint — then resolves the whole batch's permissions BEFORE any
        tool runs (concurrent tools cannot be re-run on resume without re-doing side
        effects). If a human is needed the turn suspends here; otherwise it drains with
        every decision already in hand. Sets ``step`` to ``_STOP`` (a top-level suspend
        that returns, or a Stop with nothing queued) or ``_CONTINUE`` (a Stop that found
        queued steering); leaves it ``_PROCEED`` for the normal end of the iteration."""
        self._conversation.append(response)
        tool_calls = cast(list[dict], response.tool_calls)
        outcomes: dict[str, dict] = {}
        if not self._abort_event.is_set():
            plans, pending = await self._preflight_permissions(tool_calls)
            if pending:
                # One suspend event for every turn: the session renders the prompt from it,
                # and the pause is durable. The segment closes here as input-required and a
                # later answer rebuilds the turn from its checkpoint, so a session waiting on
                # a person survives a daemon restart rather than losing the work it had
                # already done. There is no second, ephemeral continuation path any more —
                # every turn belongs to a session, and every session is addressable.
                yield Suspended(interactions=[SuspensionGate(**gate.to_dict()) for gate in pending],
                    plans={tool_call_id: plan.to_dict() for tool_call_id, plan in plans.items()},
                )
                step.directive = _STOP
                return
            else:
                decisions = self._resolve_tool_decisions(plans, {})
            async for event in self._drain_tools_concurrently(
                tool_calls, turn_tool_calls_log, turn_tool_results_log, outcomes, decisions,
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
