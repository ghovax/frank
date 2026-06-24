"""Tests for the harness orchestrator and REPL event handling.

Uses a mock LLM to exercise the full stream pipeline without network calls.
Run with: uv run python -m pytest tests/ -v
"""

import asyncio
import io
import json
import re
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage

from harness.core.agent import (
    AgentOrchestrator,
    BackgroundTaskManager,
    ReasoningChatOpenAI,
    StreamEvent,
    TaskManager,
)
from harness.core.configuration import (
    AgentConfiguration,
    ApiConfiguration,
    BashToolConfiguration,
    GlobalConfiguration,
    PermissionEvaluator,
    SpawnAgentToolConfiguration,
    ToolsConfiguration,
)
from harness.tools.tools import bash_tasks, web_tasks


ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_PATTERN.sub("", text)


def make_global_configuration() -> GlobalConfiguration:
    return GlobalConfiguration(
        api=ApiConfiguration(
            endpoint="http://localhost:1234/v1",
            model="mock-model",
            api_key="test-key",
        ),
        default_agent="main",
        agents_directory="agents",
    )


def make_agent_configuration(name: str = "main") -> AgentConfiguration:
    return AgentConfiguration(
        name=name,
        model="mock-model",
        maximum_iterations=10,
        recursion_limit=3,
        stream_agent_progress=True,
        tools=ToolsConfiguration(
            bash=BashToolConfiguration(enabled=True),
            spawn_agent=SpawnAgentToolConfiguration(enabled=False),
        ),
        system_prompt="You are a test agent.",
    )


def make_text_chunk(text: str) -> AIMessageChunk:
    return AIMessageChunk(content=text)


def make_tool_call_chunk(
    tool_name: str, arguments: dict, call_id: str = "call_00_aB1cD2eF3gH4iJ5kL6mN7oP"
) -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{"name": tool_name, "args": arguments, "id": call_id}],
    )


def make_thinking_chunk(reasoning_text: str) -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        additional_kwargs={"reasoning_content": reasoning_text},
    )


class MockBoundLLM:
    """Mock LLM that returns predetermined response sequences.

    Each call to astream consumes the next response from the list.
    A response is a list of AIMessageChunk objects to yield.
    """

    def __init__(self, responses: list[list[AIMessageChunk]]):
        self._responses = list(responses)
        self._call_count = 0

    async def astream(self, messages):
        if self._call_count >= len(self._responses):
            raise RuntimeError(
                f"MockBoundLLM exhausted: called {self._call_count + 1} times "
                f"but only {len(self._responses)} responses configured"
            )
        chunks = self._responses[self._call_count]
        self._call_count += 1
        for chunk in chunks:
            yield chunk

    @property
    def call_count(self) -> int:
        return self._call_count


def make_orchestrator(
    responses: list[list[AIMessageChunk]],
    agent_configuration: AgentConfiguration | None = None,
    conversation: list | None = None,
) -> tuple[AgentOrchestrator, MockBoundLLM]:
    """Create an orchestrator wired to a mock LLM."""
    resolved_agent_configuration = agent_configuration or make_agent_configuration()
    global_configuration = make_global_configuration()

    orchestrator = AgentOrchestrator(
        agent_configuration=resolved_agent_configuration,
        global_configuration=global_configuration,
        conversation=conversation if conversation is not None else [],
    )

    mock_llm = MockBoundLLM(responses)
    orchestrator._bound_llm = mock_llm
    return orchestrator, mock_llm


async def collect_events(
    orchestrator: AgentOrchestrator, message: str
) -> list[StreamEvent]:
    """Drain all events from a single stream() call."""
    events = []
    async for event in orchestrator.stream(message):
        events.append(event)
    return events


def events_of_type(
    events: list[StreamEvent], event_type: StreamEvent.Type
) -> list[StreamEvent]:
    return [event for event in events if event.type == event_type]


def make_repl_for_testing():
    """Build a HarnessApp instance with mocked output for testing.

    Returns the app with a _printed list capturing all output.
    _write appends Rich markup, _write_md appends [MD]...[/MD] tagged text.
    """
    with patch("repl.HarnessApp.__init__", lambda self: None):
        from repl import HarnessApp

        repl = HarnessApp()

    repl._stream_buffer = ""
    repl._streaming = False
    repl._verbose_output = False
    repl._call_counter = 0
    repl._call_labels = {}
    repl._agent_tool_counts = {}
    repl._INTERNAL_KEYS = frozenset(
        {"code", "pid", "size", "task_identifier", "output_file", "query"}
    )
    repl._agent = "main"
    repl._shared_conversation = []
    repl._pending_permissions = {}
    repl._permission_future = None

    repl._printed: list[str] = []

    from rich.console import Console

    _test_buffer = io.StringIO()
    _test_console = Console(file=_test_buffer, force_terminal=True, width=120)

    def capture_write(markup):
        _test_buffer.seek(0)
        _test_buffer.truncate(0)
        from rich.text import Text
        _test_console.print(Text.from_markup(markup))
        rendered = _test_buffer.getvalue().rstrip("\n")
        if rendered:
            repl._printed.append(rendered)

    repl._write = capture_write

    def capture_write_md(text):
        if text:
            repl._printed.append(f"[MD]{text}[/MD]")

    repl._write_md = capture_write_md

    def flush_stream_buffer():
        text = repl._stream_buffer.strip()
        if text:
            capture_write_md(text)
            repl._stream_buffer = ""

    repl._flush_stream_buffer = flush_stream_buffer

    def noop_update():
        pass

    repl._update_status = noop_update
    repl._update_shortcuts = noop_update

    return repl


def printed_plain(repl) -> str:
    """Join all captured output and strip ANSI codes."""
    return strip_ansi("\n".join(repl._printed))


class TestTaskManager:
    """Tests for the TaskManager that tracks task state."""

    def test_add_tasks_assigns_sequential_identifiers(self):
        task_manager = TaskManager()
        identifiers = task_manager.add_tasks([
            {"description": "first task"},
            {"description": "second task"},
        ])
        assert identifiers == ["task-1", "task-2"]
        all_tasks = task_manager.to_dict_list()
        assert len(all_tasks) == 2
        assert all_tasks[0]["status"] == "pending"
        assert all_tasks[1]["status"] == "pending"

    def test_batch_update_applies_multiple_changes(self):
        task_manager = TaskManager()
        task_manager.add_tasks([
            {"description": "task A"},
            {"description": "task B"},
            {"description": "task C"},
        ])
        updated_identifiers = task_manager.update_tasks([
            {"task_id": "task-1", "status": "completed", "result": "done A"},
            {"task_id": "task-3", "status": "in_progress"},
        ])
        assert set(updated_identifiers) == {"task-1", "task-3"}
        tasks_by_id = {task["identifier"]: task for task in task_manager.to_dict_list()}
        assert tasks_by_id["task-1"]["status"] == "completed"
        assert tasks_by_id["task-1"]["result"] == "done A"
        assert tasks_by_id["task-2"]["status"] == "pending"
        assert tasks_by_id["task-3"]["status"] == "in_progress"

    def test_update_nonexistent_task_returns_empty(self):
        task_manager = TaskManager()
        task_manager.add_tasks([{"description": "only task"}])
        updated_identifiers = task_manager.update_tasks([
            {"task_id": "task-99", "status": "completed"}
        ])
        assert updated_identifiers == []

    def test_dependency_unblocking_on_completion(self):
        task_manager = TaskManager()
        task_manager.add_tasks([
            {"description": "blocker"},
            {"description": "waiter", "dependencies": ["task-1"]},
        ])

        task_manager.update_tasks([{"task_id": "task-2", "status": "blocked"}])
        tasks_by_id = {task["identifier"]: task for task in task_manager.to_dict_list()}
        assert tasks_by_id["task-2"]["status"] == "blocked"

        task_manager.update_tasks([{"task_id": "task-1", "status": "completed"}])
        tasks_by_id = {task["identifier"]: task for task in task_manager.to_dict_list()}
        assert tasks_by_id["task-2"]["status"] == "pending"

    def test_empty_update_list_returns_empty(self):
        task_manager = TaskManager()
        task_manager.add_tasks([{"description": "a task"}])
        updated_identifiers = task_manager.update_tasks([])
        assert updated_identifiers == []

    def test_render_json_empty_when_no_tasks(self):
        task_manager = TaskManager()
        assert task_manager.render_json() == ""

    def test_render_json_round_trips(self):
        task_manager = TaskManager()
        task_manager.add_tasks([{"description": "task one"}])
        rendered = task_manager.render_json()
        parsed = json.loads(rendered)
        assert len(parsed) == 1
        assert parsed[0]["description"] == "task one"

    def test_multiple_dependencies_all_must_complete(self):
        task_manager = TaskManager()
        task_manager.add_tasks([
            {"description": "dep A"},
            {"description": "dep B"},
            {"description": "waiter", "dependencies": ["task-1", "task-2"]},
        ])
        task_manager.update_tasks([{"task_id": "task-3", "status": "blocked"}])

        task_manager.update_tasks([{"task_id": "task-1", "status": "completed"}])
        tasks_by_id = {task["identifier"]: task for task in task_manager.to_dict_list()}
        assert tasks_by_id["task-3"]["status"] == "blocked"

        task_manager.update_tasks([{"task_id": "task-2", "status": "completed"}])
        tasks_by_id = {task["identifier"]: task for task in task_manager.to_dict_list()}
        assert tasks_by_id["task-3"]["status"] == "pending"


