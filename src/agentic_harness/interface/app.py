import asyncio
import json
from pathlib import Path

import httpx
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Static

from agentic_harness.core.configuration import (
    PromptLoader,
    load_agent_configuration,
    list_available_agents,
)

BRAILLE_SPINNER_PATTERNS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_templates = PromptLoader(Path(__file__).parent / "templates")
_agents_directory: str = "agents"


def _render(template_name: str, **variables: str) -> str:
    return _templates.load(template_name, variables)


def _agent_color(agent_name: str) -> str:
    try:
        configuration = load_agent_configuration(agent_name, _agents_directory)
        return configuration.color or "cyan"
    except Exception:
        return "cyan"


def _agent_label(agent_name: str) -> str:
    try:
        configuration = load_agent_configuration(agent_name, _agents_directory)
        return configuration.label or configuration.name.capitalize()
    except Exception:
        return agent_name.capitalize()


class PermissionDialog(ModalScreen):
    def __init__(
        self,
        request_identifier: str,
        command: str,
        justification: str,
        risk: str,
    ):
        super().__init__()
        self._request_identifier = request_identifier
        self._command = command
        self._justification = justification
        self._risk = risk
        self.decision = None

    def compose(self) -> ComposeResult:
        yield Static("Permission Required", id="dialog-title")
        yield Static(
            _render(
                "permission_dialog_body",
                command=self._command,
                justification=self._justification or "None provided",
                risk=self._risk or "None specified",
            ),
            id="dialog-message",
        )
        with Horizontal(id="dialog-actions"):
            yield Button("Allow", variant="primary", id="allow-button")
            yield Button("Deny", variant="error", id="deny-button")

    @on(Button.Pressed, "#allow-button")
    def on_allow(self):
        self.decision = "allow"
        self.dismiss()

    @on(Button.Pressed, "#deny-button")
    def on_deny(self):
        self.decision = "deny"
        self.dismiss()


class ThinkingSpinner(Static):
    def __init__(self):
        super().__init__("⠋", classes="thinking-message", id="spinner-widget")
        self._animation_index = 0

    def on_mount(self):
        self.set_interval(0.1, self._advance_spinner)

    def _advance_spinner(self):
        self._animation_index = (self._animation_index + 1) % len(BRAILLE_SPINNER_PATTERNS)
        self.update(BRAILLE_SPINNER_PATTERNS[self._animation_index])


