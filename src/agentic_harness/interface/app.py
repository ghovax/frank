import asyncio
import json

import httpx
from textual import on
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Footer, Header, Input, Static

from agentic_harness.core.agent_configuration import list_available_agents

AGENT_COLORS = {
    "main": "cyan",
    "explore": "green",
    "code": "yellow",
}

AGENT_LABELS = {
    "main": "Main Agent",
    "explore": "Explore Agent",
    "code": "Code Agent",
}


class AgenticHarnessApp(App):
    TITLE = "agentic-harness"
    CSS = """
    Screen {
        background: $surface;
    }

    #chat-view {
        height: 1fr;
        border: solid $primary;
        padding: 0 1;
        overflow-y: scroll;
    }

    #input-container {
        dock: bottom;
        height: 3;
        padding: 0 1;
    }

    #prompt-input {
        width: 100%;
    }

    .user-message {
        color: $text;
        margin: 1 0;
        padding: 0 1;
    }

    .agent-message {
        color: $text;
        margin: 1 0;
        padding: 0 1;
        border-left: solid $accent;
    }

    .tool-panel {
        margin: 0 2;
        padding: 0 1;
        border: dashed $warning;
        color: $warning;
    }

    .tool-result {
        margin: 0 2;
        padding: 0 1;
        border: dashed $success;
        color: $success;
    }

    .status-bar {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $primary;
        color: $text;
    }

    #agent-indicator {
        padding: 0 1;
    }

    .error-message {
        color: $error;
        margin: 1 0;
        padding: 0 1;
        border: solid $error;
    }

    .system-message {
        color: $text-muted;
        margin: 1 0;
        padding: 0 1;
        font-style: italic;
    }
    """

    current_agent = reactive("main")
    available_agents: list[str] = []
    agents_dir: str = "agents"
    server_url: str = "http://127.0.0.1:8822"
    session_id: Optional[str] = None

    def __init__(self, agents_directory: str = "agents", server_url: str = "http://127.0.0.1:8822"):
        super().__init__()
        self.agents_dir = agents_directory
        self.server_url = server_url
        self.available_agents = list_available_agents(agents_directory)
        self.current_agent = self.available_agents[0] if self.available_agents else "main"

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="chat-view"):
            yield Static("[bold]Welcome to agentic-harness[/bold]", classes="system-message")
            yield Static(
                f"Active agent: [bold {AGENT_COLORS.get(self.current_agent, 'cyan')}]"
                f"{AGENT_LABELS.get(self.current_agent, self.current_agent)}[/] "
                f"(Tab to switch)",
                id="agent-indicator",
                classes="system-message",
            )
        yield Input(
            placeholder="Type a message... (Tab to switch agents)",
            id="prompt-input",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def watch_current_agent(self, old_value: str, new_value: str) -> None:
        indicator = self.query_one("#agent-indicator", Static)
        color = AGENT_COLORS.get(new_value, "cyan")
        label = AGENT_LABELS.get(new_value, new_value)
        indicator.update(
            f"Active agent: [bold {color}]{label}[/] "
            f"(Tab to switch)"
        )

    @on(Input.Submitted, "#prompt-input")
    async def on_input(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()

        chat_view = self.query_one("#chat-view", VerticalScroll)
        color = AGENT_COLORS.get(self.current_agent, "cyan")

        await chat_view.mount(
            Static(f"[bold]You:[/bold] {text}", classes="user-message")
        )
        await chat_view.mount(
            Static(
                f"[bold {color}]{AGENT_LABELS.get(self.current_agent, self.current_agent)}:[/] thinking...",
                classes="agent-message",
                id="thinking",
            )
        )
        chat_view.scroll_end(animate=False)

        await self._send_message(text, chat_view)

    async def _send_message(self, text: str, chat_view: VerticalScroll) -> None:
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                payload = {
                    "message": text,
                    "agent": self.current_agent,
                }
                if self.session_id:
                    payload["session_id"] = self.session_id

                async with client.stream(
                    "POST",
                    f"{self.server_url}/chat",
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        error_text = await response.aread()
                        self._remove_thinking(chat_view)
                        await chat_view.mount(
                            Static(
                                f"[bold red]Server error ({response.status_code}):[/] {error_text.decode()}",
                                classes="error-message",
                            )
                        )
                        chat_view.scroll_end(animate=False)
                        return

                    full_text = ""
                    tool_calls_this_response = []

                    async for line in response.aiter_lines():
                        if line.startswith("event: "):
                            event_type = line[7:]
                        elif line.startswith("data: "):
                            data_str = line[6:]
                            try:
                                data = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            self._process_event(
                                event_type, data, chat_view,
                                full_text, tool_calls_this_response,
                            )

                            if data.get("type") in ("done", "error"):
                                if data.get("type") == "done":
                                    full_text = data.get("text", full_text)
                                elif data.get("type") == "error":
                                    pass

                            if data.get("type") == "done":
                                self._remove_thinking(chat_view)
                                color = AGENT_COLORS.get(self.current_agent, "cyan")
                                await chat_view.mount(
                                    Static(
                                        f"[bold {color}]{AGENT_LABELS.get(self.current_agent, self.current_agent)}:[/] {full_text}",
                                        classes="agent-message",
                                    )
                                )
                                chat_view.scroll_end(animate=False)

        except httpx.ConnectError:
            self._remove_thinking(chat_view)
            await chat_view.mount(
                Static(
                    "[bold red]Connection error:[/] Could not reach server at "
                    f"{self.server_url}. Is the server running?",
                    classes="error-message",
                )
            )
            chat_view.scroll_end(animate=False)
        except Exception as e:
            self._remove_thinking(chat_view)
            await chat_view.mount(
                Static(
                    f"[bold red]Error:[/] {e}",
                    classes="error-message",
                )
            )
            chat_view.scroll_end(animate=False)

    def _process_event(self, event_type: str, data: dict, chat_view, full_text, tool_calls):
        asyncio.create_task(self._async_process_event(event_type, data, chat_view, full_text, tool_calls))

    async def _async_process_event(self, event_type: str, data: dict, chat_view, full_text, tool_calls):
        data_type = data.get("type", event_type)

        if data_type == "session":
            self.session_id = data.get("session_id")
        elif data_type == "status":
            pass
        elif data_type == "tool_call":
            name = data.get("name", "unknown")
            args = data.get("args", {})
            args_preview = json.dumps(args, indent=2)[:200]
            tool_calls.append(data)
            await chat_view.mount(
                Static(
                    f"[bold yellow]Tool:[/] {name}\n[dim]{args_preview}[/]",
                    classes="tool-panel",
                )
            )
            chat_view.scroll_end(animate=False)
        elif data_type == "tool_result":
            name = data.get("name", "unknown")
            result = data.get("result", "")
            result_preview = str(result)[:300]
            await chat_view.mount(
                Static(
                    f"[bold green]Result ({name}):[/]\n{dim(result_preview)}",
                    classes="tool-result",
                )
            )
            chat_view.scroll_end(animate=False)
        elif data_type == "background_started":
            task_id = data.get("task_id", "")
            agent_name = data.get("agent", "")
            await chat_view.mount(
                Static(
                    f"[bold magenta]Background Agent:[/] {agent_name} ({task_id}) started",
                    classes="system-message",
                )
            )
            chat_view.scroll_end(animate=False)
        elif data_type == "error":
            message = data.get("message", "")
            await chat_view.mount(
                Static(
                    f"[bold red]Error:[/] {message}",
                    classes="error-message",
                )
            )
            chat_view.scroll_end(animate=False)

    def _remove_thinking(self, chat_view: VerticalScroll) -> None:
        try:
            thinking = chat_view.query_one("#thinking", Static)
            if thinking:
                thinking.remove()
        except Exception:
            pass

    def key_tab(self) -> None:
        if not self.available_agents:
            return
        current_idx = self.available_agents.index(self.current_agent)
        next_idx = (current_idx + 1) % len(self.available_agents)
        new_agent = self.available_agents[next_idx]
        old_session_id = self.session_id
        self.current_agent = new_agent
        self.session_id = None

        asyncio.create_task(self._switch_agent(old_session_id, new_agent, current_idx, next_idx))

    async def _switch_agent(self, old_session_id, new_agent, current_idx, next_idx):
        if old_session_id:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"{self.server_url}/chat/{old_session_id}/switch?agent={new_agent}",
                    )
            except Exception:
                pass

        chat_view = self.query_one("#chat-view", VerticalScroll)
        color = AGENT_COLORS.get(new_agent, "cyan")
        label = AGENT_LABELS.get(new_agent, new_agent)
        await chat_view.mount(
            Static(
                f"[dim]→ Switched to [bold {color}]{label}[/][/dim]",
                classes="system-message",
            )
        )
        chat_view.scroll_end(animate=False)


def create_agent_names():
    return AGENT_LABELS, AGENT_COLORS


def dim(text: str, max_len: int = 300) -> str:
    """Truncate text for display."""
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def run_interface(agents_directory: str = "agents", server_url: str = "http://127.0.0.1:8822"):
    app = AgenticHarnessApp(
        agents_directory=agents_directory,
        server_url=server_url,
    )
    app.run()