class TestOrchestratorStream:
    """Tests for the AgentOrchestrator.stream() event pipeline."""

    @pytest.mark.asyncio
    async def test_simple_text_response(self):
        """LLM returns plain text with no tool calls."""
        orchestrator, mock_llm = make_orchestrator([
            [make_text_chunk("Hello "), make_text_chunk("world!")],
        ])
        events = await collect_events(orchestrator, "hi")

        event_types = [event.type for event in events]
        assert StreamEvent.Type.STATUS in event_types
        assert StreamEvent.Type.TEXT_CHUNK in event_types
        assert StreamEvent.Type.DONE in event_types

        done_event = events_of_type(events, StreamEvent.Type.DONE)[0]
        assert done_event.data["stop_reason"] == "completed"
        assert "Hello world!" in done_event.data["text"]

    @pytest.mark.asyncio
    async def test_thinking_indicator_before_each_llm_call(self):
        """A STATUS thinking event is emitted before every LLM call."""
        orchestrator, mock_llm = make_orchestrator([
            [make_text_chunk("response")],
        ])
        events = await collect_events(orchestrator, "test")

        status_events = events_of_type(events, StreamEvent.Type.STATUS)
        status_codes = [event.data.get("code") for event in status_events]
        assert "thinking" in status_codes

    @pytest.mark.asyncio
    async def test_thinking_event_emitted_for_reasoning_content(self):
        """Reasoning content from the LLM produces THINKING events."""
        orchestrator, mock_llm = make_orchestrator([
            [make_thinking_chunk("Let me reason about this..."), make_text_chunk("answer")],
        ])
        events = await collect_events(orchestrator, "think hard")

        thinking_events = events_of_type(events, StreamEvent.Type.THINKING)
        assert len(thinking_events) == 1
        assert "reason" in thinking_events[0].data["text"]

    @pytest.mark.asyncio
    async def test_text_chunks_appear_before_tool_calls(self):
        """Text the LLM writes before a tool call is emitted before the TOOL_CALL event."""
        orchestrator, mock_llm = make_orchestrator([
            [
                make_text_chunk("Let me check. "),
                make_tool_call_chunk(
                    "write_tasks",
                    {"tasks": [{"description": "test task"}]},
                    "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                ),
            ],
            [make_text_chunk("All done.")],
        ])
        events = await collect_events(orchestrator, "do something")

        event_types = [event.type for event in events]
        text_indices = [
            index
            for index, event_type in enumerate(event_types)
            if event_type == StreamEvent.Type.TEXT_CHUNK
        ]
        tool_call_indices = [
            index
            for index, event_type in enumerate(event_types)
            if event_type == StreamEvent.Type.TOOL_CALL
        ]

        assert text_indices, "should have text chunks"
        assert tool_call_indices, "should have tool calls"
        assert min(text_indices) < min(tool_call_indices), (
            "text should appear before tool calls"
        )

    @pytest.mark.asyncio
    async def test_second_query_completes_normally(self):
        """A second call to stream() on the same orchestrator should work."""
        conversation = []
        orchestrator, mock_llm = make_orchestrator(
            [
                [make_text_chunk("first answer")],
                [make_text_chunk("second answer")],
            ],
            conversation=conversation,
        )

        first_events = await collect_events(orchestrator, "query 1")
        first_done = events_of_type(first_events, StreamEvent.Type.DONE)[0]
        assert first_done.data["stop_reason"] == "completed"
        assert "first answer" in first_done.data["text"]

        second_events = await collect_events(orchestrator, "query 2")
        second_done = events_of_type(second_events, StreamEvent.Type.DONE)[0]
        assert second_done.data["stop_reason"] == "completed"
        assert "second answer" in second_done.data["text"]

    @pytest.mark.asyncio
    async def test_second_query_emits_thinking_indicator(self):
        """The second query also gets a STATUS thinking event."""
        conversation = []
        orchestrator, mock_llm = make_orchestrator(
            [
                [make_text_chunk("first")],
                [make_text_chunk("second")],
            ],
            conversation=conversation,
        )

        await collect_events(orchestrator, "q1")
        second_events = await collect_events(orchestrator, "q2")

        status_codes = [
            event.data.get("code")
            for event in events_of_type(second_events, StreamEvent.Type.STATUS)
        ]
        assert "thinking" in status_codes

    @pytest.mark.asyncio
    async def test_write_tasks_produces_tasks_updated_event(self):
        """The write_tasks tool produces a TASKS_UPDATED event with created tasks."""
        orchestrator, mock_llm = make_orchestrator([
            [
                make_tool_call_chunk(
                    "write_tasks",
                    {"tasks": [{"description": "do X"}, {"description": "do Y"}]},
                    "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                )
            ],
            [make_text_chunk("created tasks")],
        ])
        events = await collect_events(orchestrator, "plan")

        task_events = events_of_type(events, StreamEvent.Type.TASKS_UPDATED)
        assert len(task_events) == 1
        created_tasks = task_events[0].data["tasks"]
        assert len(created_tasks) == 2
        assert created_tasks[0]["identifier"] == "task-1"

    @pytest.mark.asyncio
    async def test_update_tasks_batch_returns_only_changed_tasks(self):
        """Batch update_tasks produces an event containing only updated tasks."""
        orchestrator, mock_llm = make_orchestrator([
            [
                make_tool_call_chunk(
                    "write_tasks",
                    {"tasks": [{"description": "A"}, {"description": "B"}]},
                    "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                )
            ],
            [
                make_tool_call_chunk(
                    "update_tasks",
                    {
                        "updates": [
                            {"task_id": "task-1", "status": "completed", "result": "did A"},
                            {"task_id": "task-2", "status": "in_progress"},
                        ]
                    },
                    "call_01_bC7dE9fG1hI3jK5lM7nO9pQ",
                )
            ],
            [make_text_chunk("done")],
        ])
        events = await collect_events(orchestrator, "do stuff")

        task_events = events_of_type(events, StreamEvent.Type.TASKS_UPDATED)
        assert len(task_events) == 2

        update_event = task_events[1]
        updated_task_identifiers = {
            task["identifier"] for task in update_event.data["tasks"]
        }
        assert updated_task_identifiers == {"task-1", "task-2"}

    @pytest.mark.asyncio
    async def test_abort_mid_stream_cancels(self):
        """Setting the abort event mid-stream produces a cancelled DONE event.

        stream() clears the abort event at the start, so we set it after
        the first STATUS event is yielded.
        """
        orchestrator, mock_llm = make_orchestrator([
            [make_text_chunk("first"), make_text_chunk("second")],
        ])

        events = []
        async for event in orchestrator.stream("abort me"):
            events.append(event)
            if event.type == StreamEvent.Type.STATUS:
                orchestrator._abort_event.set()

        done_event = events_of_type(events, StreamEvent.Type.DONE)[0]
        assert done_event.data["stop_reason"] == "cancelled"

    @pytest.mark.asyncio
    async def test_conversation_grows_across_queries(self):
        """The conversation list accumulates messages from multiple queries."""
        conversation = []
        orchestrator, mock_llm = make_orchestrator(
            [
                [make_text_chunk("answer 1")],
                [make_text_chunk("answer 2")],
            ],
            conversation=conversation,
        )

        await collect_events(orchestrator, "q1")
        length_after_first_query = len(conversation)
        assert length_after_first_query >= 2

        await collect_events(orchestrator, "q2")
        assert len(conversation) > length_after_first_query

    @pytest.mark.asyncio
    async def test_tool_call_event_carries_arguments(self):
        """TOOL_CALL events include the tool name, arguments, and call ID."""
        orchestrator, mock_llm = make_orchestrator([
            [
                make_tool_call_chunk(
                    "write_tasks",
                    {"tasks": [{"description": "check files"}]},
                    "call_05_nO5pQ7rS9tU1vW3xY5zA7bC",
                )
            ],
            [make_text_chunk("done")],
        ])
        events = await collect_events(orchestrator, "plan")

        tool_call_events = events_of_type(events, StreamEvent.Type.TOOL_CALL)
        assert len(tool_call_events) == 1
        assert tool_call_events[0].data["name"] == "write_tasks"
        assert tool_call_events[0].data["id"] == "call_05_nO5pQ7rS9tU1vW3xY5zA7bC"
        assert "tasks" in tool_call_events[0].data["arguments"]

    @pytest.mark.asyncio
    async def test_unknown_tool_produces_error_event(self):
        """Calling a tool that doesn't exist yields an ERROR event."""
        orchestrator, mock_llm = make_orchestrator([
            [make_tool_call_chunk("nonexistent_tool", {}, "call_02_rS1tU3vW5xY7zA9bC1dE3fG")],
            [make_text_chunk("ok")],
        ])
        events = await collect_events(orchestrator, "try bad tool")

        error_events = events_of_type(events, StreamEvent.Type.ERROR)
        assert len(error_events) >= 1
        assert "nonexistent_tool" in error_events[0].data.get("message", "")

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_in_single_response(self):
        """When the LLM produces multiple tool calls in one response, all are executed."""
        orchestrator, mock_llm = make_orchestrator([
            [
                AIMessageChunk(
                    content="",
                    tool_calls=[
                        {"name": "write_tasks", "args": {"tasks": [{"description": "A"}]}, "id": "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA"},
                        {"name": "write_tasks", "args": {"tasks": [{"description": "B"}]}, "id": "call_01_bC7dE9fG1hI3jK5lM7nO9pQ"},
                    ],
                )
            ],
            [make_text_chunk("both created")],
        ])
        events = await collect_events(orchestrator, "create two")

        tool_call_events = events_of_type(events, StreamEvent.Type.TOOL_CALL)
        assert len(tool_call_events) == 2

        task_events = events_of_type(events, StreamEvent.Type.TASKS_UPDATED)
        assert len(task_events) == 2

    @pytest.mark.asyncio
    async def test_text_between_tool_rounds_is_emitted(self):
        """Text chunks emitted between different tool call rounds appear in the event stream."""
        orchestrator, mock_llm = make_orchestrator([
            [
                make_text_chunk("Before first tool. "),
                make_tool_call_chunk(
                    "write_tasks",
                    {"tasks": [{"description": "step 1"}]},
                    "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                ),
            ],
            [
                make_text_chunk("Between tool rounds. "),
                make_tool_call_chunk(
                    "update_tasks",
                    {"updates": [{"task_id": "task-1", "status": "completed", "result": "done"}]},
                    "call_01_bC7dE9fG1hI3jK5lM7nO9pQ",
                ),
            ],
            [make_text_chunk("After all tools.")],
        ])
        events = await collect_events(orchestrator, "multi-step")

        text_events = events_of_type(events, StreamEvent.Type.TEXT_CHUNK)
        all_text = "".join(event.data["text"] for event in text_events)
        assert "Before first tool." in all_text
        assert "Between tool rounds." in all_text
        assert "After all tools." in all_text

    @pytest.mark.asyncio
    async def test_status_thinking_emitted_before_every_llm_round(self):
        """Each LLM call (including after tool results) gets a STATUS thinking event."""
        orchestrator, mock_llm = make_orchestrator([
            [
                make_tool_call_chunk(
                    "write_tasks",
                    {"tasks": [{"description": "task"}]},
                    "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                )
            ],
            [make_text_chunk("done")],
        ])
        events = await collect_events(orchestrator, "go")

        thinking_statuses = [
            event
            for event in events_of_type(events, StreamEvent.Type.STATUS)
            if event.data.get("code") == "thinking"
        ]
        assert len(thinking_statuses) >= 2, (
            "Should have at least two thinking indicators: "
            "one before the first LLM call and one before the follow-up"
        )

    @pytest.mark.asyncio
    async def test_maximum_iterations_produces_done_event(self):
        """Exceeding maximum_iterations yields a DONE event with the right stop reason."""
        limited_config = make_agent_configuration()
        limited_config.maximum_iterations = 2

        orchestrator, mock_llm = make_orchestrator(
            [
                [
                    make_tool_call_chunk(
                        "write_tasks",
                        {"tasks": [{"description": "A"}]},
                        "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                    )
                ],
                [
                    make_tool_call_chunk(
                        "write_tasks",
                        {"tasks": [{"description": "B"}]},
                        "call_01_bC7dE9fG1hI3jK5lM7nO9pQ",
                    )
                ],
                [
                    make_tool_call_chunk(
                        "write_tasks",
                        {"tasks": [{"description": "C"}]},
                        "call_3",
                    )
                ],
            ],
            agent_configuration=limited_config,
        )
        events = await collect_events(orchestrator, "keep going")

        done_event = events_of_type(events, StreamEvent.Type.DONE)[0]
        assert done_event.data["stop_reason"] == "maximum_iterations"

    @pytest.mark.asyncio
    async def test_empty_text_chunk_not_emitted(self):
        """Empty content chunks from the LLM should not produce TEXT_CHUNK events."""
        orchestrator, mock_llm = make_orchestrator([
            [AIMessageChunk(content=""), make_text_chunk("actual text")],
        ])
        events = await collect_events(orchestrator, "test")

        text_events = events_of_type(events, StreamEvent.Type.TEXT_CHUNK)
        assert all(event.data["text"] for event in text_events), (
            "No TEXT_CHUNK events should have empty text"
        )


class TestBackgroundTaskManager:
    """Tests for the BackgroundTaskManager polling and draining."""

    def test_no_results_initially(self):
        manager = BackgroundTaskManager()
        manager.poll()
        assert not manager.has_results()
        assert manager.drain_results() == []

    def test_has_pending_reflects_active_tasks(self):
        manager = BackgroundTaskManager()
        assert not manager.has_pending()

    @pytest.mark.asyncio
    async def test_completed_bash_task_is_collected(self):
        async def fake_task():
            return '{"code": "bash_completed", "output": "hello"}'

        identifier, output_path = bash_tasks.start(fake_task())
        await asyncio.sleep(0.05)

        manager = BackgroundTaskManager()
        manager.poll()
        assert manager.has_results()
        results = manager.drain_results()
        assert len(results) == 1
        tool_name, task_identifier, result = results[0]
        assert tool_name == "bash"
        assert task_identifier == identifier


class TestREPLEventHandler:
    """Tests for the REPL's _handle_event display logic, without a real terminal."""

    @pytest.mark.asyncio
    async def test_status_thinking_shows_indicator_on_first_call(self):
        repl = make_repl_for_testing()
        await repl._handle_event(StreamEvent(StreamEvent.Type.STATUS, code="thinking"))
        output = printed_plain(repl)
        assert "thinking" in output

    @pytest.mark.asyncio
    async def test_status_thinking_silent_after_tool_calls(self):
        """After tool calls have been made, thinking status is suppressed."""
        repl = make_repl_for_testing()
        repl._call_counter = 1
        await repl._handle_event(StreamEvent(StreamEvent.Type.STATUS, code="thinking"))
        assert not repl._printed

    @pytest.mark.asyncio
    async def test_status_unknown_code_is_silent(self):
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(StreamEvent.Type.STATUS, code="something_else")
        )
        assert not repl._printed

    @pytest.mark.asyncio
    async def test_text_chunk_buffers_without_printing(self):
        repl = make_repl_for_testing()
        await repl._handle_event(StreamEvent(StreamEvent.Type.TEXT_CHUNK, text="hello"))
        assert repl._stream_buffer == "hello"
        assert not any("hello" in line for line in repl._printed)

    @pytest.mark.asyncio
    async def test_text_chunks_accumulate_in_buffer(self):
        repl = make_repl_for_testing()
        await repl._handle_event(StreamEvent(StreamEvent.Type.TEXT_CHUNK, text="hello "))
        await repl._handle_event(StreamEvent(StreamEvent.Type.TEXT_CHUNK, text="world"))
        assert repl._stream_buffer == "hello world"

    @pytest.mark.asyncio
    async def test_tool_call_flushes_buffer_before_displaying(self):
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(StreamEvent.Type.TEXT_CHUNK, text="I'll check. ")
        )
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_CALL,
                name="bash",
                id="call_10_zA1bC3dE5fG7hI9jK1lM3nO",
                arguments={"command": "ls"},
            )
        )
        assert any("I'll check." in line for line in repl._printed), (
            f"Buffer should have been flushed before tool call. Printed: {repl._printed}"
        )
        assert repl._stream_buffer == ""

    @pytest.mark.asyncio
    async def test_tool_call_with_empty_buffer_does_not_flush_garbage(self):
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_CALL,
                name="bash",
                id="call_10_zA1bC3dE5fG7hI9jK1lM3nO",
                arguments={"command": "ls"},
            )
        )
        output = printed_plain(repl)
        assert "[MD]" not in output

    @pytest.mark.asyncio
    async def test_done_renders_remaining_buffer_as_markdown(self):
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(StreamEvent.Type.TEXT_CHUNK, text="final text")
        )
        await repl._handle_event(
            StreamEvent(StreamEvent.Type.DONE, text="final text", stop_reason="completed")
        )
        assert any("final text" in line for line in repl._printed)
        assert repl._stream_buffer == ""
        assert repl._streaming is False

    @pytest.mark.asyncio
    async def test_done_without_streaming_renders_event_text(self):
        """When no text was streamed, DONE renders the text from the event data."""
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.DONE,
                text="direct response text",
                stop_reason="completed",
            )
        )
        assert any("direct response text" in line for line in repl._printed)

    @pytest.mark.asyncio
    async def test_done_cancelled_shows_cancellation_message(self):
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(StreamEvent.Type.DONE, text="", stop_reason="cancelled")
        )
        output = printed_plain(repl)
        assert "cancelled" in output

    @pytest.mark.asyncio
    async def test_done_maximum_iterations_shows_warning(self):
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(StreamEvent.Type.DONE, text="", stop_reason="maximum_iterations")
        )
        output = printed_plain(repl)
        assert "max iterations" in output

    @pytest.mark.asyncio
    async def test_done_resets_state_for_next_query(self):
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(StreamEvent.Type.TEXT_CHUNK, text="some text")
        )
        assert repl._streaming is True

        await repl._handle_event(
            StreamEvent(StreamEvent.Type.DONE, text="some text", stop_reason="completed")
        )
        assert repl._streaming is False
        assert repl._stream_buffer == ""

    @pytest.mark.asyncio
    async def test_agent_done_format(self):
        repl = make_repl_for_testing()
        repl._agent_tool_counts["research"] = {"bash": 3, "web_search": 1}
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.AGENT_DONE,
                agent_id="research",
                step_id="research",
            )
        )
        output = printed_plain(repl)
        assert "[done] research" in output

    @pytest.mark.asyncio
    async def test_agent_done_cleans_up_tool_counts(self):
        repl = make_repl_for_testing()
        repl._agent_tool_counts["step-1"] = {"bash": 2}
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.AGENT_DONE, agent_id="step-1", step_id="step-1"
            )
        )
        assert "step-1" not in repl._agent_tool_counts

    @pytest.mark.asyncio
    async def test_tasks_updated_shows_compact_per_task_lines(self):
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TASKS_UPDATED,
                id="call_10_zA1bC3dE5fG7hI9jK1lM3nO",
                tasks=[
                    {
                        "identifier": "task-1",
                        "status": "completed",
                        "description": "do X",
                        "result": "done",
                    },
                    {
                        "identifier": "task-2",
                        "status": "in_progress",
                        "description": "do Y",
                        "result": "",
                    },
                ],
                result_message="Updated tasks",
            )
        )
        output = printed_plain(repl)
        assert "task-1" in output
        assert "task-2" in output
        assert "completed" in output or "done" in output

    @pytest.mark.asyncio
    async def test_tasks_updated_empty_list_prints_nothing(self):
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TASKS_UPDATED,
                id="call_10_zA1bC3dE5fG7hI9jK1lM3nO",
                tasks=[],
                result_message="No matching tasks found.",
            )
        )
        assert not repl._printed

    @pytest.mark.asyncio
    async def test_error_event_shows_tool_and_message(self):
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.ERROR,
                id="call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                tool="bash",
                message="command not found",
            )
        )
        output = printed_plain(repl)
        assert "error" in output
        assert "command not found" in output

    @pytest.mark.asyncio
    async def test_thinking_event_shows_text(self):
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(StreamEvent.Type.THINKING, text="reasoning about the problem")
        )
        output = printed_plain(repl)
        assert "reasoning about the problem" in output

    @pytest.mark.asyncio
    async def test_background_started_shows_agent_name(self):
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.BACKGROUND_STARTED,
                id="call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                agent="explorer",
            )
        )
        output = printed_plain(repl)
        assert "spawned" in output
        assert "explorer" in output

    @pytest.mark.asyncio
    async def test_agent_tool_call_increments_counts(self):
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.AGENT_TOOL_CALL,
                step_id="research",
                name="bash",
            )
        )
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.AGENT_TOOL_CALL,
                step_id="research",
                name="bash",
            )
        )
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.AGENT_TOOL_CALL,
                step_id="research",
                name="web_search",
            )
        )
        counts = repl._agent_tool_counts["research"]
        assert counts["bash"] == 2
        assert counts["web_search"] == 1

    @pytest.mark.asyncio
    async def test_tool_result_started_codes_silenced(self):
        """Results with _started codes are silenced (only for slow background tasks)."""
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_RESULT,
                name="bash",
                id="call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                result={"code": "bash_started", "task_identifier": "bg-123"},
            )
        )
        assert not repl._printed

    @pytest.mark.asyncio
    async def test_tool_result_shown_for_completed_codes(self):
        """Results with _completed codes show at least a header."""
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_RESULT,
                name="bash",
                id="call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                result={"code": "bash_completed", "output": "file1.txt"},
            )
        )
        output = printed_plain(repl)
        assert "result" in output
        assert "bash" in output

    @pytest.mark.asyncio
    async def test_tool_result_with_message_shows_error(self):
        """Results containing a message field display the message in red."""
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_RESULT,
                name="bash",
                id="call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                result={"message": "permission denied"},
            )
        )
        output = printed_plain(repl)
        assert "result" in output
        assert "permission denied" in output

    @pytest.mark.asyncio
    async def test_tool_result_verbose_shows_extra_fields(self):
        """In verbose mode, extra fields beyond code/output/message are displayed."""
        repl = make_repl_for_testing()
        repl._verbose_output = True
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_RESULT,
                name="web_search",
                id="call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                result={"status": "ok", "count": 3},
            )
        )
        output = printed_plain(repl)
        assert "result" in output
        assert "web_search" in output

    @pytest.mark.asyncio
    async def test_tool_result_compact_hides_body(self):
        """In compact mode, only the header is shown for dict results without a message."""
        repl = make_repl_for_testing()
        repl._verbose_output = False
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_RESULT,
                name="web_search",
                id="call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                result={"status": "ok", "count": 3},
            )
        )
        output = printed_plain(repl)
        assert "result" in output
        assert "web_search" in output
        assert "count" not in output

    @pytest.mark.asyncio
    async def test_tool_result_string_shown_in_verbose_mode(self):
        """String results are displayed line by line in verbose mode."""
        repl = make_repl_for_testing()
        repl._verbose_output = True
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_RESULT,
                name="custom_tool",
                id="call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                result="line one\nline two",
            )
        )
        output = printed_plain(repl)
        assert "result" in output
        assert "line one" in output

    @pytest.mark.asyncio
    async def test_tool_result_string_hidden_in_compact_mode(self):
        """String result bodies are hidden in compact mode."""
        repl = make_repl_for_testing()
        repl._verbose_output = False
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_RESULT,
                name="custom_tool",
                id="call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                result="line one\nline two",
            )
        )
        output = printed_plain(repl)
        assert "result" in output
        assert "line one" not in output

    @pytest.mark.asyncio
    async def test_call_labels_assigned_sequentially(self):
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_CALL,
                name="bash",
                id="call_06_dE9fG1hI3jK5lM7nO9pQ1rS",
                arguments={"command": "ls"},
            )
        )
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_CALL,
                name="bash",
                id="call_07_tU3vW5xY7zA9bC1dE3fG5hI",
                arguments={"command": "pwd"},
            )
        )
        assert repl._call_labels["call_06_dE9fG1hI3jK5lM7nO9pQ1rS"] == "#1"
        assert repl._call_labels["call_07_tU3vW5xY7zA9bC1dE3fG5hI"] == "#2"

    @pytest.mark.asyncio
    async def test_background_task_inherits_call_label(self):
        """When a tool result contains a task_identifier, it maps to the original call label."""
        repl = make_repl_for_testing()
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_CALL,
                name="web_search",
                id="call_08_jK7lM9nO1pQ3rS5tU7vW9xY",
                arguments={"query": "test"},
            )
        )
        assert repl._call_labels["call_08_jK7lM9nO1pQ3rS5tU7vW9xY"] == "#1"

        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_RESULT,
                name="web_search",
                id="call_08_jK7lM9nO1pQ3rS5tU7vW9xY",
                result={"code": "web_search_started", "task_identifier": "search-4a7b8c9d0e1f"},
            )
        )
        assert repl._call_labels.get("search-4a7b8c9d0e1f") == "#1"

        repl._printed.clear()
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_RESULT,
                name="web_search",
                task_id="search-4a7b8c9d0e1f",
                result={"code": "web_search_completed", "results": []},
            )
        )
        output = printed_plain(repl)
        assert "#1" in output
        assert "result" in output


