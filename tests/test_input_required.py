"""input-required answer routing: an external client resolves a pending permission or
question via a message/send carrying an input_response part."""

import asyncio

from a2a.types import DataPart, Message, Part, Role, TextPart

from harness.core.a2a_executor import (
    INPUT_RESPONSE_KIND,
    HarnessAgentExecutor,
    _data_part,
    _input_response_payload,
)
from harness.core.configuration import GlobalConfiguration


def _executor(pending_permissions, pending_questions):
    return HarnessAgentExecutor(
        agent_name="tester",
        global_configuration=GlobalConfiguration(),
        task_store=object(),
        pending_permissions=pending_permissions,
        pending_questions=pending_questions,
    )


def test_input_response_payload_extraction():
    message = Message(
        role=Role.user,
        parts=[
            Part(root=TextPart(text="ignored")),
            Part(root=DataPart(data={"kind": INPUT_RESPONSE_KIND, "request_id": "perm-1", "decision": "allow_once"})),
        ],
        message_id="m1",
    )
    payload = _input_response_payload(message)
    assert payload == {"kind": INPUT_RESPONSE_KIND, "request_id": "perm-1", "decision": "allow_once"}
    assert _input_response_payload(Message(role=Role.user, parts=[Part(root=TextPart(text="hi"))], message_id="m2")) is None


async def test_resolves_pending_permission():
    permissions: dict = {}
    executor = _executor(permissions, {})
    future = asyncio.get_running_loop().create_future()
    permissions["perm-ctx-1"] = future
    assert executor._resolve_input_response({"request_id": "perm-ctx-1", "decision": "allow_always"}) is True
    assert future.result() == "allow_always"


async def test_resolves_pending_question_and_decline():
    questions: dict = {}
    executor = _executor({}, questions)
    answered = asyncio.get_running_loop().create_future()
    declined = asyncio.get_running_loop().create_future()
    questions["q-ctx-1"] = answered
    questions["q-ctx-2"] = declined
    assert executor._resolve_input_response({"request_id": "q-ctx-1", "answers": [{"a": 1}]}) is True
    assert answered.result() == [{"a": 1}]
    assert executor._resolve_input_response({"request_id": "q-ctx-2", "declined": True}) is True
    assert declined.result() == {"__declined__": True}


async def test_unknown_request_id_is_noop():
    executor = _executor({}, {})
    assert executor._resolve_input_response({"request_id": "nope", "decision": "deny"}) is False
