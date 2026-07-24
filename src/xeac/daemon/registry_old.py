"""The daemon's directory of live sessions.

It knows every session's address, capability token, status, and parent, and it is the only
thing that can resolve a session id into somewhere to send a message. Sessions themselves
talk to each other directly over their sockets once they hold an address — the registry
hands out addresses, it does not stand between peers.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from a2a.types import AgentCard

from xeac.protocol.turn_record import TurnRecord

logger = logging.getLogger(__name__)

class AgentRegistry:
    """The A2A agent directory.

    Holds every served agent's request handler and AgentCard, and provides A2A
    *delegation*: one agent invokes another by sending it a real A2A message
    (``message/stream``) and receiving its task back. It also owns the mailbox
    between concurrently active tasks. The A2A context is the authorization and
    discovery boundary; mailbox delivery itself is a local safe-point operation,
    because A2A has no RPC for injecting a peer question into a running model call.
    """

    def __init__(self, task_store: TaskStore):
        self._task_store = task_store
        self._handlers: dict[str, RequestHandler] = {}
        self._cards: dict[str, AgentCard] = {}
        self._participants: dict[str, _ActiveAgentParticipant] = {}
        self._participant_task_ids: dict[str, str] = {}
        self._reserved_participants: dict[str, tuple[str, str]] = {}
        self._agent_questions: dict[str, _AgentQuestion] = {}
        # Serializes native input-required resolves per context, so answers to a
        # multi-gate batch are recorded one at a time rather than racing on task metadata.
        self._resolve_locks: dict[str, asyncio.Lock] = {}
        # External (over-the-wire) A2A agents, if configured. When a delegation names one
        # of these, make_delegate reaches it through this manager's A2A client instead of
        # the in-process local handler path.
        self._remote_manager: Optional[RemoteAgentManager] = None
        # Signs URLs for files forwarded to a remote agent as FileParts.
        self._file_url_signer: Optional[FileUrlSigner] = None
        # Per (local session context, remote agent) → the remote server's contextId, so a
        # session keeps continuity with a remote agent across turns without ever leaking
        # our own contextId.
        self._remote_contexts: dict[tuple[str, str], str] = {}

    def set_remote_manager(self, remote_manager: Optional[RemoteAgentManager]) -> None:
        """Install (or replace) the outbound A2A client manager. Safe to call on reload."""
        self._remote_manager = remote_manager

    def set_file_url_signer(self, signer: Optional[FileUrlSigner]) -> None:
        self._file_url_signer = signer

    def is_remote_agent(self, name: str, profile: str = "") -> bool:
        return (
            self._remote_manager is not None
            and self._remote_manager.is_remote(name)
            and self._remote_manager.is_allowed_for(name, profile)
        )

    def remote_roster(self, profile: str = "") -> list[dict[str, str]]:
        """Describe the reachable remote agents this ``profile`` may call, the way
        ``describe_available_agents`` describes local ones, so the model sees them in its
        roster."""
        if self._remote_manager is None:
            return []
        roster: list[dict[str, str]] = []
        for name in self._remote_manager.names():
            if not self._remote_manager.is_allowed_for(name, profile):
                continue
            card = self._remote_manager.card(name)
            description = (card.description if card is not None else "") or ""
            roster.append({
                "id": name,
                "title": (card.name if card is not None else name) or name,
                "description": (description + " (external A2A agent)").strip(),
                "role": "remote",
            })
        return roster

    def register(self, name: str, handler: RequestHandler, card: AgentCard) -> None:
        self._handlers[name] = handler
        self._cards[name] = card

    def handler_for(self, name: str) -> Optional[RequestHandler]:
        """The request handler that drives ``name``'s turns, or ``None`` if unregistered —
        the public accessor an executor uses to drive a self-sent turn, so it never reaches
        into the registry's private handler map."""
        return self._handlers.get(name)

    async def resolve_pending_input(
        self, context_id: str, request_id: str, *,
        decision: str = "", answers: Optional[list] = None, declined: bool = False,
    ) -> bool:
        """Route a native resolve (a permission decision or a question's answers) into the
        same durable ``input_response`` path an external client uses, so both share one
        resolution and one resume. Finds the input-required task that owns ``request_id``,
        then drives an ``input_response`` message/send on that task's agent handler — which
        records the answer and, once every gate is answered, rebuilds and resumes the turn."""
        tasks = await self._task_store.tasks_for_context(context_id)
        for task in tasks:
            pending = TurnRecord.from_metadata(task.metadata).pending
            if pending is None or pending.gate_for(request_id) is None:
                continue
            handler = self._handlers.get(pending.agent)
            if handler is None:
                return False
            data: dict[str, Any] = {"request_id": request_id}
            if declined:
                data["declined"] = True
            elif answers is not None:
                data["answers"] = answers
            else:
                data["decision"] = decision
            message = Message(
                role=Role.user,
                parts=[_envelope_part(INPUT_RESPONSE_KIND, **data)],
                message_id=uuid.uuid4().hex,
                task_id=task.id,
                context_id=context_id,
            )
            # Drive in the background so a REST resolve returns at once while the resumed
            # turn streams over the fan-out, serialized per context so answers to a
            # multi-gate batch are recorded one at a time rather than racing on metadata.
            lock = self._resolve_locks.setdefault(context_id, asyncio.Lock())

            async def drive(handler=handler, message=message, lock=lock) -> None:
                try:
                    async with lock:
                        async for _event in handler.on_message_send_stream(MessageSendParams(message=message)):
                            pass
                except Exception:
                    logger.exception("Durable input resume failed for context %s", context_id)

            spawn_background_task(drive())
            return True
        # No durable (top-level) record owns this request: it is a delegated agent parked in
        # place awaiting the user. Resolve its in-memory future on the delegated agent's runtime,
        # which continues on its own live delegation stream.
        if declined:
            value: Any = {"__declined__": True}
        elif answers is not None:
            value = answers
        else:
            value = decision
        for participant in list(self._participants.values()):
            if participant.context_id == context_id and participant.runtime.resolve_agent_permission(request_id, value):
                return True
        return False

    async def abort_pending_input(self, context_id: str) -> bool:
        """Deny every pending gate of a context's input-required task. The last denial
        resumes the turn, which records a denial ToolMessage for each call — keeping the
        conversation valid (no dangling tool-call AIMessage) — and then ends. Returns
        whether a pending task was found."""
        tasks = await self._task_store.tasks_for_context(context_id)
        for task in tasks:
            pending = TurnRecord.from_metadata(task.metadata).pending
            if pending is None or not pending.gates:
                continue
            for gate in pending.gates:
                if gate.is_question:
                    await self.resolve_pending_input(context_id, gate.request_id, declined=True)
                else:
                    await self.resolve_pending_input(context_id, gate.request_id, decision="deny")
            return True
        return False

    def names(self) -> list[str]:
        return list(self._cards.keys())

    def cards(self) -> list[AgentCard]:
        return list(self._cards.values())

    def card(self, name: str) -> Optional[AgentCard]:
        return self._cards.get(name)

    def active_agents(self, requesting_task_id: str) -> list[dict[str, str]]:
        """List the other addressable participants in the requester's A2A context."""
        requester = self._participants.get(requesting_task_id)
        if requester is None:
            return []
        active_participants = [
            participant
            for participant in self._participants.values()
            if (
                participant.context_id == requester.context_id
                and participant.task_id != requesting_task_id
            )
        ]
        return [
            {
                "task_identifier": participant.task_identifier,
                "agent": participant.agent_name,
            }
            for participant in sorted(
                active_participants,
                key=lambda participant: participant.task_identifier,
            )
        ]

    def reserve_participant(
        self,
        task_identifier: str,
        context_id: str,
        agent_name: str,
    ) -> None:
        """Reserve a public handle while its delegated A2A task is starting."""
        if task_identifier:
            self._reserved_participants[task_identifier] = (context_id, agent_name)

    def register_participant(
        self,
        task_id: str,
        task_identifier: str,
        context_id: str,
        agent_name: str,
        runtime: AgentRuntime,
    ) -> None:
        """Register an active A2A task as an addressable mailbox participant."""
        public_identifier = task_identifier or task_id
        participant = _ActiveAgentParticipant(
            task_id=task_id,
            task_identifier=public_identifier,
            context_id=context_id,
            agent_name=agent_name,
            runtime=runtime,
        )
        self._participants[task_id] = participant
        self._participant_task_ids[public_identifier] = task_id
        self._participant_task_ids[task_id] = task_id
        self._reserved_participants.pop(public_identifier, None)
        for question in self._agent_questions.values():
            if (
                not question.recipient_task_id
                and question.recipient_task_identifier == public_identifier
            ):
                question.recipient_task_id = task_id
                self._deliver_question(question)

    def unregister_participant(self, task_id: str) -> None:
        """Remove a terminal task and settle every unanswered mailbox exchange."""
        participant = self._participants.pop(task_id, None)
        if participant is None:
            return
        self._participant_task_ids.pop(participant.task_identifier, None)
        self._participant_task_ids.pop(task_id, None)
        for question_identifier, question in list(self._agent_questions.items()):
            if question.sender_task_id == task_id:
                recipient = self._participants.get(question.recipient_task_id)
                if recipient is not None:
                    recipient.runtime.enqueue_agent_message(AgentMessage(
                        identifier=f"message-{uuid.uuid4().hex}",
                        kind="withdrawn",
                        sender_task_id=participant.task_id,
                        sender_task_identifier=participant.task_identifier,
                        sender_agent_name=participant.agent_name,
                        recipient_task_id=recipient.task_id,
                        recipient_task_identifier=recipient.task_identifier,
                        recipient_agent_name=recipient.agent_name,
                        content="",
                        question_identifier=question.identifier,
                    ))
                self._agent_questions.pop(question_identifier, None)
            elif question.recipient_task_id == task_id:
                self._fail_question(
                    question,
                    participant.task_identifier,
                    participant.agent_name,
                )

    def release_reserved_participant(self, task_identifier: str) -> None:
        """Fail queued questions when a delegated task never becomes active."""
        reservation = self._reserved_participants.pop(task_identifier, None)
        if reservation is None:
            return
        agent_name = reservation[1]
        for question in list(self._agent_questions.values()):
            if (
                not question.recipient_task_id
                and question.recipient_task_identifier == task_identifier
            ):
                self._fail_question(question, task_identifier, agent_name)

    def ask_agent(
        self,
        sender_task_id: str,
        recipient_task_identifier: str,
        content: str,
    ) -> dict[str, Any]:
        """Queue a question for an active or starting agent in the same A2A context."""
        sender = self._participants.get(sender_task_id)
        if sender is None:
            return {
                "code": "agent_sender_not_active",
                "message": "The sending agent is no longer active.",
            }
        recipient_task_id = self._participant_task_ids.get(recipient_task_identifier, "")
        recipient = self._participants.get(recipient_task_id)
        reservation = self._reserved_participants.get(recipient_task_identifier)
        if recipient is None and reservation is None:
            return {
                "code": "agent_not_active",
                "task_identifier": recipient_task_identifier,
                "message": "No active agent has that task identifier.",
            }
        if recipient is not None:
            recipient_context_id = recipient.context_id
            recipient_agent_name = recipient.agent_name
        else:
            assert reservation is not None
            recipient_context_id, recipient_agent_name = reservation
        if recipient_context_id != sender.context_id:
            return {
                "code": "agent_context_mismatch",
                "task_identifier": recipient_task_identifier,
                "message": "Agents may communicate only within the same task context.",
            }
        if recipient is not None and recipient.task_id == sender.task_id:
            return {
                "code": "agent_self_message",
                "message": "An agent cannot ask itself a peer question.",
            }
        message_identifier = f"message-{uuid.uuid4().hex}"
        question = _AgentQuestion(
            identifier=message_identifier,
            sender_task_id=sender_task_id,
            recipient_task_id=recipient_task_id,
            recipient_task_identifier=recipient_task_identifier,
            recipient_agent_name=recipient_agent_name,
            content=content,
        )
        self._agent_questions[message_identifier] = question
        if recipient is not None:
            self._deliver_question(question)
        return {
            "code": "agent_question_queued",
            "message_identifier": message_identifier,
            "task_identifier": recipient_task_identifier,
            "agent": recipient_agent_name,
            "message": "The question will be delivered at the agent's next opening.",
        }

    def respond_agent(
        self,
        responder_task_id: str,
        question_identifier: str,
        content: str,
    ) -> dict[str, Any]:
        """Deliver one response to the active agent that asked the question."""
        question = self._agent_questions.get(question_identifier)
        responder = self._participants.get(responder_task_id)
        if question is None:
            return {
                "code": "agent_question_not_found",
                "message": "No active agent question has that message identifier.",
            }
        if responder is None or question.recipient_task_id != responder_task_id:
            return {
                "code": "agent_response_not_allowed",
                "message": "This question was not addressed to the responding agent.",
            }
        recipient = self._participants.get(question.sender_task_id)
        if recipient is None:
            self._agent_questions.pop(question_identifier, None)
            return {
                "code": "agent_question_withdrawn",
                "message": "The requesting agent is no longer active.",
            }
        self._agent_questions.pop(question_identifier, None)
        recipient.runtime.enqueue_agent_message(AgentMessage(
            identifier=f"message-{uuid.uuid4().hex}",
            kind="response",
            sender_task_id=responder.task_id,
            sender_task_identifier=responder.task_identifier,
            sender_agent_name=responder.agent_name,
            recipient_task_id=recipient.task_id,
            recipient_task_identifier=recipient.task_identifier,
            recipient_agent_name=recipient.agent_name,
            content=content,
            question_identifier=question_identifier,
        ))
        return {
            "code": "agent_response_delivered",
            "message_identifier": question_identifier,
            "task_identifier": recipient.task_identifier,
            "agent": recipient.agent_name,
            "message": "The response was delivered to the requesting agent.",
        }

    def _deliver_question(self, question: _AgentQuestion) -> None:
        sender = self._participants.get(question.sender_task_id)
        recipient = self._participants.get(question.recipient_task_id)
        if sender is None or recipient is None:
            return
        recipient.runtime.enqueue_agent_message(AgentMessage(
            identifier=question.identifier,
            kind="question",
            sender_task_id=sender.task_id,
            sender_task_identifier=sender.task_identifier,
            sender_agent_name=sender.agent_name,
            recipient_task_id=recipient.task_id,
            recipient_task_identifier=recipient.task_identifier,
            recipient_agent_name=recipient.agent_name,
            content=question.content,
            question_identifier=question.identifier,
        ))

    def _fail_question(
        self,
        question: _AgentQuestion,
        unavailable_task_identifier: str,
        unavailable_agent_name: str,
    ) -> None:
        sender = self._participants.get(question.sender_task_id)
        self._agent_questions.pop(question.identifier, None)
        if sender is None:
            return
        sender.runtime.enqueue_agent_message(AgentMessage(
            identifier=f"message-{uuid.uuid4().hex}",
            kind="failed",
            sender_task_id="",
            sender_task_identifier=unavailable_task_identifier,
            sender_agent_name=unavailable_agent_name,
            recipient_task_id=sender.task_id,
            recipient_task_identifier=sender.task_identifier,
            recipient_agent_name=sender.agent_name,
            content="",
            question_identifier=question.identifier,
        ))

    async def cancel_delegated(self, agent_name: str, task_id: str) -> None:
        """Cancel a running agent's own A2A task after targeted cancellation.

        This is a no-op if the child already finished.
        """
        handler = self._handlers.get(agent_name)
        if handler is not None and task_id:
            with suppress(Exception):
                await handler.on_cancel_task(TaskIdParams(id=task_id))

    async def _remote_delegate(
        self, agent_name: str, prompt: str, context_id: str, attachments: Optional[list[dict]] = None
    ):
        """Delegate to an external A2A agent over the wire, yielding the same event
        vocabulary the local (in-process) delegate does — so the parent runtime and the
        agents panel cannot tell a remote agent from a local one. The task's file
        attachments ride as FileParts (signed URLs to this server); the remote agent's own
        token spend is opaque to us, so no usage is relayed.
        """
        assert self._remote_manager is not None
        remote_context = self._remote_contexts.get((context_id, agent_name))
        parts: list[Part] = [Part(root=TextPart(text=prompt))]
        if self._file_url_signer is not None:
            for attachment in (attachments or []):
                file_part = build_file_part(attachment, self._file_url_signer)
                if file_part is not None:
                    parts.append(file_part)
        traceparent = _telemetry.current_traceparent()
        message = Message(
            role=Role.user,
            parts=parts,
            message_id=uuid.uuid4().hex,
            context_id=remote_context,  # None on first contact; the remote assigns one
            metadata={"traceparent": traceparent} if traceparent else None,
        )
        child_task_id = ""
        final_task: Optional[Task] = None
        block_counter = 0

        def _relay_text(text: str):
            nonlocal block_counter
            block_counter += 1
            # Remote text carries no harness content-block identity, so synthesize a
            # stable-per-chunk one — the parent relay requires a block id and merges
            # only adjacent chunks sharing it.
            return DelegateRelay(event={
                PART_KIND: "text", "text": text, "block_id": f"remote:{agent_name}:{block_counter}",
            })

        try:
            async for event in self._remote_manager.send_message(agent_name, message):
                if isinstance(event, Message):
                    for part in event.parts:
                        root = part.root
                        if isinstance(root, TextPart) and root.text:
                            yield _relay_text(root.text)
                    continue
                task, update = event
                if isinstance(task, Task):
                    final_task = task
                    if not child_task_id:
                        child_task_id = task.id
                        if task.context_id:
                            self._remote_contexts[(context_id, agent_name)] = task.context_id
                        yield DelegateStarted(child_task_id=child_task_id)
                if isinstance(update, TaskStatusUpdateEvent) and update.status.message:
                    for part in update.status.message.parts:
                        root = part.root
                        if isinstance(root, TextPart) and root.text:
                            yield _relay_text(root.text)
        except Exception as exception:  # noqa: BLE001 — a remote failure ends the lane, never the parent
            logger.warning("Remote delegation to %r failed: %s", agent_name, exception)
            yield DelegateRelay(event={
                PART_KIND: "error", "message": f"Remote agent {agent_name} could not be reached.",
                "tool_name": "spawn_agent",
            })
        yield DelegateDone(
            child_task_id=child_task_id,
            task=final_task.model_dump(by_alias=True, exclude_none=True, mode="json", exclude={"history"}) if final_task else None,
        )

    def make_delegate(self, context_id: str):
        """Return a delegate bound to a context. Calling it invokes another agent
        as a related A2A task and yields its activity as it streams, ending with
        the child's final task."""

        async def delegate(
            agent_name: str,
            prompt: str,
            parent_task_id: str,
            read_only: Optional[bool] = None,
            depth: int = 1,
            working_directory: str = "",
            project_directory: str = "",
            lane_group_id: str = "",
            lane_step_id: str = "",
            attachments: Optional[list[dict]] = None,
            permission_mode: str = "",
        ):
            # A remote agent is reached over the wire; the in-process local path follows below.
            if self.is_remote_agent(agent_name):
                async for event in self._remote_delegate(agent_name, prompt, context_id, attachments):
                    yield event
                return
            handler = self._handlers.get(agent_name)
            if handler is None:
                yield DelegateDone()
                return
            turn_fields: dict = {Metadata.DELEGATED: True, Metadata.DEPTH: depth}
            if lane_group_id and lane_step_id:
                turn_fields[Metadata.AGENT_LANE_GROUP_ID] = lane_group_id
                turn_fields[Metadata.AGENT_LANE_STEP_ID] = lane_step_id
            if read_only is not None:
                turn_fields[Metadata.READ_ONLY] = bool(read_only)
            # The caller's approval grant for this delegated agent; the executor combines it with
            # the delegated agent card and clamps away bypass before the turn runs.
            if permission_mode:
                turn_fields[Metadata.PERMISSION_MODE] = permission_mode
            if project_directory:
                turn_fields[Metadata.WORKING_DIRECTORY] = project_directory
                turn_fields[Metadata.PROJECT_DIRECTORY] = project_directory
            if working_directory:
                turn_fields[Metadata.RUNTIME_WORKING_DIRECTORY] = working_directory
            envelope = _daisy_metadata_envelope(turn_fields)
            traceparent = _telemetry.current_traceparent()
            if traceparent:
                envelope["traceparent"] = traceparent
            message = Message(
                role=Role.user,
                parts=[Part(root=TextPart(text=prompt))],
                message_id=uuid.uuid4().hex,
                context_id=context_id,
                reference_task_ids=[parent_task_id] if parent_task_id else None,
                metadata=envelope,
            )
            child_task_id = ""
            async for event in handler.on_message_send_stream(MessageSendParams(message=message)):
                if isinstance(event, Task):
                    child_task_id = event.id
                    yield DelegateStarted(child_task_id=child_task_id)
                elif isinstance(event, TaskStatusUpdateEvent):
                    child_task_id = event.task_id or child_task_id
                    if event.status.message:
                        for part in event.status.message.parts:
                            root = part.root
                            # The child already speaks the unified vocabulary. Relay its
                            # panel-relevant events verbatim (the parent prepends the path);
                            # a plain TextPart becomes a `text` event, and a parked gate's
                            # permission/question prompt is relayed so the user can answer it.
                            # The rest (token_usage, compaction, steering) is the child's
                            # private bookkeeping and is not surfaced.
                            if isinstance(root, TextPart):
                                block_identifier = content_block_identifier(root.metadata)
                                if block_identifier is None:
                                    raise ValueError(
                                        "Relayed assistant text is missing its content-block identity."
                                    )
                                yield DelegateRelay(event={
                                    PART_KIND: "text",
                                    "text": root.text,
                                    "block_id": block_identifier,
                                })
                            elif isinstance(root, DataPart) and root.data.get(PART_KIND) == "token_usage":
                                # Not a panel event — the parent folds the child's spend
                                # into its separate agent token bucket.
                                yield DelegateUsage(event=dict(root.data))
                            elif isinstance(root, DataPart) and root.data.get(PART_KIND) in _RELAYABLE_CHILD_KINDS:
                                yield DelegateRelay(event=dict(root.data))
                elif isinstance(event, TaskArtifactUpdateEvent):
                    child_task_id = event.task_id or child_task_id
            final_task = await self._task_store.get(child_task_id) if child_task_id else None
            yield DelegateDone(
                child_task_id=child_task_id,
                # Hand back only the agent's deliverable (status + result artifact),
                # never its `history`. The history holds every relayed event — including
                # full web-search page text — and the parent serializes this task into
                # its own model context as the spawn_agent result; injecting the whole
                # transcript overflows the context window. The live agents panel is fed
                # by the separate streamed events above, so it is unaffected, and the UI
                # result card only reads the artifact.
                task=final_task.model_dump(by_alias=True, exclude_none=True, mode="json", exclude={"history"}) if final_task else None,
            )

        return delegate
