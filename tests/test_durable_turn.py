"""End-to-end suspend/resume of the real ``AgentRuntime`` turn loop, driven by a scripted
fake LLM (no network). Exercises the genuine preflight, suspend, resume, tool-drain, and
safe-point CHECKPOINT logic — only the model is faked.

Covered:
  * a top-level turn hits a gate, yields the unified SUSPENDED event, and returns (the
    durable-segment tail); its conversation ends with the pending tool-call checkpoint;
  * resuming with ``allow`` runs the tool, fires CHECKPOINT, and completes;
  * resuming with ``deny`` records a denial ToolMessage (the conversation stays valid);
  * a delegated turn emits the SAME single SUSPENDED event, parks in place, and — once the
    shared resolver answers it — continues the SAME stream to completion.
"""
import asyncio

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage

import harness.core.agent as agentmod
from harness.core.agent import AgentRuntime, StreamEvent
from harness.core.configuration import GlobalConfiguration, AgentConfiguration


class _Bound:
    def __init__(self, script):
        self._script, self._i = script, 0

    async def astream(self, messages):
        chunk = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        yield chunk

    def bind_tools(self, *a, **k):
        return self


class _FakeLLM:
    def __init__(self, script):
        self._bound = _Bound(script)

    def bind_tools(self, *a, **k):
        return self._bound


def _bash_call(cmd, tool_call_id="call-1"):
    return AIMessageChunk(content="", tool_call_chunks=[{
        "name": "bash",
        "args": '{"command": "' + cmd + '", "justification": "t"}',
        "id": tool_call_id, "index": 0,
    }])


def _text(text):
    return AIMessageChunk(content=[{"type": "text", "text": text, "id": "b1", "index": 0}])


def _runtime(monkeypatch, script, *, is_agent, session_id):
    monkeypatch.setattr(agentmod, "model_is_authorized", lambda *a, **k: True)
    monkeypatch.setattr(agentmod, "build_chat_model", lambda *a, **k: _FakeLLM(script))
    configuration = AgentConfiguration(identifier="default", provider="openai", model="gpt-x")
    runtime = AgentRuntime(
        agent_configuration=configuration,
        global_configuration=GlobalConfiguration(),
        session_id=session_id,
        is_agent=is_agent,
    )
    if is_agent:
        runtime.set_delegated_policy("default")
    return runtime


async def _drain(stream):
    return [event async for event in stream]


async def test_top_level_suspend_returns_at_gate(monkeypatch):
    # An unlisted command under the interactive policy asks (ask-by-default), so the turn
    # suspends and, being top-level, returns to become a durable segment.
    runtime = _runtime(monkeypatch, [_bash_call("frobnicate --now"), _text("done")],
                       is_agent=False, session_id="ctx1")
    events = await _drain(runtime.stream("please frobnicate"))
    kinds = [e.type.value for e in events]
    assert "suspended" in kinds
    assert kinds[-1] == "suspended"  # returned at the pause
    suspended = next(e for e in events if e.type == StreamEvent.Type.SUSPENDED)
    assert suspended.data["interactions"][0]["kind"] == "permission"
    # the checkpoint is the pending tool-call AIMessage
    assert getattr(runtime.conversation[-1], "tool_calls", None)


async def test_top_level_resume_allow_runs_tool(monkeypatch):
    runtime = _runtime(monkeypatch, [_bash_call("frobnicate --now"), _text("all done")],
                       is_agent=False, session_id="ctx2")
    suspended = next(e for e in await _drain(runtime.stream("go"))
                     if e.type == StreamEvent.Type.SUSPENDED)
    request_id = suspended.data["interactions"][0]["request_id"]
    kinds = [e.type.value for e in await _drain(runtime.resume_stream(suspended.data["plans"], {request_id: "allow"}))]
    assert "tool_result" in kinds          # the approved tool actually ran
    assert "checkpoint" in kinds           # safe-point CHECKPOINT after the batch
    assert kinds[-1] == "done"
    assert any(isinstance(m, ToolMessage) for m in runtime.conversation)


async def test_top_level_resume_deny_stays_valid(monkeypatch):
    runtime = _runtime(monkeypatch, [_bash_call("frobnicate --again"), _text("ok")],
                       is_agent=False, session_id="ctx3")
    suspended = next(e for e in await _drain(runtime.stream("go"))
                     if e.type == StreamEvent.Type.SUSPENDED)
    request_id = suspended.data["interactions"][0]["request_id"]
    await _drain(runtime.resume_stream(suspended.data["plans"], {request_id: "deny"}))
    # a denied call still records a ToolMessage, so the conversation has no dangling tool_call
    assert any(isinstance(m, ToolMessage) for m in runtime.conversation)


async def test_delegated_parks_and_continues_same_stream(monkeypatch):
    runtime = _runtime(monkeypatch, [_bash_call("frobnicate --deleg"), _text("delegated done")],
                       is_agent=True, session_id="ctx4")
    seen: list[str] = []

    async def consume():
        async for event in runtime.stream("frob deleg"):
            seen.append(event.type.value)

    consumer = asyncio.ensure_future(consume())
    # The delegated turn yields SUSPENDED then parks in _await_pending_answers, registering
    # its future. Answer it the way the shared resolver's delegated path does.
    for _ in range(200):
        await asyncio.sleep(0.005)
        if "suspended" in seen and runtime._agent_permission_futures:
            break
    assert runtime._agent_permission_futures, "delegated turn should have parked"
    request_id = next(iter(runtime._agent_permission_futures))
    assert runtime.resolve_agent_permission(request_id, "allow") is True
    await asyncio.wait_for(consumer, timeout=10)

    assert seen.count("suspended") == 1           # the one unified suspend event
    assert "tool_result" in seen                  # continued the SAME stream and ran the tool
    assert seen[-1] == "done"
