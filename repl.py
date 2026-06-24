#!/usr/bin/env python3

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, RichLog, Static

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from harness.core.agent import AgentOrchestrator, StreamEvent
from harness.core.configuration import (
    GlobalConfiguration,
    load_agent_configuration,
    list_available_agents,
)
from harness.tools.tools import set_exa_client, bash_tasks, web_tasks, spawned_tasks


SESSIONS_DIR = Path.home() / ".harness_sessions"

COMMANDS = {
    "/help": "show available commands",
    "/agents": "list agent profiles",
    "/switch": "switch agent (/switch <name>)",
    "/abort": "cancel all tasks",
    "/agent": "show current agent info",
    "/session": "show session details",
    "/sessions": "list saved sessions",
    "/resume": "resume last session (/resume [id])",
    "/history": "show execution history",
    "/jobs": "show running background tasks",
    "/clear": "clear and start new session",
    "/exit": "exit",
}


class TabCompleter:
    """Cycling tab completer for slash commands and their arguments."""

    def __init__(self):
        self._candidates: list[str] = []
        self._labels: list[str] = []
        self._index: int = -1
        self._prefix: str = ""

    def reset(self) -> None:
        self._candidates = []
        self._labels = []
        self._index = -1
        self._prefix = ""

    @property
    def active(self) -> bool:
        return len(self._candidates) > 0

    @property
    def display(self) -> str:
        if not self._labels:
            return ""
        parts = []
        for index, label in enumerate(self._labels):
            if index == self._index:
                parts.append(f"[bold cyan]{label}[/bold cyan]")
            else:
                parts.append(f"[dim]{label}[/dim]")
        return "  ".join(parts)

    def complete(self, text: str, app: "HarnessApp") -> str | None:
        if not self._candidates or self._prefix != self._extract_prefix(text):
            self._build_candidates(text, app)
            if not self._candidates:
                return None
            self._index = 0
        else:
            self._index = (self._index + 1) % len(self._candidates)
        return self._candidates[self._index]

    def _extract_prefix(self, text: str) -> str:
        if not text.startswith("/"):
            return ""
        parts = text.split(maxsplit=1)
        has_space = len(parts) > 1 or text.endswith(" ")
        if has_space:
            return parts[0] + " "
        return "/"

    def _build_candidates(self, text: str, app: "HarnessApp") -> None:
        self._candidates = []
        self._labels = []
        self._index = -1

        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=1)
        command_part = parts[0]
        has_space = len(parts) > 1 or text.endswith(" ")

        if not has_space:
            self._prefix = "/"
            for cmd in COMMANDS:
                if cmd.startswith(command_part):
                    self._candidates.append(cmd + " ")
                    self._labels.append(cmd)
        else:
            self._prefix = command_part + " "
            argument_text = parts[1] if len(parts) > 1 else ""
            if command_part in ("/switch", "/agent"):
                for name in app._agents:
                    if name.startswith(argument_text):
                        self._candidates.append(f"{command_part} {name}")
                        self._labels.append(name)
            elif command_part == "/resume":
                for session in app._list_sessions()[:10]:
                    short_id = session["id"][:12]
                    preview = session.get("preview", "")[:30]
                    if short_id.startswith(argument_text):
                        self._candidates.append(f"{command_part} {short_id}")
                        label = f"{short_id} ({preview})" if preview else short_id
                        self._labels.append(label)


class StatusBar(Static):
    """Top status bar showing agent, session, state, and background task count."""

    DEFAULT_CSS = """
    StatusBar {
        dock: top;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    """


class ShortcutBar(Static):
    """Bottom shortcut hint bar."""

    DEFAULT_CSS = """
    ShortcutBar {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    """