class AgenticHarnessApp(App):
    TITLE = "agentic-harness"
    CSS_PATH = "app.tcss"

    current_agent = reactive("main")
    available_agents: list[str] = []
    agents_directory: str = "agents"
    server_url: str = "http://127.0.0.1:8822"
    session_id: str | None = None

    def __init__(self, agents_directory: str = "agents", server_url: str = "http://127.0.0.1:8822"):
        super().__init__()
        global _agents_directory
        _agents_directory = agents_directory
        self.agents_directory = agents_directory
        self.server_url = server_url
        self.available_agents = list_available_agents(agents_directory)
        if self.available_agents:
            self.current_agent = self.available_agents[0]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="chat-view"):
            yield Static("[bold]Welcome to agentic-harness[/bold]", classes="system-message")
            yield Static(
                _render(
                    "agent_indicator",
                    color=_agent_color(self.current_agent),
                    agent_name=_agent_label(self.current_agent),
                ),
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

    def watch_current_agent(self, previous_agent: str, next_agent: str) -> None:
        indicator = self.query_one("#agent-indicator", Static)
        indicator.update(
            _render(
                "agent_indicator",
                color=_agent_color(next_agent),
                agent_name=_agent_label(next_agent),
            )
        )

    @on(Input.Submitted, "#prompt-input")
    async def on_input(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()

        chat_view = self.query_one("#chat-view", VerticalScroll)
        agent_label = _agent_label(self.current_agent)

        await chat_view.mount(
            Static(_render("user_message", text=text), classes="user-message")
        )
        await chat_view.mount(
            Static(
                _render(
                    "agent_message",
                    color=_agent_color(self.current_agent),
                    agent_name=agent_label,
                    text="",
                ),
                classes="agent-message",
                id="thinking-label",
            )
        )
        spinner = ThinkingSpinner()
        await chat_view.mount(spinner)
        chat_view.scroll_end(animate=False)

        await self._send_message(text, chat_view, spinner)

    async def _send_message(
        self, text: str, chat_view: VerticalScroll, spinner: ThinkingSpinner
    ) -> None:
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                payload = {"message": text, "agent": self.current_agent}
                if self.session_id:
                    payload["session_id"] = self.session_id

                async with client.stream(
                    "POST",
                    f"{self.server_url}/chat",
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        error_text = await response.aread()
                        await self._cleanup_thinking(chat_view, spinner)
                        await chat_view.mount(
                            Static(
                                _render(
                                    "error_message",
                                    message=f"Server error ({response.status_code}): {error_text.decode()}",
                                ),
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
                            data_content = line[6:]
                            try:
                                data = json.loads(data_content)
                            except json.JSONDecodeError:
                                continue

                            await self._handle_event(
                                event_type,
                                data,
                                chat_view,
                                spinner,
                                full_text,
                                tool_calls_this_response,
                            )

                            if data.get("type") == "done":
                                full_text = data.get("text", full_text)
                                await self._cleanup_thinking(chat_view, spinner)
                                self._remove_widget_by_id(chat_view, "thinking-label")
                                agent_label = _agent_label(self.current_agent)
                                await chat_view.mount(
                                    Static(
                                        _render(
                                            "agent_message",
                                            color=_agent_color(self.current_agent),
                                            agent_name=agent_label,
                                            text=full_text,
                                        ),
                                        classes="agent-message",
                                    )
                                )
                                chat_view.scroll_end(animate=False)

        except httpx.ConnectError:
            await self._cleanup_thinking(chat_view, spinner)
            await chat_view.mount(
                Static(
                    _render(
                        "error_message",
                        message=f"Cannot reach server at {self.server_url}. Is the server running?",
                    ),
                    classes="error-message",
                )
            )
            chat_view.scroll_end(animate=False)
        except Exception as exception:
            await self._cleanup_thinking(chat_view, spinner)
            await chat_view.mount(
                Static(
                    _render("error_message", message=str(exception)),
                    classes="error-message",
                )
            )
            chat_view.scroll_end(animate=False)

    async def _handle_event(
        self,
        event_type: str,
        data: dict,
        chat_view: VerticalScroll,
        spinner: ThinkingSpinner,
        full_text: str,
        tool_calls: list,
    ) -> None:
        data_type = data.get("type", event_type)

        if data_type == "session":
            self.session_id = data.get("session_id")

        elif data_type == "thinking":
            await self._cleanup_thinking(chat_view, spinner)

        elif data_type == "tool_call":
            name = data.get("name", "unknown")
            arguments = data.get("args", {})
            justification = arguments.get("justification", "")
            risk = arguments.get("risk", "")
            arguments_preview = json.dumps(arguments, indent=2)[:200]
            tool_calls.append(data)

            justification_line = f"[dim]Why: {justification}[/]\n" if justification else ""
            risk_line = f"[dim]Risk: {risk}[/]\n" if risk else ""
            arguments_line = f"[dim]{arguments_preview}[/]"

            await chat_view.mount(
                Static(
                    _render(
                        "tool_call",
                        tool_name=name,
                        justification_text=justification_line,
                        risk_text=risk_line,
                        arguments_text=arguments_line,
                    ),
                    classes="tool-panel",
                )
            )
            chat_view.scroll_end(animate=False)

        elif data_type == "tool_result":
            name = data.get("name", "unknown")
            result = data.get("result", "")
            result_text = _truncate(str(result), 300)

            await chat_view.mount(
                Static(
                    _render("tool_result", tool_name=name, result_text=result_text),
                    classes="tool-result",
                )
            )
            chat_view.scroll_end(animate=False)

        elif data_type == "background_started":
            task_identifier = data.get("task_id", "")
            agent_name = data.get("agent", "")
            await chat_view.mount(
                Static(
                    _render(
                        "background_started",
                        agent_name=agent_name,
                        task_identifier=task_identifier,
                    ),
                    classes="system-message",
                )
            )
            chat_view.scroll_end(animate=False)

        elif data_type == "permission_request":
            await self._cleanup_thinking(chat_view, spinner)
            request_identifier = data.get("request_id", "")
            command = data.get("command", "")
            justification = data.get("justification", "")
            risk = data.get("risk", "")

            dialog = PermissionDialog(request_identifier, command, justification, risk)
            await self.push_screen(dialog)

            decision = getattr(dialog, "decision", None)
            if decision:
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        await client.post(
                            f"{self.server_url}/chat/{self.session_id}/permission",
                            json={"request_id": request_identifier, "decision": decision},
                        )
                except Exception:
                    pass

            if decision == "deny":
                await chat_view.mount(
                    Static(_render("permission_denied"), classes="error-message")
                )
                chat_view.scroll_end(animate=False)
            elif decision == "allow":
                await chat_view.mount(
                    Static(_render("permission_approved"), classes="system-message")
                )
                chat_view.scroll_end(animate=False)

        elif data_type == "error":
            await self._cleanup_thinking(chat_view, spinner)
            message = data.get("message", "")
            await chat_view.mount(
                Static(
                    _render("error_message", message=message),
                    classes="error-message",
                )
            )
            chat_view.scroll_end(animate=False)

    @staticmethod
    async def _cleanup_thinking(
        chat_view: VerticalScroll, spinner: ThinkingSpinner
    ) -> None:
        try:
            if spinner and spinner.parent:
                await spinner.remove()
        except Exception:
            pass

    @staticmethod
    def _remove_widget_by_id(container: VerticalScroll, widget_id: str) -> None:
        try:
            widget = container.query_one(f"#{widget_id}")
            if widget:
                widget.remove()
        except Exception:
            pass

    def key_tab(self) -> None:
        if not self.available_agents:
            return
        current_index = self.available_agents.index(self.current_agent)
        next_index = (current_index + 1) % len(self.available_agents)
        next_agent = self.available_agents[next_index]
        old_session_id = self.session_id
        self.current_agent = next_agent
        self.session_id = None
        asyncio.create_task(self._switch_agent(old_session_id, next_agent))

    async def _switch_agent(self, old_session_id: str | None, next_agent: str) -> None:
        if old_session_id:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"{self.server_url}/chat/{old_session_id}/switch",
                        params={"agent": next_agent},
                    )
            except Exception:
                pass

        chat_view = self.query_one("#chat-view", VerticalScroll)
        color = _agent_color(next_agent)
        label = _agent_label(next_agent)
        await chat_view.mount(
            Static(
                _render("agent_switched", color=color, agent_name=label),
                classes="system-message",
            )
        )
        chat_view.scroll_end(animate=False)

    def key_ctrl_c(self) -> None:
        self.exit()


def _truncate(text: str, maximum_length: int = 300) -> str:
    if len(text) > maximum_length:
        return text[:maximum_length] + "..."
    return text


def run_interface(
    agents_directory: str = "agents",
    server_url: str = "http://127.0.0.1:8822",
) -> None:
    application = AgenticHarnessApp(
        agents_directory=agents_directory,
        server_url=server_url,
    )
    application.run()
