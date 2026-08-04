"""Peer sessions, as tools.

A session composes with another session by messaging it. That is the harness's one
composition path, and these tools are how a model walks it — the same control-plane API the
`frank` command and the desktop client call, reached directly instead of through a shell.

Doing it through the shell was the earlier design, and it could not hold. A session that runs
`frank create` writes argv as free text, and free text cannot carry what makes a peer a *peer*:
the caller's own identity. Nothing propagated a session id into the environment its `bash`
tool ran in, so every peer created that way was born unparented — outside the tree, outside
the reaper, and outside the permission clamp, which is skipped entirely when there is no
parent to clamp against. A read-only session could create an unrestricted one. It was also
not portable: in the packaged application the daemon re-execs a bundled helper, and there is
no `frank` on the path at all.

A typed call fixes all of that by construction rather than by instruction. `parent` is not an
argument here — it is the calling session, always, so the clamp and the reaper cannot be
opted out of. The agent profile is an enumeration built from the live catalogue, so an
invented name is unrepresentable rather than refused after the fact. And the working
directory defaults to the caller's, which is what a peer working on the same problem needs
and what a model would otherwise have to remember to pass.

There is no waiting, either as an argument or behind the scenes. A peer *answers* — it calls
`message_session` on the session that created it, and its answer arrives there as an ordinary
inbound message. So a caller starts the work, carries on with whatever does not depend on it,
and ends its turn; the peer's reply wakes it the way a person's message would.

That replaced a mechanism worth naming, because its absence is the point. The caller used to
subscribe to the peer's event stream, wait for the edge where its turn stopped running, read
its last task out of the store, and reconstruct prose by walking the result artifact and then
the final status message for text parts. It reached into another session's durable record to
recover something the peer already knew, and it decided by string surgery which fragments of
a transcript constituted "the answer" — a judgement only the peer can honestly make.

There is deliberately no permission gate on creating a peer. The clamp is the control: a
child can never hold authority its parent lacks, so creating one grants nothing that asking
for it would not. Sending to an agent on *another host* is a different matter — that leaves
the machine — and stays governed by the per-profile authorization the remote agent registry
already applies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional, Protocol, runtime_checkable

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import Field, ValidationError, create_model

from frank.base.configuration import PromptLoader
from frank.base.serialization import compact
from frank.runtime.tools import context as tool_context
from frank.runtime.tools.registry import EXPLANATION

# A tool's description is the guidance a model reads to decide whether and how to call it —
# model-facing prose, and so it lives in a prompt template like every other piece of it. The
# field descriptions stay here: those are schema annotations naming what an argument is, not
# advice about when to use it.
_PROMPTS = PromptLoader(Path(__file__).resolve().parent.parent / "prompts")


def _description(tool_name: str) -> str:
    text = _PROMPTS.load(f"tool_{tool_name}", {}).strip()
    if not text:
        # The loader answers a missing template with an empty string, and an empty description
        # is a tool the model is handed with no idea what it does. Fail at import instead of
        # shipping one.
        raise ValueError(f"No description template for the {tool_name!r} tool.")
    return text


@runtime_checkable
class SessionAccess(Protocol):
    """What a session needs in order to work with its peers.

    Implemented by the worker, which is the layer that holds the session's identity and its
    connection to the daemon. It reaches the tools through the bound tool context, so the
    runtime can offer them without importing the layer above it and without a module global
    that would make two sessions in one process share one identity."""

    session_id: str
    working_directory: str
    permission_mode: str

    async def create(self, *, agent: str, working_directory: str) -> dict: ...
    async def send(self, session_id: str, text: str) -> None: ...
    async def get(self, session_id: str) -> dict: ...
    async def children(self) -> list[dict]: ...
    async def end(self, session_id: str) -> dict: ...
    async def remote_list(self) -> list[dict]: ...
    async def remote_send(self, name: str, text: str) -> dict: ...


def _unavailable(code: str) -> str:
    return compact({
        "code": code,
        "status": "error",
        "message": "Peer sessions are not available in this process.",
    })


async def _create_session(
    agent: str,
    working_directory: Optional[str] = None,
    explanation: str = "",
) -> str:
    """Make a peer, and nothing else.

    Creating and briefing used to be one call, which made this the only tool that could half
    succeed: a failed send returned an error *and* a live session, so a caller reading the
    error had already leaked a process it was not told to clean up. Every other layer has
    always kept them apart — the daemon's `session.create` takes no message, and a person types
    `frank create` then `frank send` — so the tool was the one place a session composed with a
    peer differently from the way a person does."""
    access = tool_context.current().session_access
    if access is None:
        return _unavailable("create_session_error")
    try:
        record = await access.create(
            agent=agent,
            working_directory=working_directory or access.working_directory,
        )
    except Exception as exception:  # noqa: BLE001 — surfaced to the model as a tool result
        return compact({"code": "create_session_error", "status": "error", "message": str(exception)})
    session_id = str(record.get("id") or "")
    return compact({
        "code": "session_created",
        "status": "ok",
        "session": session_id,
        "agent": agent,
    })


async def _message_session(session: str, message: str, explanation: str = "") -> str:
    access = tool_context.current().session_access
    if access is None:
        return _unavailable("message_session_error")
    try:
        outcome = await access.send(session, message)
    except Exception as exception:  # noqa: BLE001
        return compact({
            "code": "message_session_error", "status": "error",
            "session": session, "message": str(exception),
        })
    # A peer parked on a human decision does not take the message, and saying so is the point.
    # The alternative — which is what happened — is that the send reports success, the peer never
    # acts on it, and the sender concludes the peer is broken and replaces it.
    if isinstance(outcome, dict) and outcome.get("awaiting_input"):
        return compact({
            "code": "session_awaiting_input", "status": "error", "session": session,
            "message": _PROMPTS.load("session_awaiting_input", {
                "waiting_on": str(outcome.get("waiting_on") or "a decision from the user"),
            }).strip(),
        })
    return compact({"code": "message_sent", "status": "ok", "session": session})


async def _read_session(session: str, explanation: str = "") -> str:
    access = tool_context.current().session_access
    if access is None:
        return _unavailable("read_session_error")
    try:
        record = await access.get(session)
    except Exception as exception:  # noqa: BLE001
        return compact({
            "code": "read_session_error", "status": "error",
            "session": session, "message": str(exception),
        })
    return compact({"code": "session", "status": "ok", **record})


async def _list_sessions(explanation: str = "") -> str:
    access = tool_context.current().session_access
    if access is None:
        return _unavailable("list_sessions_error")
    try:
        records = await access.children()
    except Exception as exception:  # noqa: BLE001
        return compact({"code": "list_sessions_error", "status": "error", "message": str(exception)})
    return compact({"code": "sessions", "status": "ok", "sessions": records})


async def _list_remote_agents(explanation: str = "") -> str:
    access = tool_context.current().session_access
    if access is None:
        return _unavailable("list_remote_agents_error")
    try:
        agents = await access.remote_list()
    except Exception as exception:  # noqa: BLE001
        return compact({"code": "list_remote_agents_error", "status": "error", "message": str(exception)})
    return compact({"code": "remote_agents", "status": "ok", "agents": agents})


async def _message_remote_agent(name: str, message: str, explanation: str = "") -> str:
    access = tool_context.current().session_access
    if access is None:
        return _unavailable("message_remote_agent_error")
    try:
        result = await access.remote_send(name, message)
    except Exception as exception:  # noqa: BLE001
        return compact({
            "code": "message_remote_agent_error", "status": "error",
            "agent": name, "message": str(exception),
        })
    return compact({"code": "remote_agent_replied", "status": "ok", "agent": name, **result})


def build_create_session_tool(agent_names: list[str]) -> BaseTool:
    """The create tool, with the installed agent profiles baked into its schema.

    An enumeration rather than a free string, because "never invent a profile name" is an
    instruction a model can disobey, while a schema it cannot satisfy is one it cannot get
    wrong. The catalogue is read when the runtime is built, so a profile added while a session
    is running appears on its next rebuild."""
    names = tuple(sorted(agent_names))
    arguments = create_model(
        "CreateSessionArguments",
        agent=(
            Literal[names],  # type: ignore[valid-type]
            Field(description="The agent profile the peer runs."),
        ),
        # Absent is how "the same as mine" is said, which a schema expresses by leaving a field
        # out rather than by offering an empty string among the choices.
        #
        # There is deliberately no `permission_mode`. How much a session may do without a person
        # is the person's policy, not a parameter for the thing being governed to set: a peer
        # works the way its creator works, narrowed by whatever ceiling its agent profile
        # declares, and both of those are decided outside this call. Offering the choice also
        # meant the modes had to be explained to a model that has no use for the vocabulary — it
        # declares a risk and a reason for each call, and what is done with that is not its
        # business.
        working_directory=(
            Optional[str],
            Field(default=None, description="Where the peer works. Omit to use your working directory."),
        ),
        explanation=(str, Field(description=EXPLANATION)),
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
        session=(str, Field(description="The recipient session id.")),
        message=(str, Field(description="The message to send.")),
        explanation=(str, Field(description=EXPLANATION)),
    ),
)


read_session_tool = StructuredTool.from_function(
    coroutine=_read_session,
    name="read_session",
    description=_description("read_session"),
    args_schema=create_model(
        "ReadSessionArguments",
        session=(str, Field(description="The session id.")),
        explanation=(str, Field(description=EXPLANATION)),
    ),
)


list_sessions_tool = StructuredTool.from_function(
    coroutine=_list_sessions,
    name="list_sessions",
    description=_description("list_sessions"),
    args_schema=create_model("ListSessionsArguments",
        explanation=(str, Field(description=EXPLANATION)),
    ),
)


list_remote_agents_tool = StructuredTool.from_function(
    coroutine=_list_remote_agents,
    name="list_remote_agents",
    description=_description("list_remote_agents"),
    args_schema=create_model("ListRemoteAgentsArguments",
        explanation=(str, Field(description=EXPLANATION)),
    ),
)


message_remote_agent_tool = StructuredTool.from_function(
    coroutine=_message_remote_agent,
    name="message_remote_agent",
    description=_description("message_remote_agent"),
    args_schema=create_model(
        "MessageRemoteAgentArguments",
        name=(str, Field(description="The registered remote agent's name.")),
        message=(str, Field(description="The message to send.")),
        explanation=(str, Field(description=EXPLANATION)),
    ),
)


def session_tools(agent_names: list[str]) -> list[BaseTool]:
    """Every peer-session tool, or none at all when there are no profiles to run.

    Offering `create_session` with an empty enumeration would be offering a tool that cannot
    be called successfully, which costs context and invites an attempt."""
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
    """Run one session tool by name. `create_session` is passed in because its schema is built
    per-runtime from the live agent catalogue rather than being a module-level singleton.

    A call whose arguments do not fit its schema comes back as a tool error the model can read
    and correct. It used to come back as a *turn failure*: these tools are dispatched here rather
    than through the preflight that validates every other tool's arguments, so the schema was
    enforced by the invocation itself and the exception escaped as "the turn stopped
    unexpectedly". A model that sent ten briefs and forgot the `session` field on each was told
    ten times that its turn had failed, and concluded the peers were unreachable — so it created
    three more and briefed them again, which is the duplicate work a person then has to unpick.
    """
    if tool_name == "create_session":
        if create_tool is None:
            return _unavailable("create_session_error")
        tool = create_tool
    else:
        tool = _TOOLS_BY_NAME[tool_name]
    try:
        return await tool.ainvoke(tool_arguments)
    except ValidationError as exception:
        # Named, so the model can see which call to fix rather than which turn to mourn. The
        # missing field is in the message pydantic wrote; the tool name is not, and with ten
        # calls in flight it is the part that says where to look.
        return compact({
            "code": "invalid_tool_arguments",
            "status": "error",
            "tool": tool_name,
            "message": str(exception),
        })
