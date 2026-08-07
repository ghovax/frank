"""Peer sessions as tools: the one composition path a model can walk."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional, Protocol, runtime_checkable

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import Field, ValidationError, create_model

from langmesh.base.configuration import PromptLoader
from langmesh.base.serialization import compact
from langmesh.runtime.tools import context as tool_context
from langmesh.runtime.tools.registry import EXPLANATION, tool_description as _description

# The prompts these tools speak with. What they tell the *model* is a description, and lives with every other one.
_PROMPTS = PromptLoader(Path(__file__).resolve().parent.parent / "prompts")


@runtime_checkable
class SessionAccess(Protocol):
    """What a session needs in order to work with its peers, implemented by the worker that holds its identity."""

    session_id: str
    working_directory: str
    permission_mode: str

    async def create(
        self,
        *,
        agent: str,
        working_directory: str,
        inherited_conversation: list[dict[str, Any]],
    ) -> dict: ...
    async def send(self, session_id: str, text: str) -> None: ...
    async def get(self, session_id: str) -> dict: ...
    async def children(self) -> list[dict]: ...
    async def end(self, session_id: str) -> dict: ...
    async def remote_list(self) -> list[dict]: ...
    async def remote_send(self, name: str, text: str) -> dict: ...


def _unavailable(code: str) -> str:
    return compact(
        {
            "code": code,
            "status": "error",
            "message": "Peer sessions are not available in this process.",
        }
    )


async def _create_session(
    explanation: str,
    agent: str,
    working_directory: Optional[str] = None,
) -> str:
    """Make a peer, and nothing else, so the call cannot half succeed."""
    access = tool_context.current().session_access
    if access is None:
        return _unavailable("create_session_error")
    try:
        conversation_snapshot = tool_context.current().conversation_snapshot
        record = await access.create(
            agent=agent,
            working_directory=working_directory or access.working_directory,
            inherited_conversation=conversation_snapshot()
            if conversation_snapshot is not None
            else [],
        )
    except Exception as exception:  # noqa: BLE001 — surfaced to the model as a tool result
        return compact(
            {"code": "create_session_error", "status": "error", "message": str(exception)}
        )
    session_id = str(record.get("id") or "")
    return compact(
        {
            "code": "session_created",
            "status": "ok",
            "session": session_id,
            "agent": agent,
        }
    )


async def _message_session(session: str, message: str, explanation: str) -> str:
    """Hand a session a message, reporting a peer parked on a decision as an error rather than a delivery."""
    access = tool_context.current().session_access
    if access is None:
        return _unavailable("message_session_error")
    try:
        outcome = await access.send(session, message)
    except Exception as exception:  # noqa: BLE001
        return compact(
            {
                "code": "message_session_error",
                "status": "error",
                "session": session,
                "message": str(exception),
            }
        )
    # A peer parked on a decision does not take the message, and saying so is the point.
    if isinstance(outcome, dict) and outcome.get("awaiting_input"):
        return compact(
            {
                "code": "session_awaiting_input",
                "status": "error",
                "session": session,
                "message": _PROMPTS.load(
                    "session_awaiting_input",
                    {
                        "waiting_on": str(outcome.get("waiting_on") or "a decision from the user"),
                    },
                ).strip(),
            }
        )
    return compact({"code": "message_sent", "status": "ok", "session": session})


async def _read_session(session: str, explanation: str) -> str:
    """One session's record as it stands, for orienting rather than for waiting on."""
    access = tool_context.current().session_access
    if access is None:
        return _unavailable("read_session_error")
    try:
        record = await access.get(session)
    except Exception as exception:  # noqa: BLE001
        return compact(
            {
                "code": "read_session_error",
                "status": "error",
                "session": session,
                "message": str(exception),
            }
        )
    return compact({"code": "session", "status": "ok", **record})


async def _list_sessions(explanation: str) -> str:
    """This session's own subtree, which is the only part of the machine it is answerable for."""
    access = tool_context.current().session_access
    if access is None:
        return _unavailable("list_sessions_error")
    try:
        records = await access.children()
    except Exception as exception:  # noqa: BLE001
        return compact(
            {"code": "list_sessions_error", "status": "error", "message": str(exception)}
        )
    return compact({"code": "sessions", "status": "ok", "sessions": records})


async def _list_remote_agents(explanation: str) -> str:
    """The agents registered on other hosts, with the health of each, since an unreachable one is not a choice."""
    access = tool_context.current().session_access
    if access is None:
        return _unavailable("list_remote_agents_error")
    try:
        agents = await access.remote_list()
    except Exception as exception:  # noqa: BLE001
        return compact(
            {"code": "list_remote_agents_error", "status": "error", "message": str(exception)}
        )
    return compact({"code": "remote_agents", "status": "ok", "agents": agents})


