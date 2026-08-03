"""The AgentRuntime turn-loop concern (a mixin composed into AgentRuntime).

The ``stream()`` driver and its phases — the model call, the no-tool-calls finalize (goal nudge,
agent messaging, completion), and the tool batch — plus turn-message assembly, the static/dynamic
system prompt, steering drain, and turn recording."""
from __future__ import annotations

import json
import logging
from contextlib import suppress
from datetime import datetime, timezone
from frank.base import telemetry as _telemetry
from frank.base.identifiers import new_id
from frank.runtime.internals import (
    _CONTINUE,
    _detect_workspace,
    _ModelCallOutcome,
    _PhaseStep,
    _STOP,
    _STREAM_EXHAUSTED,
    _stream_next,
    _ToolPlan,
    _maybe_json,
    conversation_tokens,
)
from frank.runtime.prompt.environment import probe_local_environment, probe_user_context
from frank.protocol.events import TurnContext
from frank.base.instructions import instructions_payload
from frank.base.memories import memories_payload
from frank.base.message_content import message_content_deltas, message_text
from frank.base.model_errors import ContextWindowExceeded, over_context_window
from frank.base.skills import enabled_skills, skills_for_agent, skills_payload
from frank.runtime.turn_events import (
    Checkpoint,
    Done,
    Status,
    Steering,
    Suspended,
    SuspensionGate,
    TextChunk,
    Thinking,
    ThinkingDone,
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
from frank.base.serialization import compact


logger = logging.getLogger(__name__)

class _RunsTurns:
    """The turn itself: what the model is told, what comes back, and when it is over.

    The loop the other three serve."""

    def _locations_summary(self) -> list[dict]:
        """The workspace's locations as the model sees them: the `location` URI to pass,
        plus name/kind/base_directory/permission so it can choose the right one per tool call.

        The permission reported is the mode a call against that location would actually be judged
        by — `_call_policy`'s answer — rather than the mode recorded on the location. Those differ
        in the ordinary case: a location that names no mode of its own records `default`, meaning
        "whatever the session is", so reporting the record told the model `default` no matter what
        the session had been set to. Now that this rides in the turn context and is rebuilt every
        turn, it can state the live answer, which is the only version worth stating."""
        return [
            {
                "location": resolved.uri,
                "name": resolved.name,
                "kind": resolved.kind,
                "base_directory": resolved.base_directory,
                "permission_mode": self._call_policy(resolved).mode,
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
            all_skills = enabled_skills(list(self._catalogue.skills()))
            agent_skills = skills_for_agent(all_skills, self._agent_configuration.skills)
            memories = list(self._catalogue.memories())
            worktree_root, is_git_repo = _detect_workspace(self._working_directory)
            context_json = compact({
                "session": self._session_id,
                # Present only when another session created this one. Its presence is the
                # obligation: there is somebody waiting for an answer, and this is where to
                # send it.
                **({"parent_session": self._parent_session} if self._parent_session else {}),
                "working_directory": self._working_directory,
                "project_directory": self._project_directory,
                "worktree_root": worktree_root,
                "is_git_repo": is_git_repo,
                "session_worktree_strategy": self._global_configuration.workspace.strategy,
                "platform": platform.system(),
                "today_date": datetime.now().strftime("%Y-%m-%d"),
                # `locations` is deliberately absent: it carries each location's permission mode,
                # which a person can change mid-session, and anything changeable in here rewrites
                # the front of every request. It rides in the turn context instead.
            })
            # Also conditional: it asserts "you are running as a session", which a
            # library-embedded runtime is not, and which has no peer tools either.
            #
            # The instruction to report to a parent is conditional *within* it, on there being a
            # parent, and it names the real id. It used to ship to every session that merely had
            # the tool, phrased as "if `parent_session` is in your context" — so a session that
            # nobody created still read an instruction to report, and one duly sent its findings
            # to a session literally named `parent_session`. A placeholder in a prompt is a
            # value to whoever reads it; the way not to have it mistaken for an id is not to
            # write one.
            parent_report = (
                self._prompt_loader.load("parent_report", {"parent": self._parent_session})
                if self._parent_session else ""
            )
            agent_context = (
                self._prompt_loader.load("agent_context", {"parent_report": parent_report})
                if "message_session" in {tool.name for tool in self._tools} else ""
            )
            # The opt-in user-context section is its own template, rendered into the prompt's
            # `user_environment` slot only when enabled — so the section (heading and all) simply
            # is not there when off. Only the standing guidance lives here; the snapshot it
            # describes travels with each turn, because it is read fresh from the machine and
            # would otherwise differ between one worker and the next.
            user_environment = ""
            if self._user_context_enabled():
                user_environment = self._prompt_loader.load("user_context", {})
            # The computer/browser tools are opt-in, so their guidance (what each is for, and
            # to pick the right one rather than force one) only enters the prompt when they do.
            computer_control_guidance = ""
            if self._global_configuration.computer_control.enabled:
                computer_control_guidance = self._prompt_loader.load("computer_control_guidance", {})
            # Guidance for tools this session does not have is guidance to call something that
            # is not there. Peer sessions and MCP were stated unconditionally, so a session
            # embedded as a library — no control plane, no MCP server — was told at length how to
            # `create_session` and `message_session`, and had neither.
            available = {tool.name for tool in self._tools}
            peer_sessions = (
                self._prompt_loader.load("peer_sessions", {})
                if "create_session" in available or "message_session" in available else ""
            )
            mcp_servers = (
                self._prompt_loader.load("mcp_servers", {})
                if "call_mcp_tool" in available else ""
            )
            # Whole documents, so each is laid out by its own template rather than serialised
            # into a JSON array of escaped strings: metadata as JSON, the document as itself.
            # Empty when the machine and project supply none, which drops the section instead of
            # leaving a bare `[]` in the prompt.
            instruction_files = "".join(
                self._prompt_loader.load("instruction_file", {
                    "metadata": compact({"source": entry["source"]}),
                    "content": entry["content"].strip(),
                })
                for entry in instructions_payload(self._catalogue.instructions())
            ).strip()
            instructions = (
                self._prompt_loader.load("instructions", {"files": instruction_files})
                if instruction_files else ""
            )
            self._cached_system_prompt = self._prompt_loader.load("system_prompt", {
                "system_prompt": self._system_prompt,
                "context": context_json,
                "user_environment": user_environment,
                "instructions": instructions,
                "skills": compact(skills_payload(agent_skills)),
                "memories": compact(memories_payload(memories)),
                "agent_context": agent_context,
                "computer_control_guidance": computer_control_guidance,
                "peer_sessions": peer_sessions,
                "mcp_servers": mcp_servers,
            }).strip()
        return self._cached_system_prompt

    def _user_context_enabled(self) -> bool:
        user_context = getattr(self._global_configuration, "user_context", None)
        return user_context is not None and bool(user_context.enabled)

    def _ensure_environment_note(self) -> None:
        """Put the machine snapshot into the conversation once, at the session's first message.

        Minted once and then left alone, which is the whole point. It describes a machine, and a
        machine does not change between two turns of a conversation in any way worth re-reading;
        what it *does* do is read the live environment and the shell history, so producing it
        again later yields different bytes for reasons the user never caused.

        That is why it cannot live in the system prompt. The prompt is rebuilt whenever a worker
        wakes — a worker is created per activation — and it sits at the very front of the
        request, which is exactly where a prompt cache matches. A snapshot that differed because
        the user had opened a terminal since the last wake therefore missed the cache for every
        call of that session.

        Appended to the conversation instead, it is written once, travels in the checkpoint, and
        is part of the cached prefix from the second call onward. Appending is safe at any point,
        so a conversation that predates this simply gains one on its next turn.
        """
        if any(message.additional_kwargs.get("environment_note") for message in self._conversation):
            return
        snapshot = _maybe_json(probe_local_environment())
        payload: dict[str, Any] = {"machine": snapshot if isinstance(snapshot, dict) else {}}
        if self._user_context_enabled():
            user_context = _maybe_json(probe_user_context())
            if isinstance(user_context, dict) and user_context:
                payload["user_context"] = user_context
        note = self._reminder_message(compact(payload))
        note.additional_kwargs["environment_note"] = True
        self._conversation.append(note)


    def _append_turn_context(self) -> None:
        """Append this turn's context to the conversation, if it says anything new.

        The same treatment the environment note gets, and for the same reason: a message that is
        appended and kept extends the cached prefix, while one assembled for a single request and
        dropped guarantees the next request differs where it used to sit.

        Emitted only on change, which is what keeps that affordable. Of the fields here only
        ``now`` moves every turn, and a fresh clock reading is not worth a message of its own — it
        rides along whenever something that matters has actually changed (the directory, the
        goal, the task list, background work, the reachable locations) and is otherwise left to
        the wall clock the model can read for itself. So a session doing steady work appends
        nothing at all, and one whose situation changed appends the picture once.
        """
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
        """The structured per-turn context injected at the end of the message list: the current
        time, where the agent is, its goal, its tasks, its background work, the machine it runs
        on and the locations it may reach. Empty goal/tasks are omitted so the model isn't fed
        noise. Standing behavioural guidance lives once in the system prompt, not re-injected
        here — what lives here is everything that can *differ*, so that the system prompt in
        front of it stays byte-identical and the provider's cache keeps matching it."""
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
            screen=self._screen_context(),
            locations=self._locations_summary(),
        )
        return context.model_dump_json(exclude_defaults=True)

    def _screen_context(self) -> dict:
        """Every place a screen script can be pointed at, and what may be called in each.

        The cold-start problem this solves was designed away on paper and then left in the code:
        `control_screen` requires a target, target ids are minted by the platform, and nothing
        ever told the model what they were — so the first call of every screen task had to fail in
        order to read the list out of the error. It guessed an application name, which is not an
        address, and burned two calls before it could begin.

        Enumerating windows is window-server and accessibility work only; it never connects to a
        browser, so carrying this every turn cannot put a consent dialog in front of anybody."""
        if not self._global_configuration.computer_control.enabled:
            return {}
        try:
            from frank.computer import targets as target_registry
            from frank.computer.surface import message_loader

            # The one turn where this is expensive is skipped rather than waited on.
            #
            # Enumerating costs ~1.8s the first time a process asks and ~0.15s afterwards, and
            # the worker warms itself in the background from the moment it starts. But a session
            # is a fresh process per wake and its first message arrives right behind the fork, so
            # a turn that insisted on the listing sat in front of the model call waiting out a
            # warm-up that was already running — roughly two seconds, on the very turn a person
            # is watching for the first sign of life.
            #
            # So a cold process says so instead. The model is told the listing is being read and
            # to ask for it if this turn needs it, which costs one call on the rare turn that
            # does; every turn from the second on carries it as before, freshly enumerated. No
            # snapshot is cached and nothing here is ever stale — the choice is between the
            # current listing and none, never between the current one and an old one.
            if not target_registry.warm():
                return {"reading": message_loader("computer")("screen_warming")}

            # A read-only session is shown only the primitives it may actually run. Listing one
            # the permission layer will refuse advertises a capability and then denies it, which
            # is the same defect as any other promise the code does not keep.
            from frank.computer import workflows

            block = target_registry.context_block(
                # The session's effective mode, not the agent profile's. Those are different
                # whenever a session is created more restrictive than its profile, which is the
                # ordinary case for `read_only` — profiles rarely set one. Asking the profile
                # meant a read-only session was shown `click`, `type` and `evaluate` as things it
                # could do, which is the advertise-then-refuse defect this filter exists to
                # prevent, in the one situation it was written for.
                mutating_allowed=not self.is_read_only,
            )
            # What somebody has already worked out and saved, so a task that has been solved once
            # is imported rather than derived again. Read off the files without importing them —
            # this runs every turn, and importing user code to find out its name would run that
            # code every turn.
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
        """Hand one completed turn to the caller's transcript, if there is one.

        Separate from `_record_turn`, which feeds the `Observer` and is per *message*. This is
        one entry per turn — what was asked, what came back, how it ended and what it cost —
        which is the shape a program wants for auditing, billing and showing a history.

        A transcript that raises must not fail the turn: the work happened whether or not it
        was written down, and losing the answer because the record failed would be the worse
        of the two outcomes."""
        if self._transcript is None:
            return
        from frank.base.ports import TurnSummary

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
            message, message_id = self._steering_messages.get_nowait()
            self._conversation.append(HumanMessage(content=message))
            events.append(Steering(text=message, message_id=message_id))
        if self._steering_messages.empty():
            self._steering_available.clear()
        return events

    async def _answer_gates(self, gates: list[SuspensionGate]) -> dict[str, Any]:
        """Ask the caller's `Approvals` about each gate, in the shape a resume would carry.

        Without one, nothing is answered and every gate suspends — which is what has always
        happened, and is right when there is a person on the other end. With one, a script can
        run unattended without hand-driving the suspend/resume dance through `stream`.

        A `None` verdict means *no opinion*, and that gate suspends alone. So an approver can
        auto-allow the cases it understands and still escalate the rest, instead of facing an
        all-or-nothing choice. An approver that raises is treated the same way: the gate goes to
        a human, because a broken policy must fail closed rather than open.
        """
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
        """A reminder: something neither the user nor the model said, put into the conversation
        as a user-role message.

        The role is deliberate, and it is what keeps the conversation strictly append-only for
        the provider's prompt cache. A mid-conversation ``role:"system"`` message does not stay
        where it is put — LiteLLM hoists it into Anthropic's top-level ``system`` parameter, and
        the Responses translation folds it into ``instructions`` — so it renders before the
        entire history, and writing one rewrites the prefix and discards the cache for the whole
        conversation. A user-role reminder stays exactly where it was appended, on every
        provider, so the prefix only ever grows.

        The wrapper lives in the ``reminder`` prompt template, so the wording stays in a file
        rather than in code; it tells the model this is authoritative guidance from the system
        it is running in, not something the user typed. The ``reminder`` marker is how everything
        downstream tells the two apart — most importantly the fold, which carries the user's own
        messages across verbatim and must not mistake one of these for one of those.

        ``image_blocks`` (OpenAI-shaped ``image_url`` blocks) make a reminder multimodal: the
        user role is the one role every provider accepts images on, which is how a file the
        model asked to read reaches a vision model.

        The heading it carries lives in the ``reminder`` template, like every other piece of
        model-facing wording here. On the Responses API the `developer` role says the same thing
        by itself; elsewhere — Anthropic's Messages API has no per-message system role at all,
        only a top-level parameter — the text is the only place the distinction can live, so it
        is written once, here, and reads the same everywhere."""
        text = self._prompt_loader.load("reminder", {"content": content.strip()}).strip()
        # `transient` marks a note that is assembled for one request and never appended to the
        # conversation — so it cannot appear in the next one, and a cache breakpoint placed on it
        # is a breakpoint nothing will ever match.
        tags = {"reminder": True, **({"transient": True} if transient else {}), **(marks or {})}
        if image_blocks:
            return HumanMessage(content=[{"type": "text", "text": text}, *image_blocks], additional_kwargs=tags)
        return HumanMessage(content=text, additional_kwargs=tags)

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
        # state so the no-tool-calls phase can advance it across iterations.
        self._awaiting_goal_reconsideration = False

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
            # Before the first message of the session, so the snapshot sits at the front of the
            # conversation and every later call prefix-matches over it.
            self._ensure_environment_note()
            self._close_dangling_tool_calls()
            # A turn's input is usually plain text, but an attachment turn carries a
            # multimodal content list (a text block plus one image_url block per
            # attached image) so a vision model actually sees the pixels. LangChain's
            # HumanMessage accepts either, and the model adapter passes the content
            # straight through to the provider. A harness-initiated turn (an autonomous
            # wake, a report reminder) enters as a reminder so the
            # model treats it as its own observation, not as something the user said — in
            # a user-role message so the append stays cache-safe (_reminder_message).
            turn_message = (
                self._reminder_message(user_message)
                if as_system_note and isinstance(user_message, str)
                else HumanMessage(content=user_message)
            )
            self._conversation.append(turn_message)
            # The event-log recorder only wants prose from LangChain's standard blocks.
            recorded_user_message = message_text(turn_message)
        # After the turn's message, so the freshest picture is the last thing the model reads,
        # and once per turn rather than per iteration — a tool hop changes nothing this describes.
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

            # In-flight background work no longer holds the turn open. Completed
            # results are drained above and delivered mid-turn while the model is
            # still working; if the model goes idle with work still pending, the turn
            # simply ends and the executor's resume pump wakes the agent with an
            # autonomous turn the moment the next result lands.
            for steering_event in await self._drain_steering_messages():
                yield steering_event

            # Keeping the context inside the window. Folding is the whole of it: it replaces
            # an unbounded head with a fixed-size observation log, so what it saves compounds
            # over the rest of the run rather than being spent once. The reserved buffer
            # guarantees room to run the fold itself, and compact() re-measures the occupancy
            # so this cannot re-fire in a loop.
            if self._should_compact():
                async for compaction_event in self.compact(reason="auto"):
                    yield compaction_event

            messages = self._build_turn_messages()

            # Phase 1 — the model call. Yields the thinking/answer stream and hands back
            # the assembled response, or a terminal (cancelled) / steering condition.
            call = _ModelCallOutcome()
            try:
                async for event in self._stream_model_call(messages, call):
                    yield event
            except ContextWindowExceeded as overflow:
                # The context reading is written from a call's reported usage, and a call that is
                # refused reports none — so the indicator held whatever the last *successful* call
                # said and went on claiming it. One session showed 3% full (36,021 of 1,050,000)
                # while the provider was rejecting the conversation for length. Refusal is itself
                # a measurement, and the one thing it establishes is that the window is full.
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

    def _build_turn_messages(self) -> list:
        """The message list for this iteration's model call: the static system prompt,
        the conversation, and — only on the turn's first iteration — the dynamic context.

        Dynamic context (time, pwd, active goal, tasks, background) is injected only on
        the first iteration of a turn, when the user just sent a message; subsequent
        iterations (after tool calls) skip it to avoid re-sending the same per-turn
        metadata on every LLM call within the turn. It rides as a transient user-role
        reminder at the very tail of the request — never as a system message (LiteLLM
        would hoist it into Anthropic's top-level system param, whose fresh timestamp
        would then invalidate the ENTIRE conversation cache on every turn). As a tail
        note, everything before it still prefix-matches the provider cache.

        The per-turn context is *in* that conversation rather than assembled beside it — see
        :meth:`_append_turn_context`. It used to ride here as a transient note appended to the
        request and dropped afterwards, which made the last item of every first-iteration request
        an item the next request did not have. Measured over a real session, that was the only
        divergence the harness produced anywhere: five of twelve calls, every one of them at the
        final position. It cost little, because everything before it still matched, but "the
        request is append-only except for one item" is a weaker promise than the one this is
        supposed to make, and it is not a promise worth keeping weak for 112 tokens."""
        return [SystemMessage(content=self._build_static_system_prompt())] + self._conversation

    def _refuse_if_over_window(self, messages: list) -> None:
        """Refuse a request that cannot fit, before it is sent, and say so with numbers.

        The harness assembles the request and knows the window, so whether the two fit is a fact
        it can compute rather than a verdict it has to wait for. Measuring here beats every
        alternative on the three things that matter: it is the same answer for every provider
        (including any that reports an overflow with no machine-readable code at all), it names
        the size instead of merely the failure, and it does not spend a round trip discovering
        something already knowable.

        This replaced matching phrases like "exceeds the context window" against whatever prose a
        provider returned — a test that would have quietly stopped working the day a provider
        reworded its message, and never worked in another language.

        Conservative on purpose (see :func:`over_context_window`): the count uses one general
        tokenizer as a proxy for every model's own, so it is approximate, and refusing a request
        the model would have taken is the worse of the two mistakes. Anything this misses the
        provider still catches, and is still classified properly when it does.

        Conservative is not the same as blind, though, and this used to be both. Reading only
        each message's prose meant a tool call's arguments and the size of what came back were
        counted as nothing — so a conversation that was almost entirely tool traffic measured
        near zero, and one request went out at 272,640 tokens against a 272,000 window without
        this noticing. :func:`conversation_tokens` counts what is actually sent."""
        window = self._context_window
        if window <= 0:
            return                              # the catalogue is cold; it says nothing about room
        tokens = conversation_tokens(messages)
        if not over_context_window(tokens, window):
            return
        # Recorded so the indicator agrees with the refusal instead of reporting the reading from
        # the last call that succeeded.
        self._latest_context_tokens = tokens
        raise ContextWindowExceeded(
            "The assembled request is larger than this model's context window.",
            model=self.effective_model_identifier, context_window=window, tokens=tokens,
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
        # A hook may read the conversation about to leave the process, and may change it.
        if not self._hooks.empty:
            messages = await self._hooks.before_model(messages)
        self._refuse_if_over_window(messages)
        model_stream = self._bound_model.astream(messages)
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
            # model with a reminder and let it retry. Model-facing; not surfaced.
            if response.content:
                self._conversation.append(response)
            for invalid in response.invalid_tool_calls:
                self._conversation.append(self._reminder_message(
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
            self._conversation.append(self._reminder_message(goal_continuation))
            yield Status(code="goal_check",
            )
            step.directive = _CONTINUE
            return
        self._record_turn(
            recorded_user_message, turn_tool_calls_log,
            turn_tool_results_log, final_text,
        )
        await self._record_transcript_turn(
            recorded_user_message, final_text, "completed", turn_tool_calls_log,
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
            gates = [SuspensionGate(**gate.to_dict()) for gate in pending]
            answered = await self._answer_gates(gates)
            if gates and len(answered) < len(gates):
                # One suspend event for every turn: the session renders the prompt from it,
                # and the pause is durable. The segment closes here as input-required and a
                # later answer rebuilds the turn from its checkpoint, so a session waiting on
                # a person survives a daemon restart rather than losing the work it had
                # already done. There is no second, ephemeral continuation path any more —
                # every turn belongs to a session, and every session is addressable.
                #
                # Any gate an `Approvals` did answer is folded into the plans, so a partial
                # answer narrows what the human is asked rather than being discarded.
                yield Suspended(
                    interactions=[gate for gate in gates if gate.request_id not in answered],
                    plans={tool_call_id: plan.to_dict() for tool_call_id, plan in plans.items()},
                )
                step.directive = _STOP
                return
            else:
                decisions = self._resolve_tool_decisions(plans, answered)
            # After the barrier, deliberately. A hook sees only what the rules already
            # approved, so it can drop calls and can never introduce one.
            if not self._hooks.empty:
                tool_calls = await self._hooks.before_tools(tool_calls)
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