class HarnessApp(App):
    """Textual TUI for the harness agent orchestrator."""

    CSS = """
    Screen {
        background: transparent;
        layout: vertical;
    }
    #output {
        height: 1fr;
        background: transparent;
        scrollbar-size: 1 1;
    }
    #separator {
        dock: bottom;
        height: 1;
        background: $surface-lighten-1;
    }
    #input {
        dock: bottom;
        height: 2;
        border: none;
        padding: 0 1;
        margin: 0;
    }
    #input:focus {
        border: none;
    }
    """

    BINDINGS = [
        Binding("escape", "abort", "Abort", show=True),
        Binding("ctrl+o", "toggle_verbose", "Verbose", show=True),
        Binding("ctrl+c", "clear_or_quit", "Quit", show=True),
        Binding("i", "focus_input", "Input", show=True, priority=True),
    ]

    _INTERNAL_KEYS = frozenset({
        "code", "pid", "size", "task_identifier", "output_file", "query",
    })

    def __init__(self):
        super().__init__()
        self._config = GlobalConfiguration.from_yaml("configuration.yaml")
        self._session_id = uuid.uuid4().hex
        self._agent = self._config.default_agent
        self._orchestrators: dict[str, AgentOrchestrator] = {}
        self._pending_permissions: dict[str, asyncio.Future] = {}
        self._shared_conversation: list = []
        self._streaming = False
        self._state = "ready"
        self._agents = list_available_agents(self._config.agents_directory)

        self._stream_buffer = ""
        self._call_counter = 0
        self._call_labels: dict[str, str] = {}
        self._agent_tool_counts: dict[str, dict[str, int]] = {}
        self._input_history: list[str] = []
        self._history_index: int = -1
        self._history_stash: str = ""
        self._verbose_output = False
        self._shell_mode = False

        self._permission_future: asyncio.Future | None = None
        self._tab_completer = TabCompleter()

        exa_api_key = self._config.exa.effective_api_key
        if exa_api_key:
            try:
                from exa_py import Exa
                set_exa_client(Exa(api_key=exa_api_key))
            except ImportError:
                pass

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status")
        yield RichLog(id="output", markup=True, wrap=True, auto_scroll=True, min_width=0)
        yield Static("", id="separator")
        yield Input(placeholder="/help", id="input")
        yield ShortcutBar(id="shortcuts")

    def on_mount(self) -> None:
        self._update_status()
        self._update_shortcuts()
        self.query_one("#input", Input).focus()

    def _update_status(self) -> None:
        background_count = bash_tasks.active_count + web_tasks.active_count + spawned_tasks.active_count
        parts = [
            f"harness  {self._agent}  {self._session_id[:12]}",
        ]
        if background_count:
            parts.append(f"+{background_count}")
        self.query_one("#status", StatusBar).update("  ".join(parts))

    def _update_shortcuts(self) -> None:
        state_color = "green" if self._state == "ready" else "yellow"
        state_label = f"[{state_color}]{self._state}[/{state_color}]"
        agent_label = f"[cyan]{self._agent}[/{self._agent}]" if self._state == "ready" else f"[yellow]{self._agent}[/{self._agent}]"
        shortcuts = f" {state_label} {self._agent}  [dim]esc[/dim] abort  [dim]^O[/dim] verbose  [dim]^C[/dim] quit  [dim]tab[/dim] complete/cycle  [dim]i[/dim] input"
        self.query_one("#shortcuts", ShortcutBar).update(shortcuts)
        prompt_input = self.query_one("#input", Input)
        prompt_input.disabled = self._state == "busy" and self._permission_future is None

    def _log(self) -> RichLog:
        return self.query_one("#output", RichLog)

    def _write(self, markup: str) -> None:
        self._log().write(Text.from_markup(markup))

    @staticmethod
    def _escape_html_tags(text: str) -> str:
        parts = re.split(r"(```.*?```|`.+?`)", text, flags=re.DOTALL)
        for index in range(0, len(parts), 2):
            parts[index] = re.sub(r"<(/?\w)", r"&lt;\1", parts[index])
        return "".join(parts)

    def _write_md(self, text: str) -> None:
        if not text:
            return
        self._log().write(Markdown(self._escape_html_tags(text)))

    def _format_args(self, value: Any, indent: int = 2) -> str:
        prefix = " " * indent
        if not isinstance(value, dict):
            return f"{prefix}{value}"
        lines = []
        for key, inner in value.items():
            if isinstance(inner, str) and "\n" in inner:
                lines.append(f"{prefix}{key}:")
                for sub_line in inner.split("\n"):
                    lines.append(f"{prefix}  {sub_line}")
            else:
                lines.append(f"{prefix}{key}: {inner}")
        return "\n".join(lines)

    def _assign_call_label(self, call_id: str) -> str:
        if call_id not in self._call_labels:
            self._call_counter += 1
            self._call_labels[call_id] = f"#{self._call_counter}"
        return self._call_labels[call_id]

    def _resolve_call_label(self, call_id: str) -> str:
        return self._call_labels.get(call_id, call_id)

    # -- event handling (same logic, writes to RichLog instead of stdout) --

    def _flush_stream_buffer(self) -> None:
        text = self._stream_buffer.strip()
        if text:
            self._write_md(text)
            self._stream_buffer = ""

    def _display_tool_result(self, tool_name: str, result, call_label: str = "") -> None:
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict):
                    result = parsed
            except (json.JSONDecodeError, TypeError):
                pass

        if isinstance(result, dict):
            code = result.get("code", "")
            if code in ("bash_started", "web_search_started"):
                return

            header = f"[magenta]result ({tool_name})[/magenta]"
            if call_label:
                header += f" [dim]{call_label}[/dim]"

            message = result.get("message", "")
            if message:
                self._write(header)
                self._write(f"[red]  {message}[/red]")
                return

            self._write(header)
            if not self._verbose_output:
                return

            if code == "web_search_completed" and "results" in result:
                for entry in result["results"]:
                    title = entry.get("title", "")
                    url = entry.get("url", "")
                    summary = entry.get("summary", "")
                    published = entry.get("published_date", "")
                    self._write(f"  [bold]{title}[/bold]")
                    date_part = f" [dim]{published}[/dim]" if published else ""
                    self._write(f"  [cyan]{url}[/cyan]{date_part}")
                    if summary:
                        self._write_md(summary[:300])
            elif code == "bash_completed":
                output = result.get("output", "")
                if output:
                    for line in output.rstrip("\n").split("\n")[:50]:
                        self._write(f"  [dim]{line}[/dim]")
                    if output.count("\n") > 50:
                        self._write(f"  [dim]... ({output.count(chr(10)) - 50} more lines)[/dim]")
                output_file = result.get("output_file", "")
                if output_file and not output:
                    self._write(f"  [dim]output: {output_file}[/dim]")
            else:
                displayable = {
                    key: value for key, value in result.items()
                    if key not in self._INTERNAL_KEYS and key not in ("output", "message", "code")
                }
                if displayable:
                    self._write(f"[dim]{self._format_args(displayable)}[/dim]")

        elif isinstance(result, str) and result.strip():
            header = f"[magenta]result ({tool_name})[/magenta]"
            if call_label:
                header += f" [dim]{call_label}[/dim]"
            self._write(header)
            if self._verbose_output:
                for line in result.rstrip("\n").split("\n"):
                    self._write(f"  {line}")

    async def _handle_event(self, event: StreamEvent) -> None:
        event_type = event.type

        if event_type == StreamEvent.Type.STATUS:
            code = event.data.get("code", "")
            if code == "thinking" and self._call_counter == 0:
                self._write("[dim]thinking[/dim]")

        elif event_type == StreamEvent.Type.TEXT_CHUNK:
            text = event.data.get("text", "")
            if text:
                self._streaming = True
                self._stream_buffer += text

        elif event_type == StreamEvent.Type.THINKING:
            text = event.data.get("text", "")
            if text:
                self._write(f"[italic yellow]{text}[/italic yellow]")

        elif event_type == StreamEvent.Type.TOOL_CALL:
            self._flush_stream_buffer()
            tool_name = event.data.get("name", "")
            call_id = event.data.get("id", "")
            call_label = self._assign_call_label(call_id) if call_id else ""
            arguments = event.data.get("arguments", {})
            hidden_keys = () if self._verbose_output else ("read_only", "risk", "result_count")
            displayable = {
                key: value for key, value in arguments.items()
                if key not in hidden_keys
            }
            label_suffix = f" [dim]{call_label}[/dim]" if call_label else ""
            self._write(f"[bold blue]tool: {tool_name}[/bold blue]{label_suffix}")
            if tool_name == "orchestrate" and "steps" in arguments:
                steps = arguments["steps"]
                displayable.pop("steps", None)
                for step in steps:
                    step_id = step.get("id", "?")
                    agent_name = step.get("agent", "?")
                    dependencies = step.get("depends_on", None)
                    if dependencies and len(dependencies) > 0:
                        self._write(f"  [dim]{step_id} ({agent_name})[/dim] -> [dim]{', '.join(dependencies)}[/dim]")
                    else:
                        self._write(f"  [dim]{step_id} ({agent_name})[/dim]")
                if displayable:
                    self._write(f"[dim]{self._format_args(displayable)}[/dim]")
            elif displayable:
                self._write(f"[dim]{self._format_args(displayable)}[/dim]")

        elif event_type == StreamEvent.Type.TOOL_RESULT:
            tool_name = event.data.get("name", "")
            call_id = event.data.get("id", "") or event.data.get("task_id", "")
            call_label = self._resolve_call_label(call_id) if call_id else ""
            result = event.data.get("result", "")
            if isinstance(result, dict):
                task_identifier = result.get("task_identifier", "")
                if task_identifier and call_id and call_id in self._call_labels:
                    self._call_labels[task_identifier] = self._call_labels[call_id]
            self._display_tool_result(tool_name, result, call_label)

        elif event_type == StreamEvent.Type.PERMISSION_REQUEST:
            await self._ask_permission(event)

        elif event_type == StreamEvent.Type.ERROR:
            call_id = event.data.get("id", "")
            call_label = self._resolve_call_label(call_id) if call_id else ""
            error_data: dict[str, Any] = {}
            tool_name = event.data.get("tool", "")
            if tool_name:
                error_data["tool"] = tool_name
            error_data["message"] = event.data.get("message", "unknown error")
            label_suffix = f" [dim]{call_label}[/dim]" if call_label else ""
            self._write(f"[bold red]error[/bold red]{label_suffix}")
            self._write(f"[red]{self._format_args(error_data)}[/red]")

        elif event_type == StreamEvent.Type.DONE:
            stop_reason = event.data.get("stop_reason", "")
            if stop_reason == "cancelled":
                self._write("[yellow]cancelled[/yellow]")
                self._shared_conversation.append(
                    SystemMessage(content=json.dumps({"type": "cancelled"}))
                )
            elif stop_reason == "maximum_iterations":
                self._write("[yellow]max iterations reached[/yellow]")
                self._shared_conversation.append(
                    SystemMessage(content=json.dumps({"type": "max_iterations"}))
                )
            elif stop_reason == "completed":
                self._flush_stream_buffer()
                if not self._streaming:
                    text = event.data.get("text", "").strip()
                    if text:
                        self._write_md(text)
            self._stream_buffer = ""
            self._streaming = False

        elif event_type == StreamEvent.Type.TASKS_UPDATED:
            tasks = event.data.get("tasks", [])
            status_colors = {"completed": "green", "in_progress": "yellow", "blocked": "red", "pending": "dim"}
            for task in tasks:
                tid = task.get("identifier", "")
                status = task.get("status", "")
                color = status_colors.get(status, "dim")
                desc = task.get("description", "")
                result = task.get("result", "")
                line = f"[magenta]{tid}[/magenta] [{color}]{status}[/{color}]"
                if result:
                    line += f" [dim]{result[:80]}[/dim]"
                elif desc:
                    line += f" [dim]{desc[:80]}[/dim]"
                self._write(line)

        elif event_type == StreamEvent.Type.BACKGROUND_STARTED:
            call_id = event.data.get("id", "")
            call_label = self._resolve_call_label(call_id) if call_id else ""
            agent_name = event.data.get("agent", "")
            label_suffix = f" {call_label}" if call_label else ""
            self._write(f"[dim]spawned {agent_name}{label_suffix}[/dim]")

        elif event_type == StreamEvent.Type.AGENT_TEXT_CHUNK:
            pass

        elif event_type == StreamEvent.Type.AGENT_TOOL_CALL:
            agent_id = event.data.get("agent_id", "") or event.data.get("step_id", "")
            tool_name = event.data.get("name", "")
            counts = self._agent_tool_counts.setdefault(agent_id, {})
            counts[tool_name] = counts.get(tool_name, 0) + 1

        elif event_type == StreamEvent.Type.AGENT_DONE:
            agent_id = event.data.get("agent_id", "") or event.data.get("step_id", "")
            self._agent_tool_counts.pop(agent_id, None)
            self._write(f"[dim]\\[done] {agent_id}[/dim]")

        self._update_status()

    # -- permission handling --

    async def _ask_permission(self, event: StreamEvent) -> None:
        request_id = event.data.get("request_id", "")
        future = self._pending_permissions.get(request_id)
        if not future or future.done():
            return

        call_id = event.data.get("id", "")
        call_label = self._resolve_call_label(call_id) if call_id else ""

        permission_data: dict[str, Any] = {}
        command = event.data.get("command", "")
        if command:
            permission_data["command"] = command
        risk = event.data.get("risk", "low")
        permission_data["risk"] = risk
        justification = event.data.get("justification", "")
        if justification:
            permission_data["justification"] = justification

        label_suffix = f" [dim]{call_label}[/dim]" if call_label else ""
        self._write(f"[bold yellow]permission required[/bold yellow]{label_suffix}")
        self._write(f"[dim]{self._format_args(permission_data)}[/dim]")

        prompt_input = self.query_one("#input", Input)
        prompt_input.placeholder = "enter to allow, esc to deny"
        prompt_input.disabled = False
        prompt_input.focus()

        self._permission_future = asyncio.get_event_loop().create_future()
        try:
            allowed = await self._permission_future
        except asyncio.CancelledError:
            allowed = False
        finally:
            self._permission_future = None
            prompt_input.placeholder = "type a message or /help"

        if allowed:
            future.set_result(True)
            self._write("[green]approved[/green]")
        else:
            future.set_result(False)
            self._write("[red]denied[/red]")

    # -- input handling --

    def _enter_shell_mode(self) -> None:
        self._shell_mode = True
        input_widget = self.query_one("#input", Input)
        input_widget.styles.background = "#1a1a2e"
        input_widget.placeholder = "shell command"
        self.query_one("#shortcuts", ShortcutBar).update(
            " [bold yellow]shell[/bold yellow]  [dim]esc[/dim] exit  [dim]^C[/dim] exit  [dim]enter[/dim] run"
        )

    def _exit_shell_mode(self) -> None:
        self._shell_mode = False
        input_widget = self.query_one("#input", Input)
        input_widget.styles.background = None
        input_widget.placeholder = "/help"
        self._update_shortcuts()

    def on_key(self, event) -> None:
        input_widget = self.query_one("#input", Input)

        if self._shell_mode and event.key == "backspace" and not input_widget.value:
            event.prevent_default()
            event.stop()
            self._exit_shell_mode()
            return

        if event.key == "up" and input_widget.has_focus:
            event.prevent_default()
            event.stop()
            if not self._input_history:
                return
            if self._history_index == -1:
                self._history_stash = input_widget.value
                self._history_index = len(self._input_history) - 1
            elif self._history_index > 0:
                self._history_index -= 1
            input_widget.value = self._input_history[self._history_index]
            input_widget.cursor_position = len(input_widget.value)
            return

        if event.key == "down" and input_widget.has_focus:
            event.prevent_default()
            event.stop()
            if self._history_index == -1:
                return
            if self._history_index < len(self._input_history) - 1:
                self._history_index += 1
                input_widget.value = self._input_history[self._history_index]
            else:
                self._history_index = -1
                input_widget.value = self._history_stash
            input_widget.cursor_position = len(input_widget.value)
            return

        if event.key != "tab":
            if self._tab_completer.active:
                self._tab_completer.reset()
                self._update_shortcuts()
            return
        if not input_widget.has_focus:
            return
        event.prevent_default()
        event.stop()
        if not input_widget.value:
            if self._agents:
                current_index = self._agents.index(self._agent) if self._agent in self._agents else -1
                self._agent = self._agents[(current_index + 1) % len(self._agents)]
                self._update_status()
                self._update_shortcuts()
            return
        result = self._tab_completer.complete(input_widget.value, self)
        if result:
            input_widget.value = result
            input_widget.cursor_position = len(result)
        self.query_one("#shortcuts", ShortcutBar).update(self._tab_completer.display)

    def on_input_changed(self, event: Input.Changed) -> None:
        if not self._shell_mode and event.value == "!":
            event.input.value = ""
            self._enter_shell_mode()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        self._history_index = -1
        if self._tab_completer.active:
            self._tab_completer.reset()
            self._update_shortcuts()
        if text:
            self._input_history.append(text)

        if self._permission_future and not self._permission_future.done():
            self._permission_future.set_result(True)
            return

        if not text:
            return

        if self._shell_mode:
            self._exit_shell_mode()
            self._run_shell_command(text)
            return

        if text.startswith("/"):
            if not self._command(text):
                self.exit()
            return

        self._write(f"[dim]>[/dim] [on grey23]{text}[/on grey23]")
        self._run_stream(text)

    def action_abort(self) -> None:
        if self._shell_mode:
            self._exit_shell_mode()
            self.query_one("#input", Input).value = ""
            return
        if self._permission_future and not self._permission_future.done():
            self._permission_future.set_result(False)
            return
        orchestrator = self._orchestrators.get(self._agent)
        if orchestrator:
            orchestrator.abort()

    def action_toggle_verbose(self) -> None:
        self._verbose_output = not self._verbose_output
        self._log().clear()
        self._display_conversation_history()

    def action_focus_input(self) -> None:
        input_widget = self.query_one("#input", Input)
        if input_widget.has_focus:
            input_widget.insert_text_at_cursor("i")
        else:
            input_widget.focus()

    def action_clear_or_quit(self) -> None:
        if self._shell_mode:
            self._exit_shell_mode()
            self.query_one("#input", Input).value = ""
            return
        input_widget = self.query_one("#input", Input)
        if input_widget.value:
            input_widget.value = ""
            self._tab_completer.reset()
            self._update_shortcuts()
        else:
            self.exit()

    # -- shell command mode --

    @work(exclusive=False)
    async def _run_shell_command(self, command: str) -> None:
        self._write(f"[bold yellow]![/bold yellow] [on grey15]{command}[/on grey15]")
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await process.communicate()
            output = stdout.decode(errors="replace").rstrip()
        except Exception as exception:
            output = str(exception)

        if output:
            for line in output.split("\n"):
                self._write(f"[dim]{line}[/dim]")

        self._shared_conversation.append(SystemMessage(content=json.dumps({
            "type": "shell_command",
            "command": command,
            "output": output[:4000],
        })))
        self._save_session()

    # -- streaming --

    @work(exclusive=True)
    async def _run_stream(self, message: str) -> None:
        if self._agent not in self._orchestrators:
            self._make_orchestrator()

        self._streaming = False
        self._stream_buffer = ""
        self._state = "busy"
        self._update_shortcuts()
        self._update_status()

        try:
            async for event in self._orchestrators[self._agent].stream(message):
                try:
                    await self._handle_event(event)
                except asyncio.CancelledError:
                    raise
                except Exception as exception:
                    self._write(f"[red]event error: {exception}[/red]")
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            self._write(f"[bold red]stream error: {exception}[/bold red]")
        finally:
            self._state = "ready"
            self._update_shortcuts()
            self._update_status()
            self._save_session()
            self.query_one("#input", Input).focus()

    def _make_orchestrator(self) -> None:
        agent_configuration = load_agent_configuration(self._agent, self._config.agents_directory)
        agent_configuration.stream_agent_progress = True
        self._orchestrators[self._agent] = AgentOrchestrator(
            agent_configuration=agent_configuration,
            global_configuration=self._config,
            pending_permissions=self._pending_permissions,
            session_id=self._session_id,
            conversation=self._shared_conversation,
        )

    # -- slash commands --

    def _command(self, text: str) -> bool:
        parts = text[1:].strip().split(maxsplit=1)
        command_name = parts[0].lower() if parts else ""
        argument = parts[1].strip() if len(parts) > 1 else ""

        if command_name in ("exit", "quit", "q"):
            return False

        if command_name == "help":
            for label, description in COMMANDS.items():
                self._write(f"[dim]{label:<16} {description}[/dim]")

        elif command_name == "agents":
            for name in self._agents:
                if name == self._agent:
                    self._write(f"[cyan]{name} *[/cyan]")
                else:
                    self._write(f"[dim]{name}[/dim]")

        elif command_name == "switch":
            if not argument:
                self._write("[red]usage: /switch <name>[/red]")
            elif argument not in self._agents:
                self._write(f"[red]unknown agent: {argument}[/red]")
            else:
                self._agent = argument
                self._write(f"[green]switched to {argument}[/green]")
                self._update_status()
                self._update_shortcuts()

        elif command_name == "agent":
            state = "active" if self._agent in self._orchestrators else "idle"
            self._write(f"[cyan]{self._agent}[/cyan] [dim]{state}  {self._session_id[:12]}[/dim]")

        elif command_name == "session":
            state = "active" if self._agent in self._orchestrators else "idle"
            self._write(f"[dim]{self._session_id[:12]}  {self._agent}  {state}[/dim]")

        elif command_name == "abort":
            from harness.tools.tools import cancel_all_background_tasks
            orchestrator = self._orchestrators.get(self._agent)
            if orchestrator:
                orchestrator.abort()
            cancel_all_background_tasks()
            self._write("[yellow]aborted all tasks[/yellow]")
            self._update_status()

        elif command_name == "history":
            orchestrator = self._orchestrators.get(self._agent)
            if orchestrator:
                history = orchestrator.get_execution_history()
                for entry in (history or [])[-10:]:
                    self._write(f"[dim]{entry.get('type', '?')}[/dim]")
                if not history:
                    self._write("[dim]empty[/dim]")
            else:
                self._write("[dim]no active session[/dim]")

        elif command_name == "sessions":
            sessions = self._list_sessions()
            if not sessions:
                self._write("[dim]no saved sessions[/dim]")
            else:
                for session in sessions[:10]:
                    sid = session["id"][:12]
                    agent = session["agent"]
                    count = session["messages"]
                    timestamp = session.get("timestamp", "")[:19].replace("T", " ")
                    preview = session["preview"]
                    self._write(f"[cyan]{sid}[/cyan] [dim]{agent}  {count} messages  {timestamp}  {preview}[/dim]")

        elif command_name == "resume":
            target = argument if argument else None
            if not target:
                sessions = self._list_sessions()
                if not sessions:
                    self._write("[dim]no saved sessions[/dim]")
                    return True
                target = sessions[0]["id"]
            if self._load_session(target):
                self._write(f"[green]resumed {target[:12]}[/green]")
                self._display_conversation_history()
                self._update_status()
                self._update_shortcuts()
            else:
                self._write(f"[red]session not found: {target}[/red]")

        elif command_name == "jobs":
            total = bash_tasks.active_count + web_tasks.active_count + spawned_tasks.active_count
            if total == 0:
                self._write("[dim]no background tasks[/dim]")
            else:
                if bash_tasks.active_count:
                    self._write(f"[cyan]bash[/cyan] [dim]{bash_tasks.active_count} running[/dim]")
                    for identifier in bash_tasks.list_active():
                        self._write(f"[dim]  {identifier}[/dim]")
                if web_tasks.active_count:
                    self._write(f"[cyan]web_search[/cyan] [dim]{web_tasks.active_count} running[/dim]")
                    for identifier in web_tasks.list_active():
                        self._write(f"[dim]  {identifier}[/dim]")
                if spawned_tasks.active_count:
                    self._write(f"[cyan]agents[/cyan] [dim]{spawned_tasks.active_count} running[/dim]")
                    for identifier in spawned_tasks.list_active():
                        self._write(f"[dim]  {identifier}[/dim]")

        elif command_name == "clear":
            self._save_session()
            self._session_id = uuid.uuid4().hex
            self._shared_conversation.clear()
            self._orchestrators.clear()
            self._call_counter = 0
            self._call_labels.clear()
            self._log().clear()
            self._update_status()

        else:
            self._write(f"[red]unknown: /{command_name}[/red]")

        return True

    # -- conversation history display --

    def _display_conversation_history(self) -> None:
        if not self._shared_conversation:
            return
        call_counter = 0
        call_id_to_label: dict[str, str] = {}
        call_id_to_tool: dict[str, str] = {}
        for message in self._shared_conversation:
            if isinstance(message, HumanMessage):
                content = message.content if isinstance(message.content, str) else str(message.content)
                self._write(
                    f"[dim]>[/dim] [on grey23]{content}[/on grey23]"
                )
            elif isinstance(message, (AIMessage,)) or type(message).__name__ == "AIMessageChunk":
                content = message.content if isinstance(message.content, str) else str(message.content)
                if content.strip():
                    self._write_md(content)
                tool_calls = getattr(message, "tool_calls", [])
                for tool_call in tool_calls:
                    call_counter += 1
                    name = tool_call.get("name", "")
                    call_id = tool_call.get("id", "")
                    label = f"#{call_counter}"
                    if call_id:
                        call_id_to_label[call_id] = label
                        call_id_to_tool[call_id] = name
                    arguments = tool_call.get("args", {})
                    hidden_keys = () if self._verbose_output else ("read_only", "risk", "result_count")
                    displayable = {
                        key: value for key, value in arguments.items()
                        if key not in hidden_keys
                    }
                    self._write(f"[bold blue]tool: {name}[/bold blue] [dim]{label}[/dim]")
                    if displayable:
                        self._write(f"[dim]{self._format_args(displayable)}[/dim]")
            elif isinstance(message, ToolMessage):
                call_id = getattr(message, "tool_call_id", "")
                label = call_id_to_label.get(call_id, "")
                tool_name = call_id_to_tool.get(call_id, "")
                content = message.content if isinstance(message.content, str) else str(message.content)
                self._display_tool_result(tool_name or "", content, label)
            elif isinstance(message, SystemMessage):
                try:
                    marker = json.loads(message.content)
                    marker_type = marker.get("type", "")
                    if marker_type == "cancelled":
                        self._write("[yellow]cancelled[/yellow]")
                    elif marker_type == "max_iterations":
                        self._write("[yellow]max iterations reached[/yellow]")
                    elif marker_type == "shell_command":
                        command = marker.get("command", "")
                        output = marker.get("output", "")
                        self._write(f"[bold yellow]![/bold yellow] [on grey15]{command}[/on grey15]")
                        if output:
                            for line in output.split("\n"):
                                self._write(f"[dim]{line}[/dim]")
                except (json.JSONDecodeError, TypeError):
                    pass

    # -- session persistence --

    _MESSAGE_TYPE_NAMES = {
        "HumanMessage": "human",
        "AIMessage": "ai",
        "AIMessageChunk": "ai",
        "SystemMessage": "system",
        "ToolMessage": "tool",
    }

    _MESSAGE_TYPE_CLASSES = {
        "human": HumanMessage,
        "ai": AIMessage,
        "system": SystemMessage,
        "tool": ToolMessage,
    }

    _LOADABLE_FIELDS = {"content", "tool_call_id", "tool_calls", "additional_kwargs"}

    @staticmethod
    def _serialize_message(message) -> dict | None:
        class_name = type(message).__name__
        type_name = HarnessApp._MESSAGE_TYPE_NAMES.get(class_name)
        if not type_name:
            return None
        content = message.content if isinstance(message.content, str) else str(message.content)
        entry = {"type": type_name, "content": content}
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            entry["tool_calls"] = tool_calls
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id
        additional = getattr(message, "additional_kwargs", None)
        if additional:
            entry["additional_kwargs"] = additional
        return entry

    def _save_session(self) -> None:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        session_file = SESSIONS_DIR / f"{self._session_id}.json"
        messages = []
        for message in self._shared_conversation:
            entry = self._serialize_message(message)
            if entry:
                messages.append(entry)
        session_data = {
            "session_id": self._session_id,
            "agent": self._agent,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "messages": messages,
        }
        session_file.write_text(json.dumps(session_data, default=str))

    def _resolve_session_file(self, session_id: str) -> Path | None:
        exact = SESSIONS_DIR / f"{session_id}.json"
        if exact.exists():
            return exact
        if not SESSIONS_DIR.exists():
            return None
        matches = [f for f in SESSIONS_DIR.glob("*.json") if f.stem.startswith(session_id)]
        if len(matches) == 1:
            return matches[0]
        return None

    def _load_session(self, session_id: str) -> bool:
        session_file = self._resolve_session_file(session_id)
        if not session_file:
            return False
        data = json.loads(session_file.read_text())
        type_map = self._MESSAGE_TYPE_CLASSES
        self._shared_conversation.clear()
        for entry in data.get("messages", []):
            cls = type_map.get(entry.get("type", ""))
            if not cls:
                continue
            fields = {key: value for key, value in entry.items() if key in self._LOADABLE_FIELDS}
            try:
                self._shared_conversation.append(cls(**fields))
            except Exception:
                pass
        self._session_id = data.get("session_id", session_file.stem)
        self._agent = data.get("agent", self._agent)
        self._orchestrators.clear()
        return True

    def _list_sessions(self) -> list[dict]:
        if not SESSIONS_DIR.exists():
            return []
        sessions = []
        for session_file in SESSIONS_DIR.glob("*.json"):
            try:
                data = json.loads(session_file.read_text())
                human_messages = [m for m in data.get("messages", []) if m.get("type") == "human"]
                preview = human_messages[0]["content"][:60] if human_messages else ""
                sessions.append({
                    "id": data.get("session_id", session_file.stem),
                    "timestamp": data.get("timestamp", ""),
                    "agent": data.get("agent", ""),
                    "messages": len(data.get("messages", [])),
                    "preview": preview,
                })
            except Exception:
                pass
        sessions.sort(key=lambda s: s["timestamp"], reverse=True)
        return sessions


def main():
    app = HarnessApp()
    app.run()


if __name__ == "__main__":
    main()
