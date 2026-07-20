"""AgentRunner — drives one spawned agent's turn to a serialized A2A Task.

In its own module because it instantiates the composed ``AgentRuntime`` (via a call-time
import): keeping it out of the mixin files and the shared ``agent_internals`` leaf lets the
tool mixin import ``AgentRunner`` without an import cycle back to ``AgentRuntime``."""
from __future__ import annotations

from a2a.types import Task
from a2a.types import TaskState
from daisy.core.configuration import AgentConfiguration
from daisy.core.configuration import GlobalConfiguration
from daisy.core.handoff import build_task
from daisy.core.handoff import serialize_task
from daisy.core.turn_events import Done
from daisy.core.turn_events import Error
from daisy.core.turn_events import TextChunk
from daisy.core.turn_events import TurnEvent
from typing import AsyncIterator
from typing import Optional
import json




class AgentRunner:
    def __init__(
        self,
        agent_configuration: AgentConfiguration,
        global_configuration: GlobalConfiguration,
        task_identifier: str,
        prompt: str,
        stream_progress: bool = True,
        read_only_override: Optional[bool] = None,
        working_directory: str = "",
        project_directory: str = "",
    ):
        self.task_identifier = task_identifier
        self.prompt = prompt
        self._stream_progress = stream_progress
        self._agent_name = agent_configuration.identifier
        # Imported at call time (not module scope) so this module can late-bind AgentRuntime
        # without an import cycle back to the composed class.
        from daisy.core.agent import AgentRuntime
        self._runtime = AgentRuntime(
            agent_configuration=agent_configuration,
            global_configuration=global_configuration,
            working_directory=working_directory,
            project_directory=project_directory,
            is_agent=True,
        )
        # An explicit override (from the spawning call or step)
        # wins over the agent profile's own permission_mode.
        if read_only_override is not None:
            self._runtime.set_read_only(read_only_override)

    async def run_stream(self, always_yield_text: bool = False) -> AsyncIterator[TurnEvent]:
        """Yield each event as the agent produces it, guaranteeing the run
        ends with a non-empty final report.

        The final DONE event always carries the artifact text in ``text``.
        """
        outcome = {"text": "", "stop_reason": "completed"}
        async for event in self._drain(self.prompt, always_yield_text, outcome):
            yield event

        # If the agent produced no deliverable (and was not cancelled), force one
        # by re-prompting for a self-contained conclusion. The conversation is
        # preserved, so the agent still has all the context it gathered.
        if not outcome["text"].strip() and outcome["stop_reason"] != "cancelled":
            conclusion_prompt = self._runtime._prompt_loader.load("conclusion", {})
            async for event in self._drain(conclusion_prompt, always_yield_text, outcome):
                yield event

        done_event = Done(text=outcome["text"], stop_reason=outcome["stop_reason"],
        )
        yield done_event

    async def _drain(
        self, prompt: str, always_yield_text: bool, outcome: dict,
    ) -> AsyncIterator[TurnEvent]:
        """Stream one turn through the inner runtime, forwarding events and
        recording ``(text, stop_reason)`` into ``outcome``. DONE events are
        swallowed so :meth:`run_stream` can emit a single terminal DONE."""
        async for event in self._runtime.stream(prompt):
            if isinstance(event, Done):
                if event.text.strip():
                    outcome["text"] = event.text
                outcome["stop_reason"] = event.stop_reason or outcome["stop_reason"]
                continue
            if isinstance(event, TextChunk):
                if self._stream_progress or always_yield_text:
                    yield event
                continue
            if isinstance(event, Error):
                outcome["stop_reason"] = "error"
                if not outcome["text"]:
                    outcome["text"] = event.message or "unknown"
            yield event

    async def run_to_task(self) -> Task:
        """Run the agent to completion and return its outcome as an A2A Task."""
        final_text = ""
        stop_reason = "completed"
        async for event in self.run_stream():
            if isinstance(event, Done):
                final_text = event.text or final_text
                stop_reason = event.stop_reason or stop_reason
        state = TaskState.completed
        if stop_reason == "error":
            state = TaskState.failed
        elif stop_reason == "cancelled":
            state = TaskState.canceled
        if not final_text.strip():
            final_text = "Agent produced no output."
            state = TaskState.failed
        return build_task(self.task_identifier, self._agent_name, state, final_text)

    async def run(self) -> str:
        """Return the agent's outcome as a serialized A2A Task (used by
        spawn_agent). The structured task — status + artifacts — is handed to the
        parent verbatim rather than flattened to a bare string."""
        task = await self.run_to_task()
        return json.dumps(serialize_task(task))