async def _message_remote_agent(name: str, message: str, explanation: str) -> str:
    """One exchange with an agent on another host, whose reply is the whole of what comes back."""
    access = tool_context.current().session_access
    if access is None:
        return _unavailable("message_remote_agent_error")
    try:
        result = await access.remote_send(name, message)
    except Exception as exception:  # noqa: BLE001
        return compact(
            {
                "code": "message_remote_agent_error",
                "status": "error",
                "agent": name,
                "message": str(exception),
            }
        )
    return compact({"code": "remote_agent_replied", "status": "ok", "agent": name, **result})


def build_create_session_tool(agent_names: list[str]) -> BaseTool:
    """The create tool with the installed profiles baked into its schema, as an enumeration rather than a string."""
    names = tuple(sorted(agent_names))
    arguments = create_model(
        "CreateSessionArguments",
        explanation=(str, Field(description=EXPLANATION)),
        agent=(
            Literal[names],  # type: ignore[valid-type]
            Field(description="The agent profile the peer runs."),
        ),
        # Absent is how "the same as mine" is said, which a schema expresses by leaving a field out.
        working_directory=(
            Optional[str],
            Field(
                default=None,
                description="Where the peer works. Omit to use your working directory.",
            ),
        ),
    )
    return StructuredTool.from_function(
        coroutine=_create_session,
        name="create_session",
        description=_description("create_session"),
        args_schema=arguments,
    )


message_session_tool = StructuredTool.from_function(
    coroutine=_message_session,
    name="message_session",
    description=_description("message_session"),
    args_schema=create_model(
        "MessageSessionArguments",
        explanation=(str, Field(description=EXPLANATION)),
        session=(str, Field(description="The recipient session id.")),
        message=(str, Field(description="The message to send.")),
    ),
)


read_session_tool = StructuredTool.from_function(
    coroutine=_read_session,
    name="read_session",
    description=_description("read_session"),
    args_schema=create_model(
        "ReadSessionArguments",
        explanation=(str, Field(description=EXPLANATION)),
        session=(str, Field(description="The session id.")),
    ),
)


list_sessions_tool = StructuredTool.from_function(
    coroutine=_list_sessions,
    name="list_sessions",
    description=_description("list_sessions"),
    args_schema=create_model(
        "ListSessionsArguments",
        explanation=(str, Field(description=EXPLANATION)),
    ),
)


list_remote_agents_tool = StructuredTool.from_function(
    coroutine=_list_remote_agents,
    name="list_remote_agents",
    description=_description("list_remote_agents"),
    args_schema=create_model(
        "ListRemoteAgentsArguments",
        explanation=(str, Field(description=EXPLANATION)),
    ),
)


message_remote_agent_tool = StructuredTool.from_function(
    coroutine=_message_remote_agent,
    name="message_remote_agent",
    description=_description("message_remote_agent"),
    args_schema=create_model(
        "MessageRemoteAgentArguments",
        explanation=(str, Field(description=EXPLANATION)),
        name=(str, Field(description="The registered remote agent's name.")),
        message=(str, Field(description="The message to send.")),
    ),
)


def session_tools(agent_names: list[str]) -> list[BaseTool]:
    """Every peer-session tool, or none at all when there are no profiles to run."""
    if not agent_names:
        return []
    return [
        build_create_session_tool(agent_names),
        message_session_tool,
        read_session_tool,
        list_sessions_tool,
    ]


def remote_agent_tools() -> list[BaseTool]:
    return [list_remote_agents_tool, message_remote_agent_tool]


_TOOLS_BY_NAME: dict[str, Any] = {
    "message_session": message_session_tool,
    "read_session": read_session_tool,
    "list_sessions": list_sessions_tool,
    "list_remote_agents": list_remote_agents_tool,
    "message_remote_agent": message_remote_agent_tool,
}


async def invoke(tool_name: str, tool_arguments: dict, create_tool: BaseTool | None) -> str:
    """Run one session tool by name, with the create tool passed in because its schema is per-runtime."""
    if tool_name == "create_session":
        if create_tool is None:
            return _unavailable("create_session_error")
        tool = create_tool
    else:
        tool = _TOOLS_BY_NAME[tool_name]
    try:
        return await tool.ainvoke(tool_arguments)
    except ValidationError as exception:
        # Named, so the model can see which call to fix rather than which turn to mourn.
        return compact(
            {
                "code": "invalid_tool_arguments",
                "status": "error",
                "tool": tool_name,
                "message": str(exception),
            }
        )