class TestEventSequenceIntegration:
    """Integration tests verifying full event sequences through the orchestrator."""

    @pytest.mark.asyncio
    async def test_text_then_tool_then_text_flow(self):
        """Full flow: text -> tool call -> tool result -> text -> done."""
        orchestrator, mock_llm = make_orchestrator([
            [
                make_text_chunk("I'll look into that. "),
                make_tool_call_chunk(
                    "write_tasks",
                    {"tasks": [{"description": "check files"}]},
                    "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                ),
            ],
            [
                make_text_chunk("Here's what I found. "),
                make_tool_call_chunk(
                    "update_tasks",
                    {
                        "updates": [
                            {
                                "task_id": "task-1",
                                "status": "completed",
                                "result": "all good",
                            }
                        ]
                    },
                    "call_01_bC7dE9fG1hI3jK5lM7nO9pQ",
                ),
            ],
            [make_text_chunk("Everything looks good.")],
        ])

        events = await collect_events(orchestrator, "check my project")
        event_types = [event.type for event in events]

        assert StreamEvent.Type.TEXT_CHUNK in event_types
        assert StreamEvent.Type.TOOL_CALL in event_types
        assert StreamEvent.Type.TASKS_UPDATED in event_types
        assert StreamEvent.Type.DONE in event_types

        done_event = events_of_type(events, StreamEvent.Type.DONE)[0]
        assert done_event.data["stop_reason"] == "completed"

        task_events = events_of_type(events, StreamEvent.Type.TASKS_UPDATED)
        assert len(task_events) == 2

        update_event = task_events[1]
        updated_tasks = update_event.data["tasks"]
        assert len(updated_tasks) == 1
        assert updated_tasks[0]["identifier"] == "task-1"
        assert updated_tasks[0]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_three_consecutive_queries_all_complete(self):
        """Three queries in a row on the same orchestrator all produce completed DONE events."""
        conversation = []
        orchestrator, mock_llm = make_orchestrator(
            [
                [make_text_chunk("one")],
                [make_text_chunk("two")],
                [make_text_chunk("three")],
            ],
            conversation=conversation,
        )

        for query_number, expected_text in enumerate(["one", "two", "three"], 1):
            events = await collect_events(orchestrator, f"query {query_number}")
            done_event = events_of_type(events, StreamEvent.Type.DONE)[0]
            assert done_event.data["stop_reason"] == "completed"
            assert expected_text in done_event.data["text"]

    @pytest.mark.asyncio
    async def test_event_ordering_status_before_content(self):
        """STATUS events always appear before any content events in each round."""
        orchestrator, mock_llm = make_orchestrator([
            [make_text_chunk("hello")],
        ])
        events = await collect_events(orchestrator, "hi")

        first_status_index = next(
            index
            for index, event in enumerate(events)
            if event.type == StreamEvent.Type.STATUS
        )
        first_content_index = next(
            index
            for index, event in enumerate(events)
            if event.type in (StreamEvent.Type.TEXT_CHUNK, StreamEvent.Type.TOOL_CALL)
        )
        assert first_status_index < first_content_index

    @pytest.mark.asyncio
    async def test_conversation_messages_are_correct_types(self):
        """After a query, the conversation contains the right message types."""
        from langchain_core.messages import HumanMessage, AIMessage

        conversation = []
        orchestrator, mock_llm = make_orchestrator(
            [[make_text_chunk("response")]],
            conversation=conversation,
        )
        await collect_events(orchestrator, "question")

        message_types = [type(message).__name__ for message in conversation]
        assert "HumanMessage" in message_types
        assert "AIMessageChunk" in message_types

    @pytest.mark.asyncio
    async def test_tool_result_messages_added_to_conversation(self):
        """After a tool call, ToolMessage entries appear in the conversation."""
        from langchain_core.messages import ToolMessage

        conversation = []
        orchestrator, mock_llm = make_orchestrator(
            [
                [
                    make_tool_call_chunk(
                        "write_tasks",
                        {"tasks": [{"description": "test"}]},
                        "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                    )
                ],
                [make_text_chunk("done")],
            ],
            conversation=conversation,
        )
        await collect_events(orchestrator, "go")

        tool_messages = [
            message for message in conversation if isinstance(message, ToolMessage)
        ]
        assert len(tool_messages) >= 1

    @pytest.mark.asyncio
    async def test_full_repl_event_sequence_renders_correctly(self):
        """Simulate a full event sequence through the REPL handler and verify output."""
        repl = make_repl_for_testing()

        await repl._handle_event(StreamEvent(StreamEvent.Type.STATUS, code="thinking"))
        await repl._handle_event(
            StreamEvent(StreamEvent.Type.TEXT_CHUNK, text="Let me think. ")
        )
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TOOL_CALL,
                name="write_tasks",
                id="call_10_zA1bC3dE5fG7hI9jK1lM3nO",
                arguments={"tasks": [{"description": "analyze code"}]},
            )
        )
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.TASKS_UPDATED,
                id="call_10_zA1bC3dE5fG7hI9jK1lM3nO",
                tasks=[
                    {"identifier": "task-1", "status": "pending", "description": "analyze code", "result": ""},
                ],
                result_message="Created tasks: task-1",
            )
        )
        await repl._handle_event(StreamEvent(StreamEvent.Type.STATUS, code="thinking"))
        await repl._handle_event(
            StreamEvent(StreamEvent.Type.TEXT_CHUNK, text="Analysis complete.")
        )
        await repl._handle_event(
            StreamEvent(
                StreamEvent.Type.DONE,
                text="Analysis complete.",
                stop_reason="completed",
            )
        )

        output = printed_plain(repl)
        assert "thinking" in output
        assert "Let me think." in output
        assert "write_tasks" in output
        assert "task-1" in output
        assert "Analysis complete." in output


