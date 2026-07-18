"""Executor↔a2a-sdk integration: a top-level turn suspends as a durable A2A
``input-required`` task (pending record + conversation checkpoint persisted), and an
``input_response`` ``message/send`` rebuilds from that checkpoint and resumes to completion.

Drives the real ``HarnessAgentExecutor`` through the real ``DefaultRequestHandler`` and
``AppendOnlyTaskStore``, with only the model faked.
"""
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from langchain_core.messages import AIMessageChunk

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import Message, Role, Part, TextPart, DataPart, MessageSendParams, TaskState

import harness.core.agent as agentmod
import harness.core.a2a_executor as execmod
from harness.core.a2a_executor import HarnessAgentExecutor, PENDING_INTERACTION_KEY
from harness.core.task_store import AppendOnlyTaskStore
from harness.core.configuration import GlobalConfiguration, AgentConfiguration


def _bash_call(cmd, tool_call_id="call-1"):
    return AIMessageChunk(content="", tool_call_chunks=[{
        "name": "bash",
        "args": '{"command": "' + cmd + '", "justification": "t"}',
        "id": tool_call_id, "index": 0,
    }])


def _text(text):
    return AIMessageChunk(content=[{"type": "text", "text": text, "id": "b1", "index": 0}])


class _Bound:
    def __init__(self, script):
        self._script, self._i = script, 0

    async def astream(self, messages):
        yield self._script[min(self._i, len(self._script) - 1)]
        self._i += 1

    def bind_tools(self, *a, **k):
        return self


class _FakeLLM:
    def __init__(self, script):
        self._bound = _Bound(script)

    def bind_tools(self, *a, **k):
        return self._bound


async def _drain(handler, message):
    return [event async for event in handler.on_message_send_stream(MessageSendParams(message=message))]


def _user_message(text, context_id, task_id=None):
    return Message(
        role=Role.user, parts=[Part(root=TextPart(text=text))],
        message_id=uuid.uuid4().hex, context_id=context_id, task_id=task_id,
    )


async def test_executor_suspend_persists_and_resume_completes(monkeypatch):
    # The turn's two model calls: a gated bash tool, then the final text after resume.
    script = [_bash_call("frobnicate --now"), _text("all done")]
    monkeypatch.setattr(agentmod, "model_is_authorized", lambda *a, **k: True)
    monkeypatch.setattr(agentmod, "build_chat_model", lambda *a, **k: _FakeLLM(script))
    configuration = AgentConfiguration(identifier="default", provider="openai", model="gpt-x")
    monkeypatch.setattr(execmod, "load_agent_configuration", lambda *a, **k: configuration)

    store = AppendOnlyTaskStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.initialize()
    executor = HarnessAgentExecutor(
        agent_name="default", global_configuration=GlobalConfiguration(), task_store=store,
    )
    handler = DefaultRequestHandler(agent_executor=executor, task_store=store)

    context_id = "ctx-exec"
    # --- segment 1: the turn suspends as input-required ---
    await _drain(handler, _user_message("please frobnicate", context_id))

    tasks = await store.tasks_for_context(context_id)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.status.state == TaskState.input_required, "top-level pause closes the segment as input-required"
    pending = (task.metadata or {}).get(PENDING_INTERACTION_KEY)
    assert isinstance(pending, dict) and pending["gates"], "pending interactions persisted on the task"
    checkpoint = await store.load_checkpoint(context_id)
    assert checkpoint, "the conversation checkpoint was saved at the pause"
    request_id = pending["gates"][0]["request_id"]

    # --- segment 2: an input_response rebuilds from the checkpoint and resumes ---
    answer = Message(
        role=Role.user,
        parts=[Part(root=DataPart(data={"kind": "input_response", "request_id": request_id, "decision": "allow"}))],
        message_id=uuid.uuid4().hex, context_id=context_id, task_id=task.id,
    )
    await _drain(handler, answer)

    resumed = await store.get(task.id)
    assert resumed.status.state == TaskState.completed, "the answered turn resumes and completes"
    assert PENDING_INTERACTION_KEY not in (resumed.metadata or {}), "the pending record is cleared on resume"
