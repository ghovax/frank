"""End-to-end tests for the outbound A2A client (``harness.core.remote_agents``).

Each test points a ``RemoteAgentManager`` at a real, locally-served echo A2A agent and
drives it over real TCP, so card resolution, transport negotiation, streaming, and the
host-trust checks are all exercised for real.
"""

from a2a.client.helpers import create_text_message_object
from a2a.types import Message, Role, Task, TextPart

from harness.core.remote_agents import (
    RemoteAgentConfiguration,
    RemoteAgentManager,
    RemoteAgentTrustError,
    _assert_url_trusted,
)

from tests.echo_agent import running_echo_agent


def _texts_from_event(event) -> list[str]:
    """Collect every TextPart string carried by one send_message event (a (Task, Update)
    tuple or a bare Message)."""
    texts: list[str] = []
    messages = []
    if isinstance(event, Message):
        messages.append(event)
    elif isinstance(event, tuple):
        task, update = event
        if isinstance(task, Task):
            for artifact in (task.artifacts or []):
                for part in artifact.parts:
                    if isinstance(part.root, TextPart):
                        texts.append(part.root.text)
        status_message = getattr(getattr(update, "status", None), "message", None)
        if status_message is not None:
            messages.append(status_message)
    for message in messages:
        for part in message.parts:
            if isinstance(part.root, TextPart):
                texts.append(part.root.text)
    return texts


async def test_resolves_card_and_streams_echo():
    with running_echo_agent() as base_url:
        manager = RemoteAgentManager({
            "echo": RemoteAgentConfiguration(
                name="echo",
                card_url=f"{base_url}/.well-known/agent-card.json",
                allow_private=True,  # 127.0.0.1
            )
        })
        await manager.start()
        assert manager.health("echo")["health"] == "ok", manager.health("echo")
        assert manager.card("echo") is not None
        assert manager.card("echo").name == "echo"

        message = create_text_message_object(role=Role.user, content="hello")
        collected: list[str] = []
        async for event in manager.send_message("echo", message):
            collected.extend(_texts_from_event(event))
        await manager.aclose()

        assert any("echo: hello" in text for text in collected), collected


async def test_unknown_agent_raises():
    manager = RemoteAgentManager({})
    try:
        async for _ in manager.send_message("nope", create_text_message_object(content="x")):
            pass
        assert False, "expected LookupError"
    except LookupError:
        pass


async def test_private_host_refused_without_optin():
    with running_echo_agent() as base_url:
        manager = RemoteAgentManager({
            "echo": RemoteAgentConfiguration(
                name="echo",
                card_url=f"{base_url}/.well-known/agent-card.json",
                # allow_private defaults False -> a 127.0.0.1 target must be refused
            )
        })
        await manager.start()
        assert manager.health("echo")["health"] == "untrusted", manager.health("echo")


async def test_make_delegate_routes_to_remote_agent():
    """The locality-aware delegate reaches a remote agent over the wire and yields the
    same started/event/done vocabulary a local delegation does."""
    from harness.core.a2a_executor import AgentRegistry

    with running_echo_agent() as base_url:
        manager = RemoteAgentManager({
            "echo": RemoteAgentConfiguration(
                name="echo",
                card_url=f"{base_url}/.well-known/agent-card.json",
                allow_private=True,
            )
        })
        await manager.start()

        registry = AgentRegistry(task_store=object())  # task store unused on the remote path
        registry.set_remote_manager(manager)
        assert registry.is_remote_agent("echo")
        assert any(entry["id"] == "echo" for entry in registry.remote_roster())

        delegate = registry.make_delegate("ctx-1")
        events = []
        async for event in delegate("echo", "hello", "", None, 1, "", "", "", ""):
            events.append(event)
        await manager.aclose()

        kinds = [event["type"] for event in events]
        assert "started" in kinds, kinds
        assert kinds[-1] == "done", kinds
        relayed_text = [
            event["event"].get("text", "")
            for event in events
            if event["type"] == "event" and event["event"].get("kind") == "text"
        ]
        assert any("echo: hello" in text for text in relayed_text), events
        done = events[-1]
        artifacts = (done["task"] or {}).get("artifacts") or []
        artifact_text = [
            part.get("text", "")
            for artifact in artifacts
            for part in artifact.get("parts", [])
        ]
        assert any("echo: hello" in text for text in artifact_text), done


def test_origin_mismatch_blocked():
    configuration = RemoteAgentConfiguration(
        name="acme", card_url="https://agents.acme.com/.well-known/agent-card.json"
    )
    _assert_url_trusted("https://agents.acme.com/a2a", configuration)  # same origin: fine
    try:
        _assert_url_trusted("https://evil.example/a2a", configuration)
        assert False, "expected RemoteAgentTrustError"
    except RemoteAgentTrustError:
        pass