class TestMarkdownRendering:
    """Tests for the REPL's markdown rendering, especially HTML tag handling."""

    def _render(self, text: str) -> str:
        """Render text through the REPL's markdown pipeline and return plain text."""
        from repl import HarnessApp

        escaped = HarnessApp._escape_html_tags(text)

        from rich.console import Console
        from rich.markdown import Markdown

        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=True, width=100)
        console.print(Markdown(escaped))
        return strip_ansi(buffer.getvalue()).strip()

    def test_html_tags_in_plain_text_are_preserved(self):
        """HTML-like tags outside code blocks should render as visible text, not be stripped."""
        text = "<tool_calls>\n<invoke name=\"bash\">\n<parameter name=\"cmd\">echo hi</parameter>\n</invoke>\n</tool_calls>"
        rendered = self._render(text)
        assert "<invoke" in rendered
        assert "<parameter" in rendered
        assert "echo hi" in rendered

    def test_code_block_tags_are_untouched(self):
        """Tags inside fenced code blocks should render normally."""
        text = "```\n<div>hello</div>\n```"
        rendered = self._render(text)
        assert "<div>" in rendered
        assert "hello" in rendered

    def test_inline_code_tags_are_untouched(self):
        """Tags inside inline code should render normally."""
        text = "Use `<div>` for containers"
        rendered = self._render(text)
        assert "<div>" in rendered

    def test_blockquotes_are_preserved(self):
        """Markdown blockquotes using > should still render."""
        text = "> This is a quote"
        rendered = self._render(text)
        assert "This is a quote" in rendered

    def test_less_than_in_comparisons_preserved(self):
        """Less-than signs in comparisons should not be escaped."""
        text = "if a < b then x < 10"
        rendered = self._render(text)
        assert "a < b" in rendered

    def test_bold_and_italic_render(self):
        """Standard markdown formatting should still work."""
        text = "**bold** and *italic*"
        rendered = self._render(text)
        assert "bold" in rendered
        assert "italic" in rendered

    def test_unclosed_backtick_does_not_truncate(self):
        """Text with an unclosed backtick should still render fully."""
        text = "The block starts with a `\nand continues here"
        rendered = self._render(text)
        assert "starts with" in rendered
        assert "continues here" in rendered

    def test_unclosed_code_block_does_not_truncate(self):
        """Text with an unclosed fenced code block should still render."""
        text = "Before\n```\nsome code\nno closing fence"
        rendered = self._render(text)
        assert "Before" in rendered

    def test_mixed_tags_and_code(self):
        """Tags outside code are escaped, tags inside code are literal."""
        text = "See <invoke name=\"x\">:\n```\n<div>code</div>\n```"
        rendered = self._render(text)
        assert "<invoke" in rendered
        assert "<div>" in rendered

    def test_empty_text_renders_without_error(self):
        text = ""
        rendered = self._render(text)
        assert rendered == "" or rendered.strip() == ""

    def test_plain_text_without_markdown_renders(self):
        text = "Just a simple sentence."
        rendered = self._render(text)
        assert "Just a simple sentence." in rendered


class TestReasoningPreservation:
    """Tests that reasoning_content survives across conversation turns."""

    def _make_llm(self) -> ReasoningChatOpenAI:
        return ReasoningChatOpenAI(
            model="test-model",
            base_url="http://localhost:1234/v1",
            api_key="test",
        )

    def test_reasoning_content_included_in_payload(self):
        """Assistant messages with reasoning_content include it in the API payload."""
        llm = self._make_llm()
        messages = [
            HumanMessage(content="question"),
            AIMessageChunk(
                content="answer",
                additional_kwargs={"reasoning_content": "my reasoning"},
            ),
            HumanMessage(content="follow up"),
        ]
        payload = llm._get_request_payload(messages)
        assistant_message = payload["messages"][1]
        assert assistant_message["role"] == "assistant"
        assert assistant_message["reasoning_content"] == "my reasoning"

    def test_messages_without_reasoning_are_unaffected(self):
        """Messages without reasoning_content don't get a spurious field."""
        llm = self._make_llm()
        messages = [
            HumanMessage(content="hello"),
            AIMessageChunk(content="hi"),
            HumanMessage(content="bye"),
        ]
        payload = llm._get_request_payload(messages)
        for payload_message in payload["messages"]:
            assert "reasoning_content" not in payload_message

    def test_reasoning_aligned_with_system_message_present(self):
        """reasoning_content maps to the correct message even with a system message."""
        llm = self._make_llm()
        messages = [
            SystemMessage(content="system prompt"),
            HumanMessage(content="q1"),
            AIMessageChunk(
                content="a1",
                additional_kwargs={"reasoning_content": "thought 1"},
            ),
            HumanMessage(content="q2"),
            AIMessageChunk(
                content="a2",
                additional_kwargs={"reasoning_content": "thought 2"},
            ),
        ]
        payload = llm._get_request_payload(messages)
        payload_messages = payload["messages"]

        assert "reasoning_content" not in payload_messages[0]
        assert "reasoning_content" not in payload_messages[1]
        assert payload_messages[2]["reasoning_content"] == "thought 1"
        assert "reasoning_content" not in payload_messages[3]
        assert payload_messages[4]["reasoning_content"] == "thought 2"

    def test_reasoning_survives_chunk_accumulation(self):
        """reasoning_content accumulates correctly across streamed chunks."""
        chunk1 = AIMessageChunk(
            content="",
            additional_kwargs={"reasoning_content": "First part. "},
        )
        chunk2 = AIMessageChunk(
            content="",
            additional_kwargs={"reasoning_content": "Second part."},
        )
        chunk3 = AIMessageChunk(content="The answer.")

        accumulated = chunk1 + chunk2 + chunk3
        assert accumulated.content == "The answer."
        assert accumulated.additional_kwargs["reasoning_content"] == "First part. Second part."

    def test_reasoning_preserved_in_conversation_across_turns(self):
        """reasoning_content in conversation messages is included when building the payload."""
        llm = self._make_llm()

        chunk1 = AIMessageChunk(
            content="",
            additional_kwargs={"reasoning_content": "thinking..."},
        )
        chunk2 = AIMessageChunk(content="response text")
        accumulated_response = chunk1 + chunk2

        conversation = [
            HumanMessage(content="first question"),
            accumulated_response,
            HumanMessage(content="second question"),
        ]

        payload = llm._get_request_payload(conversation)
        assistant_payload = payload["messages"][1]
        assert assistant_payload["content"] == "response text"
        assert assistant_payload["reasoning_content"] == "thinking..."

    @pytest.mark.asyncio
    async def test_orchestrator_preserves_reasoning_in_conversation(self):
        """The orchestrator stores reasoning_content in conversation messages."""
        conversation = []
        orchestrator, mock_llm = make_orchestrator(
            [
                [
                    make_thinking_chunk("Let me reason about this..."),
                    make_text_chunk("Here is my answer."),
                ],
            ],
            conversation=conversation,
        )

        await collect_events(orchestrator, "question")

        ai_messages = [
            message for message in conversation
            if isinstance(message, (AIMessageChunk,))
        ]
        assert len(ai_messages) >= 1
        reasoning = ai_messages[0].additional_kwargs.get("reasoning_content", "")
        assert "reason" in reasoning

    @pytest.mark.asyncio
    async def test_reasoning_available_on_second_turn(self):
        """On the second LLM call, the first turn's reasoning is in the conversation."""
        conversation = []
        orchestrator, mock_llm = make_orchestrator(
            [
                [
                    make_thinking_chunk("Deep reasoning here."),
                    make_text_chunk("Answer 1."),
                ],
                [make_text_chunk("Answer 2.")],
            ],
            conversation=conversation,
        )

        await collect_events(orchestrator, "q1")

        ai_messages = [
            message for message in conversation
            if isinstance(message, (AIMessageChunk,))
        ]
        assert ai_messages[0].additional_kwargs.get("reasoning_content") == "Deep reasoning here."

        await collect_events(orchestrator, "q2")
        assert len(conversation) >= 4


