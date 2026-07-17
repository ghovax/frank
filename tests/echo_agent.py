"""A minimal in-repo A2A agent used as a real peer in tests.

It stands up a genuine A2A JSON-RPC server (the SDK's ``A2AFastAPIApplication`` +
``DefaultRequestHandler``) that echoes whatever text it receives, served over real TCP by
uvicorn on an ephemeral port. This lets the outbound client be exercised end-to-end —
card resolution, transport negotiation, streaming — without any external network.
"""

import socket
import threading
import time
from contextlib import contextmanager

import uvicorn

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps.jsonrpc import A2AFastAPIApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    FilePart,
    Part,
    TaskState,
    TextPart,
)
from a2a.utils import new_task


class EchoExecutor(AgentExecutor):
    """Streams one working update and one result artifact, both echoing the input."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        text = context.get_user_input()
        file_names = [
            (root.file.name or "file")
            for part in (context.message.parts if context.message else [])
            if isinstance((root := getattr(part, "root", part)), FilePart)
        ]
        reply = f"echo: {text}"
        if file_names:
            reply += f" files: {', '.join(file_names)}"
        task = context.current_task or new_task(context.message)
        await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()
        await updater.update_status(
            TaskState.working,
            updater.new_agent_message([Part(root=TextPart(text=reply))]),
        )
        await updater.add_artifact([Part(root=TextPart(text=reply))], name="result", last_chunk=True)
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task is not None:
            updater = TaskUpdater(event_queue, context.current_task.id, context.current_task.context_id)
            await updater.cancel()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_echo_app(base_url: str):
    card = AgentCard(
        name="echo",
        description="Echo test agent.",
        url=f"{base_url}/",
        version="1.0.0",
        protocol_version="0.3.0",
        preferred_transport="JSONRPC",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[AgentSkill(id="echo", name="echo", description="Echoes its input.", tags=["test"])],
    )
    handler = DefaultRequestHandler(agent_executor=EchoExecutor(), task_store=InMemoryTaskStore())
    return A2AFastAPIApplication(agent_card=card, http_handler=handler).build()


@contextmanager
def running_echo_agent():
    """Run the echo agent on an ephemeral port in a background thread; yield its base URL."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    app = build_echo_app(base_url)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not server.started:
            raise RuntimeError("echo agent did not start in time")
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)
