"""Shared runtime internals extracted from agent.py.

The helper functions, small dataclasses, and support classes the AgentRuntime concern
mixins reference. Kept in a leaf module (it imports only stable modules, never agent.py or
the mixin files) so the dependency graph is a clean DAG — agent_internals -> mixin files ->
agent.py — with no import cycle."""
from __future__ import annotations

from contextlib import suppress
from harness.core.agent_internals import _ToolGate
from harness.core.agent_internals import model_is_authorized
from harness.core.agent_messages import AgentMessage
from harness.core.configuration import AgentConfiguration
from harness.core.configuration import load_agent_configuration
from harness.core.turn_events import Relayed
from harness.core.turn_events import TurnEvent
from typing import Any
import asyncio
import uuid




class _DelegationMixin:

    async def _await_pending_answers(self, pending: list["_ToolGate"]) -> dict[str, Any]:
        """Park a delegated turn in place until the user answers each gate. The gates were
        surfaced to the panel by the executor from the SUSPENDED event; here the turn awaits
        the answers on per-gate futures (completed by the shared resolver's delegated
        routing), racing an abort that ends any outstanding gate fail-safe — a question
        declines, a permission denies. A top-level turn never reaches this: it returned at
        SUSPENDED to become a durable segment."""
        loop = asyncio.get_running_loop()
        futures: dict[str, "asyncio.Future"] = {}
        gate_by_request: dict[str, "_ToolGate"] = {}
        for gate in pending:
            future = loop.create_future()
            self._agent_permission_futures[gate.request_id] = future
            futures[gate.request_id] = future
            gate_by_request[gate.request_id] = gate
        answers: dict[str, Any] = {}
        abort_waiter = asyncio.ensure_future(self._abort_event.wait())
        try:
            for request_id, future in futures.items():
                await asyncio.wait({future, abort_waiter}, return_when=asyncio.FIRST_COMPLETED)
                if future.done():
                    answers[request_id] = future.result()
                else:
                    answers[request_id] = (
                        {"__declined__": True}
                        if gate_by_request[request_id].kind == "question"
                        else "deny"
                    )
        finally:
            abort_waiter.cancel()
            for request_id in futures:
                self._agent_permission_futures.pop(request_id, None)
        return answers

    def enqueue_agent_message(self, message: AgentMessage) -> None:
        """Queue a peer delivery for the next safe model boundary."""
        self._agent_messages.put_nowait(message)
        self._agent_message_available.set()

    def _drain_spawned_agent_events(self) -> list[TurnEvent]:
        """Every live agent event queued since the last drain. Yielded by the
        turn loop so a non-blocking spawned agent's activity streams to the panel
        while the parent works, and any backlog flushes on the wake turn."""
        events: list[TurnEvent] = []
        while not self._spawned_agent_events.empty():
            events.append(self._spawned_agent_events.get_nowait())
        return events

    def _drain_agent_messages(self) -> bool:
        """Deliver queued peer messages into the model conversation at a safe boundary."""
        delivered = False
        while not self._agent_messages.empty():
            message = self._agent_messages.get_nowait()
            delivered = True
            variables = {
                "message_identifier": message.question_identifier,
                "sender_agent_name": message.sender_agent_name,
                "sender_task_identifier": message.sender_task_identifier,
                "recipient_agent_name": message.recipient_agent_name,
                "recipient_task_identifier": message.recipient_task_identifier,
                "content": message.content,
            }
            if message.kind == "question":
                self._pending_agent_questions.add(message.question_identifier)
                template_name = "agent_question_received"
            elif message.kind == "response":
                self._outstanding_agent_questions.discard(message.question_identifier)
                template_name = "agent_response_received"
            elif message.kind == "failed":
                self._outstanding_agent_questions.discard(message.question_identifier)
                template_name = "agent_message_failed"
            else:
                self._pending_agent_questions.discard(message.question_identifier)
                template_name = "agent_question_withdrawn"
            self._conversation.append(
                self._harness_note_message(self._prompt_loader.load(template_name, variables))
            )
        if self._agent_messages.empty():
            self._agent_message_available.clear()
        return delivered

    async def _wait_for_agent_message_or_abort(self) -> bool:
        """Wait for an outstanding reply while keeping Stop immediately responsive."""
        message_waiter = asyncio.create_task(self._agent_message_available.wait())
        abort_waiter = asyncio.create_task(self._abort_event.wait())
        try:
            completed, _pending = await asyncio.wait(
                {message_waiter, abort_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            return message_waiter in completed and self._agent_message_available.is_set()
        finally:
            message_waiter.cancel()
            abort_waiter.cancel()
            with suppress(BaseException):
                await message_waiter
            with suppress(BaseException):
                await abort_waiter

    def _load_agent(self, name: str) -> AgentConfiguration:
        return load_agent_configuration(
            name,
            self._global_configuration.agent_directories_for(self._project_directory),
        )

    def _authorized_default_model(self) -> str:
        """The default agent's model when we hold credentials for it — the fallback a
        delegated agent uses when its own configured provider isn't authorized. Returns ""
        when even the default isn't authorized (nothing better to offer), leaving the
        original model in place."""
        try:
            default_configuration = self._load_agent(self._global_configuration.default_agent)
        except Exception:  # noqa: BLE001 — a missing/broken default just means no fallback
            return ""
        candidate = default_configuration.model_identifier
        if candidate and model_is_authorized(candidate, self._global_configuration):
            return candidate
        return ""

    def _build_agent_prompt(self, prompt: str, read_only: bool | None) -> str:
        mode = "read-only investigation" if read_only else "delegated task"
        return self._prompt_loader.load("agent_task", {
            "mode": mode,
            "prompt": prompt,
        })

    def _relay_child_event(self, delegated: dict, group_id: str, step_id: str) -> "TurnEvent | None":
        """Wrap one relayed agent event for re-emission. The child already speaks
        the unified vocabulary; we only prefix this agent's path segment so it renders
        at the right depth. Non-event control signals (started/done) carry no panel
        update and return ``None``."""
        if delegated.get("type") != "event":
            return None
        child_event = dict(delegated["event"])
        segment = {"group_id": group_id, "step_id": step_id}
        child_event["path"] = [segment, *child_event.get("path", [])]
        child_event["event_id"] = child_event.get("event_id") or uuid.uuid4().hex
        return Relayed(event=child_event)

    async def _publish_spawned_agent_event(self, event: Relayed) -> None:
        agent_event = event.event
        if self._agent_event_sink is not None:
            self._agent_event_sink(agent_event)
        await self._spawned_agent_events.put(event)

    async def _settle_agent_lane(self, group_id: str, step_id: str, state: str) -> None:
        """Publish exactly one terminal event for an agents-panel lane."""
        lane_key = (group_id, step_id)
        if lane_key in self._settled_agent_lanes:
            return
        self._settled_agent_lanes.add(lane_key)
        event = Relayed(event={
                "kind": "done",
                "path": [{"group_id": group_id, "step_id": step_id}],
                "state": state,
                "event_id": uuid.uuid4().hex,
            },
        )
        await self._publish_spawned_agent_event(event)