class TestBashPermissionPatterns:
    """Tests for the bash permission pattern matching and evaluation logic.

    These are pure unit tests — no commands are executed.
    """

    def _make_bash_config(self, permissions: dict[str, str]) -> BashToolConfiguration:
        return BashToolConfiguration(
            enabled=True,
            background_allowed=True,
            permissions=permissions,
        )

    def test_no_permissions_allows_everything(self):
        config = self._make_bash_config({})
        assert config.evaluate_permission("rm -rf /") == "allow"
        assert config.evaluate_permission("sudo reboot") == "allow"
        assert config.evaluate_permission("echo hello") == "allow"

    def test_wildcard_deny_matches_command_at_start(self):
        config = self._make_bash_config({"sudo *": "deny"})
        assert config.evaluate_permission("sudo reboot") == "deny"
        assert config.evaluate_permission("sudo rm -rf /") == "deny"
        assert config.evaluate_permission("sudo apt install vim") == "deny"

    def test_wildcard_ask_matches_command_at_start(self):
        config = self._make_bash_config({"rm *": "ask"})
        assert config.evaluate_permission("rm file.txt") == "ask"
        assert config.evaluate_permission("rm -rf /tmp/junk") == "ask"

    def test_wildcard_matches_command_after_double_ampersand(self):
        config = self._make_bash_config({"rm *": "deny"})
        assert config.evaluate_permission("echo hello && rm -rf /") == "deny"

    def test_wildcard_matches_command_after_semicolon(self):
        config = self._make_bash_config({"sudo *": "deny"})
        assert config.evaluate_permission("echo hi; sudo rm -rf /") == "deny"

    def test_wildcard_matches_command_after_pipe(self):
        config = self._make_bash_config({"rm *": "deny"})
        assert config.evaluate_permission("ls | rm") == "deny"

    def test_wildcard_matches_command_after_or_operator(self):
        config = self._make_bash_config({"rm *": "deny"})
        assert config.evaluate_permission("false || rm -rf /tmp") == "deny"

    def test_wildcard_matches_command_inside_find_exec(self):
        config = self._make_bash_config({"rm *": "deny"})
        assert config.evaluate_permission("find . -exec rm {} \\;") == "deny"

    def test_wildcard_matches_command_after_xargs(self):
        config = self._make_bash_config({"rm *": "deny"})
        assert config.evaluate_permission("cat file | xargs rm") == "deny"

    def test_wildcard_matches_command_inside_subshell(self):
        config = self._make_bash_config({"rm *": "deny"})
        assert config.evaluate_permission("echo $(rm -rf /tmp)") == "deny"

    def test_wildcard_matches_command_inside_backtick_subshell(self):
        config = self._make_bash_config({"sudo *": "deny"})
        assert config.evaluate_permission("echo `sudo reboot`") == "deny"

    def test_wildcard_does_not_false_positive_on_partial_word(self):
        """'rm *' should not match 'format' or 'inform'."""
        config = self._make_bash_config({"rm *": "deny"})
        assert config.evaluate_permission("format disk") == "allow"
        assert config.evaluate_permission("echo remove_file") == "allow"

    def test_exact_match_pattern(self):
        config = self._make_bash_config({"reboot": "deny"})
        assert config.evaluate_permission("reboot") == "deny"
        assert config.evaluate_permission("reboot now") == "allow"
        assert config.evaluate_permission("echo reboot") == "allow"

    def test_longest_pattern_wins(self):
        config = self._make_bash_config({
            "rm *": "ask",
            "rm -rf *": "deny",
        })
        assert config.evaluate_permission("rm file.txt") == "ask"
        assert config.evaluate_permission("rm -rf /") == "deny"

    def test_chmod_ask_pattern(self):
        config = self._make_bash_config({"chmod *": "ask"})
        assert config.evaluate_permission("chmod 777 file.txt") == "ask"
        assert config.evaluate_permission("chmod +x script.sh") == "ask"
        assert config.evaluate_permission("ls -la") == "allow"

    def test_multiple_patterns_independent(self):
        config = self._make_bash_config({
            "rm *": "ask",
            "sudo *": "deny",
            "chmod *": "ask",
        })
        assert config.evaluate_permission("rm foo") == "ask"
        assert config.evaluate_permission("sudo foo") == "deny"
        assert config.evaluate_permission("chmod 644 bar") == "ask"
        assert config.evaluate_permission("echo safe") == "allow"


class TestPermissionEvaluator:
    """Tests for the PermissionEvaluator that wraps pattern matching and tool checks."""

    def _make_evaluator(self, permissions: dict[str, str] | None = None) -> PermissionEvaluator:
        config = AgentConfiguration(
            name="test",
            tools=ToolsConfiguration(
                bash=BashToolConfiguration(
                    permissions=permissions or {"rm *": "ask", "sudo *": "deny"},
                ),
                spawn_agent=SpawnAgentToolConfiguration(enabled=True),
            ),
            tools_enabled=["spawn_agent"],
            recursion_limit=3,
            system_prompt="test",
        )
        return PermissionEvaluator(config)

    def test_evaluate_bash_deny(self):
        evaluator = self._make_evaluator()
        assert evaluator.evaluate_bash_permission("sudo reboot") == "deny"

    def test_evaluate_bash_ask(self):
        evaluator = self._make_evaluator()
        assert evaluator.evaluate_bash_permission("rm file.txt") == "ask"

    def test_evaluate_bash_allow(self):
        evaluator = self._make_evaluator()
        assert evaluator.evaluate_bash_permission("echo hello") == "allow"

    def test_check_tool_enabled_passes_for_listed_tool(self):
        evaluator = self._make_evaluator()
        evaluator.check_tool_enabled("spawn_agent")

    def test_check_tool_enabled_raises_for_unlisted_tool(self):
        evaluator = self._make_evaluator()
        from harness.core.configuration import PermissionError as HarnessPermissionError
        with pytest.raises(HarnessPermissionError):
            evaluator.check_tool_enabled("orchestrate")

    def test_check_tool_enabled_always_allows_bash(self):
        evaluator = self._make_evaluator()
        evaluator.check_tool_enabled("bash")

    def test_check_spawn_agent_within_limits(self):
        evaluator = self._make_evaluator()
        evaluator.check_spawn_agent(current_depth=1, current_concurrent=2)

    def test_check_spawn_agent_exceeds_depth(self):
        evaluator = self._make_evaluator()
        from harness.core.configuration import PermissionError as HarnessPermissionError
        with pytest.raises(HarnessPermissionError, match="Recursion limit"):
            evaluator.check_spawn_agent(current_depth=3, current_concurrent=0)

    def test_check_spawn_agent_exceeds_concurrency(self):
        evaluator = self._make_evaluator()
        from harness.core.configuration import PermissionError as HarnessPermissionError
        with pytest.raises(HarnessPermissionError, match="Maximum concurrent"):
            evaluator.check_spawn_agent(current_depth=0, current_concurrent=5)


class TestOrchestratorPermissions:
    """Tests for how the orchestrator handles denied and ask-permission commands.

    These tests use the mock LLM so no real commands are ever executed.
    """

    @pytest.mark.asyncio
    async def test_denied_command_produces_error_event(self):
        """A command matching a 'deny' pattern yields an ERROR and is never executed."""
        agent_config = make_agent_configuration()
        agent_config.tools = ToolsConfiguration(
            bash=BashToolConfiguration(
                permissions={"sudo *": "deny"},
            ),
            spawn_agent=SpawnAgentToolConfiguration(enabled=False),
        )

        orchestrator, mock_llm = make_orchestrator(
            [
                [
                    make_tool_call_chunk(
                        "bash",
                        {"command": "sudo rm -rf /", "read_only": False, "risk": "high"},
                        "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                    )
                ],
                [make_text_chunk("ok")],
            ],
            agent_configuration=agent_config,
        )
        events = await collect_events(orchestrator, "do something dangerous")

        error_events = events_of_type(events, StreamEvent.Type.ERROR)
        assert len(error_events) >= 1
        assert "not permitted" in error_events[0].data["message"]

    @pytest.mark.asyncio
    async def test_denied_command_injects_system_message(self):
        """After a deny, a SystemMessage is injected telling the LLM not to retry."""
        conversation = []
        agent_config = make_agent_configuration()
        agent_config.tools = ToolsConfiguration(
            bash=BashToolConfiguration(
                permissions={"sudo *": "deny"},
            ),
            spawn_agent=SpawnAgentToolConfiguration(enabled=False),
        )

        orchestrator, mock_llm = make_orchestrator(
            [
                [
                    make_tool_call_chunk(
                        "bash",
                        {"command": "sudo rm -rf /", "read_only": False, "risk": "high"},
                        "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                    )
                ],
                [make_text_chunk("understood")],
            ],
            agent_configuration=agent_config,
            conversation=conversation,
        )
        await collect_events(orchestrator, "run sudo")

        system_messages = [
            message for message in conversation
            if isinstance(message, SystemMessage) and "security policy" in message.content
        ]
        assert len(system_messages) == 1
        assert "bypass" in system_messages[0].content
        assert "sudo" in system_messages[0].content

    @pytest.mark.asyncio
    async def test_allowed_command_does_not_produce_error(self):
        """A safe command matching no deny/ask patterns proceeds without error."""
        agent_config = make_agent_configuration()
        agent_config.tools = ToolsConfiguration(
            bash=BashToolConfiguration(
                permissions={"sudo *": "deny", "rm *": "ask"},
            ),
            spawn_agent=SpawnAgentToolConfiguration(enabled=False),
        )

        orchestrator, mock_llm = make_orchestrator(
            [
                [
                    make_tool_call_chunk(
                        "bash",
                        {"command": "echo hello", "read_only": True, "risk": "low"},
                        "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                    )
                ],
                [make_text_chunk("done")],
            ],
            agent_configuration=agent_config,
        )
        events = await collect_events(orchestrator, "run echo")

        error_events = events_of_type(events, StreamEvent.Type.ERROR)
        permission_events = events_of_type(events, StreamEvent.Type.PERMISSION_REQUEST)
        assert len(error_events) == 0
        assert len(permission_events) == 0

    @pytest.mark.asyncio
    async def test_dangerous_command_in_middle_of_chain_is_denied(self):
        """A denied command buried in a chain (echo && sudo ...) is caught and blocked."""
        agent_config = make_agent_configuration()
        agent_config.tools = ToolsConfiguration(
            bash=BashToolConfiguration(
                permissions={"sudo *": "deny"},
            ),
            spawn_agent=SpawnAgentToolConfiguration(enabled=False),
        )

        orchestrator, mock_llm = make_orchestrator(
            [
                [
                    make_tool_call_chunk(
                        "bash",
                        {"command": "echo safe && sudo rm -rf /", "read_only": False, "risk": "high"},
                        "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                    )
                ],
                [make_text_chunk("done")],
            ],
            agent_configuration=agent_config,
        )
        events = await collect_events(orchestrator, "chain command")

        error_events = [
            event for event in events_of_type(events, StreamEvent.Type.ERROR)
            if "not permitted" in event.data.get("message", "")
        ]
        assert len(error_events) == 1

    @pytest.mark.asyncio
    async def test_disabled_tool_produces_error(self):
        """Calling a tool that is not enabled yields an ERROR event."""
        agent_config = make_agent_configuration()
        agent_config.tools_enabled = ["spawn_agent"]
        agent_config.tools.spawn_agent.enabled = True

        orchestrator, mock_llm = make_orchestrator(
            [
                [
                    make_tool_call_chunk(
                        "orchestrate",
                        {"steps": [{"id": "s1", "agent": "main", "prompt": "test"}]},
                        "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                    )
                ],
                [make_text_chunk("ok")],
            ],
            agent_configuration=agent_config,
        )
        events = await collect_events(orchestrator, "orchestrate")

        error_events = events_of_type(events, StreamEvent.Type.ERROR)
        assert len(error_events) >= 1
        assert "not enabled" in error_events[0].data["message"]


class TestSlashCommands:
    """Tests for slash command handling via _command()."""

    def test_help_lists_all_commands(self):
        repl = make_repl_for_testing()
        repl._command("/help")
        output = printed_plain(repl)
        assert "/help" in output
        assert "/exit" in output
        assert "/switch" in output

    def test_switch_changes_agent(self):
        repl = make_repl_for_testing()
        repl._agents = ["main", "code", "explore"]
        repl._command("/switch code")
        assert repl._agent == "code"
        output = printed_plain(repl)
        assert "switched to code" in output

    def test_switch_unknown_agent_shows_error(self):
        repl = make_repl_for_testing()
        repl._agents = ["main", "code"]
        repl._command("/switch nonexistent")
        output = printed_plain(repl)
        assert "unknown agent" in output

    def test_agents_lists_available(self):
        repl = make_repl_for_testing()
        repl._agents = ["main", "code", "explore"]
        repl._command("/agents")
        output = printed_plain(repl)
        assert "main" in output
        assert "code" in output
        assert "explore" in output

    def test_session_shows_short_id(self):
        repl = make_repl_for_testing()
        repl._session_id = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
        repl._orchestrators = {}
        repl._command("/session")
        output = printed_plain(repl)
        assert "a1b2c3d4e5f6" in output
        assert "a1b2c3d4e5f6a7b8" not in output

    def test_exit_returns_false(self):
        repl = make_repl_for_testing()
        assert repl._command("/exit") is False

    def test_unknown_command_shows_error(self):
        repl = make_repl_for_testing()
        repl._command("/foobar")
        output = printed_plain(repl)
        assert "unknown" in output

    @pytest.mark.asyncio
    async def test_clear_resets_session(self):
        from repl import HarnessApp
        from textual.widgets import Input
        app = HarnessApp()
        async with app.run_test(size=(120, 30)) as pilot:
            old_session = app._session_id
            input_widget = app.query_one("#input", Input)
            input_widget.value = "/clear"
            await pilot.press("enter")
            await pilot.pause()
            assert app._session_id != old_session
            assert len(app._shared_conversation) == 0


class TestSessionHistoryDisplay:
    """Tests for visual conversation history on session resume.

    The resumed display must produce the same format as the live view:
    tool calls with name + #N label + arguments, results with name + #N label.
    """

    def test_display_human_messages_with_prompt_format(self):
        repl = make_repl_for_testing()
        repl._agent = "main"
        repl._shared_conversation = [
            HumanMessage(content="What is 2+2?"),
            AIMessageChunk(content="The answer is 4."),
            HumanMessage(content="And 3+3?"),
            AIMessageChunk(content="That would be 6."),
        ]

        repl._display_conversation_history()
        output = printed_plain(repl)
        assert ">" in output
        assert "What is 2+2?" in output
        assert "The answer is 4." in output or "[MD]The answer is 4.[/MD]" in "\n".join(repl._printed)
        assert "And 3+3?" in output

    def test_display_empty_conversation_prints_nothing(self):
        repl = make_repl_for_testing()
        repl._shared_conversation = []

        repl._display_conversation_history()
        assert not repl._printed

    def test_display_tool_calls_with_labels_and_arguments(self):
        repl = make_repl_for_testing()
        repl._shared_conversation = [
            HumanMessage(content="list files"),
            AIMessageChunk(
                content="",
                tool_calls=[
                    {"name": "bash", "args": {"command": "ls"}, "id": "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA"},
                    {"name": "bash", "args": {"command": "pwd"}, "id": "call_01_bC7dE9fG1hI3jK5lM7nO9pQ"},
                ],
            ),
        ]

        repl._display_conversation_history()
        output = printed_plain(repl)
        assert "tool: bash" in output
        assert "#1" in output
        assert "#2" in output
        assert "ls" in output
        assert "pwd" in output

    def test_display_tool_results_with_matching_labels(self):
        from langchain_core.messages import ToolMessage

        repl = make_repl_for_testing()
        repl._shared_conversation = [
            HumanMessage(content="run echo"),
            AIMessageChunk(
                content="",
                tool_calls=[{"name": "bash", "args": {"command": "echo hi"}, "id": "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA"}],
            ),
            ToolMessage(content='{"output": "hi"}', tool_call_id="call_00_Xk9mR2vL4wN8pQ1sT3uY5zA"),
            AIMessageChunk(content="Done."),
        ]

        repl._display_conversation_history()
        output = printed_plain(repl)
        assert "tool: bash" in output
        assert "#1" in output
        assert "result (bash)" in output
        assert "result (bash)" in output and "#1" in output
        assert '{"output"' not in output

    def test_display_matches_live_format_exactly(self):
        """The resumed display must produce output structurally identical to the live view."""
        from langchain_core.messages import ToolMessage

        repl = make_repl_for_testing()
        repl._shared_conversation = [
            HumanMessage(content="search for news"),
            AIMessageChunk(
                content="Let me search.",
                tool_calls=[
                    {"name": "web_search", "args": {"query": "latest news"}, "id": "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA"},
                    {"name": "bash", "args": {"command": "echo test"}, "id": "call_01_bC7dE9fG1hI3jK5lM7nO9pQ"},
                ],
            ),
            ToolMessage(content='{"code": "web_search_completed"}', tool_call_id="call_00_Xk9mR2vL4wN8pQ1sT3uY5zA"),
            ToolMessage(content='{"code": "bash_completed"}', tool_call_id="call_01_bC7dE9fG1hI3jK5lM7nO9pQ"),
            AIMessageChunk(content="Here are the results."),
        ]

        repl._display_conversation_history()
        output = printed_plain(repl)

        lines_with_content = [line.strip() for line in output.split("\n") if line.strip()]
        tool_web = [line for line in lines_with_content if "tool: web_search" in line]
        tool_bash = [line for line in lines_with_content if "tool: bash" in line]
        result_web = [line for line in lines_with_content if "result (web_search)" in line]
        result_bash = [line for line in lines_with_content if "result (bash)" in line]

        assert len(tool_web) == 1
        assert "#1" in tool_web[0]
        assert len(tool_bash) == 1
        assert "#2" in tool_bash[0]
        assert len(result_web) == 1
        assert "#1" in result_web[0]
        assert len(result_bash) == 1
        assert "#2" in result_bash[0]

    def test_display_cancelled_marker(self):
        """Cancelled events saved in conversation appear as 'cancelled' in history."""
        repl = make_repl_for_testing()
        repl._agent = "main"
        repl._shared_conversation = [
            HumanMessage(content="do something"),
            SystemMessage(content=json.dumps({"type": "cancelled"})),
            HumanMessage(content="try again"),
            AIMessageChunk(content="Here you go."),
        ]

        repl._display_conversation_history()
        output = printed_plain(repl)
        assert "do something" in output
        assert "cancelled" in output
        assert "try again" in output

    def test_display_max_iterations_marker(self):
        repl = make_repl_for_testing()
        repl._agent = "main"
        repl._shared_conversation = [
            HumanMessage(content="run forever"),
            SystemMessage(content=json.dumps({"type": "max_iterations"})),
        ]

        repl._display_conversation_history()
        output = printed_plain(repl)
        assert "max iterations" in output

    def test_display_user_message_with_chevron_prefix(self):
        """User messages in history show with > prefix."""
        repl = make_repl_for_testing()
        repl._shared_conversation = [
            HumanMessage(content="search something"),
        ]

        repl._display_conversation_history()
        output = printed_plain(repl)
        assert ">" in output
        assert "search something" in output


class TestSessionLoadByPrefix:
    """Tests for loading sessions by short (prefix) ID."""

    def _make_repl_with_sessions_dir(self, tmp_path):
        import repl as repl_module

        original_sessions_dir = repl_module.SESSIONS_DIR
        repl_module.SESSIONS_DIR = tmp_path

        repl = make_repl_for_testing()
        repl._shared_conversation = []
        repl._session_id = "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d"
        repl._agent = "main"
        repl._orchestrators = {}

        return repl, original_sessions_dir, repl_module

    def _write_session(self, tmp_path, session_id, messages=None):
        data = {
            "session_id": session_id,
            "agent": "main",
            "timestamp": "2026-06-24T12:00:00+00:00",
            "messages": messages or [
                {"type": "human", "content": f"hello from {session_id[:8]}"},
                {"type": "ai", "content": f"response from {session_id[:8]}"},
            ],
        }
        (tmp_path / f"{session_id}.json").write_text(json.dumps(data))

    def test_load_by_full_id(self, tmp_path):
        full_id = "7a3b9c2d1e4f56a8b0c3d7e9f1a24b6c"
        self._write_session(tmp_path, full_id)
        repl, original, module = self._make_repl_with_sessions_dir(tmp_path)
        try:
            assert repl._load_session(full_id)
            assert repl._session_id == full_id
            assert len(repl._shared_conversation) == 2
        finally:
            module.SESSIONS_DIR = original

    def test_load_by_short_prefix(self, tmp_path):
        full_id = "7a3b9c2d1e4f56a8b0c3d7e9f1a24b6c"
        self._write_session(tmp_path, full_id)
        repl, original, module = self._make_repl_with_sessions_dir(tmp_path)
        try:
            assert repl._load_session("7a3b9c2d1e4f")
            assert repl._session_id == full_id
        finally:
            module.SESSIONS_DIR = original

    def test_ambiguous_prefix_fails(self, tmp_path):
        self._write_session(tmp_path, "e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0")
        self._write_session(tmp_path, "e5f6a7b8c9d01a2b3c4d5e6f7a8b9c0d")
        repl, original, module = self._make_repl_with_sessions_dir(tmp_path)
        try:
            assert not repl._load_session("e5f6a7b8c9d0")
        finally:
            module.SESSIONS_DIR = original

    def test_nonexistent_prefix_fails(self, tmp_path):
        self._write_session(tmp_path, "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7")
        repl, original, module = self._make_repl_with_sessions_dir(tmp_path)
        try:
            assert not repl._load_session("ff00112233aa")
        finally:
            module.SESSIONS_DIR = original

    def test_loaded_session_restores_full_id(self, tmp_path):
        full_id = "f4dcf1a5f6f3429e8b7c1d2e3a4f5b6c"
        self._write_session(tmp_path, full_id)
        repl, original, module = self._make_repl_with_sessions_dir(tmp_path)
        try:
            assert repl._load_session("f4dcf1a5f6f3")
            assert repl._session_id == full_id
        finally:
            module.SESSIONS_DIR = original

    def test_serialize_produces_clean_format(self, tmp_path):
        """Saved messages use short type names and only essential fields."""
        from repl import HarnessApp

        entry = HarnessApp._serialize_message(HumanMessage(content="hello"))
        assert entry == {"type": "human", "content": "hello"}

        entry = HarnessApp._serialize_message(
            AIMessageChunk(
                content="answer",
                additional_kwargs={"reasoning_content": "thinking"},
                tool_calls=[{"name": "bash", "args": {}, "id": "call_10_zA1bC3dE5fG7hI9jK1lM3nO"}],
            )
        )
        assert entry["type"] == "ai"
        assert entry["content"] == "answer"
        assert entry["tool_calls"][0]["name"] == "bash"
        assert entry["tool_calls"][0]["id"] == "call_10_zA1bC3dE5fG7hI9jK1lM3nO"
        assert entry["additional_kwargs"]["reasoning_content"] == "thinking"
        assert "response_metadata" not in entry
        assert "usage_metadata" not in entry
        assert "invalid_tool_calls" not in entry

    def test_round_trip_save_and_load(self, tmp_path):
        """Messages survive a save/load round trip with correct types and content."""
        import repl as repl_module
        from langchain_core.messages import ToolMessage

        original_sessions_dir = repl_module.SESSIONS_DIR
        repl_module.SESSIONS_DIR = tmp_path
        try:
            repl = make_repl_for_testing()
            repl._session_id = "f8a9b0c1d2e34f4a5b6c7d8e9f0a1b2c"
            repl._agent = "main"
            repl._orchestrators = {}
            repl._shared_conversation = [
                HumanMessage(content="what is 2+2?"),
                AIMessageChunk(
                    content="",
                    tool_calls=[{"name": "bash", "args": {"command": "echo 4"}, "id": "call_10_zA1bC3dE5fG7hI9jK1lM3nO"}],
                ),
                ToolMessage(content="4", tool_call_id="call_10_zA1bC3dE5fG7hI9jK1lM3nO"),
                AIMessageChunk(content="The answer is 4."),
            ]

            repl._save_session()

            saved = json.loads((tmp_path / f"{repl._session_id}.json").read_text())
            types = [message["type"] for message in saved["messages"]]
            assert types == ["human", "ai", "tool", "ai"]

            repl._shared_conversation.clear()
            assert repl._load_session(repl._session_id)
            assert len(repl._shared_conversation) == 4
            assert repl._shared_conversation[0].content == "what is 2+2?"
            assert repl._shared_conversation[2].content == "4"
            assert repl._shared_conversation[3].content == "The answer is 4."
        finally:
            repl_module.SESSIONS_DIR = original_sessions_dir


class TestCancellationAndRecovery:
    """Tests for aborting mid-stream and recovering on the next query."""

    @pytest.mark.asyncio
    async def test_cancellation_during_llm_streaming_leaves_conversation_usable(self):
        """Aborting while the LLM is streaming text should leave the conversation
        in a state where the next query still works."""
        conversation = []
        orchestrator, mock_llm = make_orchestrator(
            [
                [make_text_chunk("partial "), make_text_chunk("response")],
                [make_text_chunk("second answer")],
            ],
            conversation=conversation,
        )

        events = []
        async for event in orchestrator.stream("first query"):
            events.append(event)
            if event.type == StreamEvent.Type.TEXT_CHUNK:
                orchestrator._abort_event.set()

        done_event = events_of_type(events, StreamEvent.Type.DONE)[0]
        assert done_event.data["stop_reason"] == "cancelled"

        second_events = await collect_events(orchestrator, "second query")
        second_done = events_of_type(second_events, StreamEvent.Type.DONE)[0]
        assert second_done.data["stop_reason"] == "completed"
        assert "second answer" in second_done.data["text"]

    @pytest.mark.asyncio
    async def test_cancellation_during_tool_execution_leaves_conversation_usable(self):
        """Aborting while tools are being executed should not corrupt conversation state."""
        conversation = []
        orchestrator, mock_llm = make_orchestrator(
            [
                [
                    make_text_chunk("I'll create tasks. "),
                    make_tool_call_chunk(
                        "write_tasks",
                        {"tasks": [{"description": "task A"}]},
                        "call_00_Xk9mR2vL4wN8pQ1sT3uY5zA",
                    ),
                ],
                [make_text_chunk("recovered")],
            ],
            conversation=conversation,
        )

        events = []
        async for event in orchestrator.stream("do work"):
            events.append(event)
            if event.type == StreamEvent.Type.TASKS_UPDATED:
                orchestrator._abort_event.set()

        done_event = events_of_type(events, StreamEvent.Type.DONE)[0]
        assert done_event.data["stop_reason"] == "cancelled"

        second_events = await collect_events(orchestrator, "continue")
        second_done = events_of_type(second_events, StreamEvent.Type.DONE)[0]
        assert second_done.data["stop_reason"] == "completed"

    @pytest.mark.asyncio
    async def test_conversation_has_human_message_after_cancellation(self):
        """Even after cancellation, the human message is in the conversation."""
        conversation = []
        orchestrator, mock_llm = make_orchestrator(
            [
                [make_text_chunk("response")],
            ],
            conversation=conversation,
        )

        orchestrator._abort_event.set()
        await asyncio.sleep(0)
        events = await collect_events(orchestrator, "my question")

        human_messages = [
            message for message in conversation
            if isinstance(message, HumanMessage)
        ]
        assert len(human_messages) == 1
        assert human_messages[0].content == "my question"

    @pytest.mark.asyncio
    async def test_multiple_cancellations_then_success(self):
        """Multiple aborted queries followed by a successful one should all work."""
        conversation = []
        orchestrator, mock_llm = make_orchestrator(
            [
                [make_text_chunk("will be cancelled 1")],
                [make_text_chunk("will be cancelled 2")],
                [make_text_chunk("success")],
            ],
            conversation=conversation,
        )

        for query_number in range(1, 3):
            events = []
            async for event in orchestrator.stream(f"query {query_number}"):
                events.append(event)
                if event.type == StreamEvent.Type.STATUS:
                    orchestrator._abort_event.set()

        final_events = await collect_events(orchestrator, "final query")
        final_done = events_of_type(final_events, StreamEvent.Type.DONE)[0]
        assert final_done.data["stop_reason"] == "completed"
        assert "success" in final_done.data["text"]


class TestMultiTurnWithTools:
    """Tests for complex multi-turn conversations with interleaved tool calls."""

    @pytest.mark.asyncio
    async def test_tool_calls_across_multiple_turns(self):
        """Tool calls across multiple user queries accumulate correctly."""
        conversation = []
        orchestrator, mock_llm = make_orchestrator(
            [
                [
                    make_tool_call_chunk(
                        "write_tasks",
                        {"tasks": [{"description": "from turn 1"}]},
                        "call_03_hI5jK7lM9nO1pQ3rS5tU7vW",
                    ),
                ],
                [make_text_chunk("Turn 1 done.")],
                [
                    make_tool_call_chunk(
                        "update_tasks",
                        {"updates": [{"task_id": "task-1", "status": "completed", "result": "done"}]},
                        "call_04_xY1zA3bC5dE7fG9hI1jK3lM",
                    ),
                ],
                [make_text_chunk("Turn 2 done.")],
            ],
            conversation=conversation,
        )

        events_1 = await collect_events(orchestrator, "create a task")
        done_1 = events_of_type(events_1, StreamEvent.Type.DONE)[0]
        assert done_1.data["stop_reason"] == "completed"

        events_2 = await collect_events(orchestrator, "complete the task")
        done_2 = events_of_type(events_2, StreamEvent.Type.DONE)[0]
        assert done_2.data["stop_reason"] == "completed"

        assert len(conversation) >= 4

    @pytest.mark.asyncio
    async def test_multiple_tool_types_in_single_turn(self):
        """A turn with write_tasks + update_tasks produces correct events."""
        orchestrator, mock_llm = make_orchestrator([
            [
                AIMessageChunk(
                    content="Planning and updating.",
                    tool_calls=[
                        {"name": "write_tasks", "args": {"tasks": [{"description": "X"}]}, "id": "call_10_zA1bC3dE5fG7hI9jK1lM3nO"},
                        {"name": "update_tasks", "args": {"updates": [{"task_id": "task-1", "status": "in_progress"}]}, "id": "call_11_pQ5rS7tU9vW1xY3zA5bC7dE"},
                    ],
                ),
            ],
            [make_text_chunk("All done.")],
        ])
        events = await collect_events(orchestrator, "do both")

        tool_calls = events_of_type(events, StreamEvent.Type.TOOL_CALL)
        assert len(tool_calls) == 2
        assert tool_calls[0].data["name"] == "write_tasks"
        assert tool_calls[1].data["name"] == "update_tasks"

        task_updates = events_of_type(events, StreamEvent.Type.TASKS_UPDATED)
        assert len(task_updates) == 2

    @pytest.mark.asyncio
    async def test_denied_command_does_not_corrupt_next_turn(self):
        """A denied command in one turn doesn't break the next turn."""
        agent_config = make_agent_configuration()
        agent_config.tools = ToolsConfiguration(
            bash=BashToolConfiguration(permissions={"sudo *": "deny"}),
            spawn_agent=SpawnAgentToolConfiguration(enabled=False),
        )

        conversation = []
        orchestrator, mock_llm = make_orchestrator(
            [
                [
                    make_tool_call_chunk(
                        "bash",
                        {"command": "sudo rm -rf /", "read_only": False, "risk": "high"},
                        "call_02_rS1tU3vW5xY7zA9bC1dE3fG",
                    ),
                ],
                [make_text_chunk("Sorry, I can't do that.")],
                [make_text_chunk("Here's a safe answer.")],
            ],
            agent_configuration=agent_config,
            conversation=conversation,
        )

        events_1 = await collect_events(orchestrator, "delete everything")
        error_events = events_of_type(events_1, StreamEvent.Type.ERROR)
        assert any("not permitted" in event.data.get("message", "") for event in error_events)

        events_2 = await collect_events(orchestrator, "try something safe")
        done_2 = events_of_type(events_2, StreamEvent.Type.DONE)[0]
        assert done_2.data["stop_reason"] == "completed"
        assert "safe answer" in done_2.data["text"]


class TestResumeConsistency:
    """Tests verifying that save → load → display produces output matching the live session."""

    def _make_repl_with_tmp_sessions(self, tmp_path):
        import repl as repl_module
        original = repl_module.SESSIONS_DIR
        repl_module.SESSIONS_DIR = tmp_path
        return original, repl_module

    def _collect_display_output(self, repl) -> str:
        repl._printed.clear()
        repl._display_conversation_history()
        return printed_plain(repl)

    def test_simple_conversation_round_trip_display(self, tmp_path):
        """A simple human→ai conversation displays identically after round-trip."""
        original, module = self._make_repl_with_tmp_sessions(tmp_path)
        try:
            repl = make_repl_for_testing()
            repl._session_id = "a1b2c3d4e5f64a7b8c9d0e1f2a3b4c5d"
            repl._agent = "main"
            repl._orchestrators = {}
            repl._shared_conversation = [
                HumanMessage(content="hello"),
                AIMessageChunk(content="Hi there!"),
            ]

            repl._save_session()
            original_output = self._collect_display_output(repl)

            repl._shared_conversation.clear()
            repl._printed.clear()
            assert repl._load_session(repl._session_id)
            resumed_output = self._collect_display_output(repl)

            assert original_output == resumed_output
        finally:
            module.SESSIONS_DIR = original

    def test_tool_call_round_trip_display(self, tmp_path):
        """Tool calls with labels display identically after round-trip."""
        from langchain_core.messages import ToolMessage
        original, module = self._make_repl_with_tmp_sessions(tmp_path)
        try:
            repl = make_repl_for_testing()
            repl._session_id = "d6e7f8a9b0c14d2e3f4a5b6c7d8e9f0a"
            repl._agent = "main"
            repl._orchestrators = {}
            repl._shared_conversation = [
                HumanMessage(content="search news"),
                AIMessageChunk(
                    content="Searching.",
                    tool_calls=[
                        {"name": "web_search", "args": {"query": "news"}, "id": "call_10_zA1bC3dE5fG7hI9jK1lM3nO"},
                        {"name": "bash", "args": {"command": "echo test"}, "id": "call_11_pQ5rS7tU9vW1xY3zA5bC7dE"},
                    ],
                ),
                ToolMessage(content='{"results": []}', tool_call_id="call_10_zA1bC3dE5fG7hI9jK1lM3nO"),
                ToolMessage(content='{"output": "test"}', tool_call_id="call_11_pQ5rS7tU9vW1xY3zA5bC7dE"),
                AIMessageChunk(content="Here are the results."),
            ]

            repl._save_session()
            original_output = self._collect_display_output(repl)

            repl._shared_conversation.clear()
            repl._printed.clear()
            assert repl._load_session(repl._session_id)
            resumed_output = self._collect_display_output(repl)

            assert original_output == resumed_output

            assert "tool: web_search" in resumed_output
            assert "#1" in resumed_output
            assert "tool: bash" in resumed_output
            assert "#2" in resumed_output
            assert "result (web_search)" in resumed_output
            assert "result (bash)" in resumed_output
        finally:
            module.SESSIONS_DIR = original

    def test_multi_turn_round_trip_display(self, tmp_path):
        """A multi-turn conversation with tools displays identically after round-trip."""
        from langchain_core.messages import ToolMessage
        original, module = self._make_repl_with_tmp_sessions(tmp_path)
        try:
            repl = make_repl_for_testing()
            repl._session_id = "1b2c3d4e5f6a47b8c9d0e1f2a3b4c5d6"
            repl._agent = "main"
            repl._orchestrators = {}
            repl._shared_conversation = [
                HumanMessage(content="first question"),
                AIMessageChunk(
                    content="Let me check.",
                    tool_calls=[{"name": "bash", "args": {"command": "ls"}, "id": "call_10_zA1bC3dE5fG7hI9jK1lM3nO"}],
                ),
                ToolMessage(content="file.txt", tool_call_id="call_10_zA1bC3dE5fG7hI9jK1lM3nO"),
                AIMessageChunk(content="Found file.txt."),
                HumanMessage(content="second question"),
                AIMessageChunk(
                    content="",
                    tool_calls=[{"name": "bash", "args": {"command": "cat file.txt"}, "id": "call_11_pQ5rS7tU9vW1xY3zA5bC7dE"}],
                ),
                ToolMessage(content="hello world", tool_call_id="call_11_pQ5rS7tU9vW1xY3zA5bC7dE"),
                AIMessageChunk(content="The file contains hello world."),
            ]

            repl._save_session()
            original_output = self._collect_display_output(repl)

            repl._shared_conversation.clear()
            repl._printed.clear()
            assert repl._load_session(repl._session_id)
            resumed_output = self._collect_display_output(repl)

            assert original_output == resumed_output

            assert "first question" in resumed_output
            assert "second question" in resumed_output
            assert "#1" in resumed_output
            assert "#2" in resumed_output
            assert "result (bash)" in resumed_output
        finally:
            module.SESSIONS_DIR = original

    def test_cancelled_session_round_trip(self, tmp_path):
        """A session saved after cancellation loads and displays correctly."""
        original, module = self._make_repl_with_tmp_sessions(tmp_path)
        try:
            repl = make_repl_for_testing()
            repl._session_id = "e7f8a9b0c1d24e3f4a5b6c7d8e9f0a1b"
            repl._agent = "main"
            repl._orchestrators = {}
            repl._shared_conversation = [
                HumanMessage(content="do something"),
                AIMessageChunk(
                    content="Starting.",
                    tool_calls=[{"name": "bash", "args": {"command": "sleep 60"}, "id": "call_10_zA1bC3dE5fG7hI9jK1lM3nO"}],
                ),
                SystemMessage(content=json.dumps({"type": "cancelled"})),
                HumanMessage(content="try again"),
                AIMessageChunk(content="Here you go."),
            ]

            repl._save_session()
            original_output = self._collect_display_output(repl)

            repl._shared_conversation.clear()
            repl._printed.clear()
            assert repl._load_session(repl._session_id)
            resumed_output = self._collect_display_output(repl)

            assert original_output == resumed_output
            assert "do something" in resumed_output
            assert "cancelled" in resumed_output
            assert "try again" in resumed_output
            assert "tool: bash" in resumed_output
        finally:
            module.SESSIONS_DIR = original

    def test_label_numbering_consistent_across_turns(self, tmp_path):
        """Tool call labels in a multi-turn session are numbered sequentially across turns."""
        from langchain_core.messages import ToolMessage
        original, module = self._make_repl_with_tmp_sessions(tmp_path)
        try:
            repl = make_repl_for_testing()
            repl._session_id = "2c3d4e5f6a7b48c9d0e1f2a3b4c5d6e7"
            repl._agent = "main"
            repl._orchestrators = {}
            repl._shared_conversation = [
                HumanMessage(content="turn 1"),
                AIMessageChunk(
                    content="",
                    tool_calls=[{"name": "bash", "args": {"command": "echo 1"}, "id": "call_10_zA1bC3dE5fG7hI9jK1lM3nO"}],
                ),
                ToolMessage(content="1", tool_call_id="call_10_zA1bC3dE5fG7hI9jK1lM3nO"),
                AIMessageChunk(content="Got 1."),
                HumanMessage(content="turn 2"),
                AIMessageChunk(
                    content="",
                    tool_calls=[{"name": "bash", "args": {"command": "echo 2"}, "id": "call_11_pQ5rS7tU9vW1xY3zA5bC7dE"}],
                ),
                ToolMessage(content="2", tool_call_id="call_11_pQ5rS7tU9vW1xY3zA5bC7dE"),
                AIMessageChunk(content="Got 2."),
                HumanMessage(content="turn 3"),
                AIMessageChunk(
                    content="",
                    tool_calls=[
                        {"name": "bash", "args": {"command": "echo 3"}, "id": "call_12_fG9hI1jK3lM5nO7pQ9rS1tU"},
                        {"name": "bash", "args": {"command": "echo 4"}, "id": "call_13_vW3xY5zA7bC9dE1fG3hI5jK"},
                    ],
                ),
                ToolMessage(content="3", tool_call_id="call_12_fG9hI1jK3lM5nO7pQ9rS1tU"),
                ToolMessage(content="4", tool_call_id="call_13_vW3xY5zA7bC9dE1fG3hI5jK"),
                AIMessageChunk(content="Got 3 and 4."),
            ]

            repl._save_session()
            repl._shared_conversation.clear()
            assert repl._load_session(repl._session_id)

            repl._printed.clear()
            repl._display_conversation_history()
            output = printed_plain(repl)

            assert "#1" in output
            assert "#2" in output
            assert "#3" in output
            assert "#4" in output

            lines = output.split("\n")
            tool_lines = [line for line in lines if "tool: bash" in line]
            result_lines = [line for line in lines if "result (bash)" in line]
            assert len(tool_lines) == 4
            assert len(result_lines) == 4
        finally:
            module.SESSIONS_DIR = original


class TestTextualTUI:
    """Integration tests using Textual's run_test() to exercise the actual TUI."""

    @pytest.mark.asyncio
    async def test_app_mounts_with_all_widgets(self):
        from repl import HarnessApp
        app = HarnessApp()
        async with app.run_test(size=(120, 30)) as pilot:
            assert app.query_one("#status") is not None
            assert app.query_one("#output") is not None
            assert app.query_one("#input") is not None

    @pytest.mark.asyncio
    async def test_status_bar_shows_agent_and_session(self):
        from repl import HarnessApp
        app = HarnessApp()
        async with app.run_test(size=(120, 30)) as pilot:
            status = app.query_one("#status")
            status_text = str(status.render())
            assert app._agent in status_text
            assert app._session_id[:12] in status_text

    @pytest.mark.asyncio
    async def test_slash_help_writes_to_output(self):
        from repl import HarnessApp
        from textual.widgets import Input, RichLog
        app = HarnessApp()
        async with app.run_test(size=(120, 30)) as pilot:
            input_widget = app.query_one("#input", Input)
            input_widget.value = "/help"
            await pilot.press("enter")
            await pilot.pause()
            output_log = app.query_one("#output", RichLog)
            assert len(output_log.lines) > 0

    @pytest.mark.asyncio
    async def test_slash_agents_writes_to_output(self):
        from repl import HarnessApp
        from textual.widgets import Input, RichLog
        app = HarnessApp()
        async with app.run_test(size=(120, 30)) as pilot:
            input_widget = app.query_one("#input", Input)
            input_widget.value = "/agents"
            await pilot.press("enter")
            await pilot.pause()
            output_log = app.query_one("#output", RichLog)
            assert len(output_log.lines) > 0

    @pytest.mark.asyncio
    async def test_input_clears_after_submission(self):
        from repl import HarnessApp
        from textual.widgets import Input
        app = HarnessApp()
        async with app.run_test(size=(120, 30)) as pilot:
            input_widget = app.query_one("#input", Input)
            input_widget.value = "/help"
            await pilot.press("enter")
            await pilot.pause()
            assert input_widget.value == ""

    @pytest.mark.asyncio
    async def test_slash_switch_changes_agent(self):
        from repl import HarnessApp
        from textual.widgets import Input
        app = HarnessApp()
        async with app.run_test(size=(120, 30)) as pilot:
            original_agent = app._agent
            target_agent = [a for a in app._agents if a != original_agent][0]
            input_widget = app.query_one("#input", Input)
            input_widget.value = f"/switch {target_agent}"
            await pilot.press("enter")
            await pilot.pause()
            assert app._agent == target_agent

    @pytest.mark.asyncio
    async def test_slash_exit_closes_app(self):
        from repl import HarnessApp
        from textual.widgets import Input
        app = HarnessApp()
        async with app.run_test(size=(120, 30)) as pilot:
            input_widget = app.query_one("#input", Input)
            input_widget.value = "/exit"
            await pilot.press("enter")
            await pilot.pause()
