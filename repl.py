#!/usr/bin/env python3

import asyncio
import io
import json
import os
import re
import select
import sys
import termios
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import FormattedText, ANSI
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from rich.console import Console
from rich.markdown import Markdown
from rich.style import Style as RichStyle
from rich.syntax import PygmentsSyntaxTheme

from harness.core.agent import AgentOrchestrator, StreamEvent
from harness.core.configuration import (
    GlobalConfiguration,
    load_agent_configuration,
    list_available_agents,
)
from harness.tools.tools import set_exa_client, bash_tasks, web_tasks, spawned_tasks


_original_pygments_init = PygmentsSyntaxTheme.__init__

def _pygments_theme_no_bg(self, theme):
    _original_pygments_init(self, theme)
    self._background_style = RichStyle(bgcolor=None)

PygmentsSyntaxTheme.__init__ = _pygments_theme_no_bg


PROMPT_STYLE = Style.from_dict({
    "agent": "ansicyan",
    "separator": "ansibrightblack",
    "state-ready": "ansigreen",
    "state-busy": "ansiyellow",
})

HISTORY_PATH = Path.home() / ".harness_history"
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
    "/clear": "clear terminal",
    "/exit": "exit",
}


class SlashCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return
        for command, description in COMMANDS.items():
            if command.startswith(text):
                yield Completion(command, start_position=-len(text), display_meta=description)


class HarnessREPL:
    """Interactive terminal REPL for the harness agent orchestrator."""

    _INTERNAL_KEYS = frozenset({
        "code", "pid", "size", "task_identifier", "output_file", "query",
    })

    def __init__(self):
        self._config = GlobalConfiguration.from_yaml("configuration.yaml")
        self._session_id = uuid.uuid4().hex
        self._agent = self._config.default_agent
        self._orchestrators: dict[str, AgentOrchestrator] = {}
        self._pending_permissions: dict[str, asyncio.Future] = {}
        self._shared_conversation: list = []
        self._streaming = False
        self._state = "ready"
        self._agents = list_available_agents(self._config.agents_directory)

        self._rich_buffer = io.StringIO()
        self._console = Console(file=self._rich_buffer, force_terminal=True)
        self._stream_buffer = ""
        self._call_counter = 0
        self._call_labels: dict[str, str] = {}
        self._agent_tool_counts: dict[str, dict[str, int]] = {}
        self._needs_newline = False
        self._verbose_output = False

        exa_api_key = self._config.exa.effective_api_key
        if exa_api_key:
            try:
                from exa_py import Exa
                set_exa_client(Exa(api_key=exa_api_key))
            except ImportError:
                pass

        bindings = KeyBindings()

        @bindings.add("c-c")
        def _on_ctrl_c(event):
            orchestrator = self._orchestrators.get(self._agent)
            if orchestrator:
                orchestrator.abort()
            event.app.exit(result="")

        @bindings.add("c-o")
        def _on_ctrl_o(event):
            self._verbose_output = not self._verbose_output
            mode = "verbose" if self._verbose_output else "compact"
            self._rich(f"[dim]output: {mode}[/dim]")

        @bindings.add("tab")
        def _on_tab(event):
            buffer = event.app.current_buffer
            if buffer.text.startswith("/"):
                buffer.start_completion()
            elif not buffer.text:
                if not self._agents:
                    return
                current_index = (
                    self._agents.index(self._agent)
                    if self._agent in self._agents
                    else -1
                )
                self._agent = self._agents[(current_index + 1) % len(self._agents)]
                event.app.invalidate()

        self._prompt = PromptSession(
            history=FileHistory(str(HISTORY_PATH)),
            style=PROMPT_STYLE,
            key_bindings=bindings,
            completer=SlashCompleter(),
            complete_while_typing=Condition(
                lambda: self._prompt.default_buffer.text.startswith("/")
            ),
            enable_history_search=True,
        )
        self._confirm_prompt = PromptSession(style=PROMPT_STYLE)

    def _rich(self, *args, **kwargs):
        """Print rich-formatted output to the terminal."""
        if self._needs_newline:
            print_formatted_text(ANSI(""))
            self._needs_newline = False
        self._rich_buffer.seek(0)
        self._rich_buffer.truncate(0)
        self._console.print(*args, **kwargs)
        rendered = self._rich_buffer.getvalue().rstrip("\n")
        if rendered:
            print_formatted_text(ANSI(rendered))

    @staticmethod
    def _escape_html_tags(text: str) -> str:
        """Escape HTML-like tags outside code spans/blocks so they render literally."""
        parts = re.split(r"(```.*?```|`.+?`)", text, flags=re.DOTALL)
        for index in range(0, len(parts), 2):
            parts[index] = re.sub(r"<(/?\w)", r"&lt;\1", parts[index])
        return "".join(parts)

    def _render_md(self, text: str) -> str:
        """Render markdown to an ANSI string with collapsed blank lines."""
        self._rich_buffer.seek(0)
        self._rich_buffer.truncate(0)
        self._console.print(Markdown(self._escape_html_tags(text)))
        return re.sub(r"\n\s*\n", "\n", self._rich_buffer.getvalue()).rstrip("\n\n")

    def _md(self, text: str):
        """Render a markdown string to the terminal."""
        if not text:
            return
        rendered = self._render_md(text)
        if rendered:
            print_formatted_text(ANSI(rendered))

    def _assign_call_label(self, call_id: str) -> str:
        """Assign a short sequential label (#1, #2, ...) for a tool call ID."""
        if call_id not in self._call_labels:
            self._call_counter += 1
            self._call_labels[call_id] = f"#{self._call_counter}"
        return self._call_labels[call_id]

    def _resolve_call_label(self, call_id: str) -> str:
        """Look up the short label for a tool call ID, or return the raw ID."""
        return self._call_labels.get(call_id, call_id)

    def _format_yaml(self, value: Any, indent: int = 2) -> str:
        """Render a value as indented YAML for terminal display."""
        raw = yaml.dump(
            value,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        ).rstrip("\n")
        prefix = " " * indent
        return "\n".join(f"{prefix}{line}" for line in raw.split("\n"))

    @asynccontextmanager
    async def _streaming_input(self):
        """Context manager: disables echo/canonical mode while keeping output processing intact."""
        stdin_descriptor = sys.stdin.fileno()
        saved_termios = termios.tcgetattr(stdin_descriptor)
        reader_task = None
        try:
            streaming_attrs = list(saved_termios)
            streaming_attrs[3] = streaming_attrs[3] & ~(termios.ECHO | termios.ICANON)
            termios.tcsetattr(stdin_descriptor, termios.TCSANOW, streaming_attrs)
            reader_task = asyncio.create_task(self._read_abort_keys(stdin_descriptor))
            yield
        finally:
            if reader_task and not reader_task.done():
                reader_task.cancel()
                try:
                    await reader_task
                except asyncio.CancelledError:
                    pass
            termios.tcsetattr(stdin_descriptor, termios.TCSANOW, saved_termios)
            termios.tcflush(stdin_descriptor, termios.TCIFLUSH)

    def _stdin_has_data(self, stdin_descriptor: int) -> bool:
        readable, _, _ = select.select([stdin_descriptor], [], [], 0)
        return bool(readable)

    def _drain_escape_sequence(self, stdin_descriptor: int):
        """Consume a full CSI escape sequence after reading \\x1b[."""
        while self._stdin_has_data(stdin_descriptor):
            sequence_byte = os.read(stdin_descriptor, 1)
            if not sequence_byte or (0x40 <= sequence_byte[0] <= 0x7E):
                return

    async def _read_abort_keys(self, stdin_descriptor: int):
        """Poll stdin for Escape or Ctrl+C during streaming and trigger abort."""
        while True:
            await asyncio.sleep(0.05)
            if not self._stdin_has_data(stdin_descriptor):
                continue
            try:
                byte = os.read(stdin_descriptor, 1)
            except OSError:
                return
            if not byte:
                continue
            if byte == b"\x0f":
                self._verbose_output = not self._verbose_output
                mode = "verbose" if self._verbose_output else "compact"
                self._rich(f"[dim]output: {mode}[/dim]")
                continue
            if byte == b"\x03":
                orchestrator = self._orchestrators.get(self._agent)
                if orchestrator:
                    orchestrator.abort()
                return
            if byte == b"\x1b":
                await asyncio.sleep(0.05)
                if self._stdin_has_data(stdin_descriptor):
                    next_byte = os.read(stdin_descriptor, 1)
                    if next_byte == b"[":
                        await asyncio.sleep(0.02)
                        self._drain_escape_sequence(stdin_descriptor)
                        continue
                orchestrator = self._orchestrators.get(self._agent)
                if orchestrator:
                    orchestrator.abort()
                return

    def _display_tool_result(self, tool_name: str, result, call_label: str = ""):
        """Format and print a tool result. Compact mode shows header only, verbose shows full body."""
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                pass

        if isinstance(result, dict):
            code = result.get("code", "")

            if code in ("bash_started", "web_search_started", "bash_completed", "web_search_completed"):
                return

            header = f"[magenta]result ({tool_name})[/magenta]"
            if call_label:
                header += f" [dim]{call_label}[/dim]"

            message = result.get("message", "")
            if message:
                self._rich(header)
                self._rich(f"[red]  {message}[/red]")
                return

            if not self._verbose_output:
                self._rich(header)
                return

            self._rich(header)
            displayable = {
                key: value for key, value in result.items()
                if key not in self._INTERNAL_KEYS and key not in ("output", "message", "code")
            }
            if displayable:
                self._rich(f"[dim]{self._format_yaml(displayable)}[/dim]")

        elif isinstance(result, str) and result.strip():
            header = f"[magenta]result ({tool_name})[/magenta]"
            if call_label:
                header += f" [dim]{call_label}[/dim]"
            self._rich(header)
            if self._verbose_output:
                for line in result.rstrip("\n").split("\n"):
                    self._rich(f"  {line}")

    def _make_orchestrator(self):
        agent_configuration = load_agent_configuration(self._agent, self._config.agents_directory)
        agent_configuration.stream_agent_progress = True
        self._orchestrators[self._agent] = AgentOrchestrator(
            agent_configuration=agent_configuration,
            global_configuration=self._config,
            pending_permissions=self._pending_permissions,
            session_id=self._session_id,
            conversation=self._shared_conversation,
        )

    def _command(self, text: str) -> bool:
        """Handle a slash command. Returns False to exit the REPL."""
        parts = text[1:].strip().split(maxsplit=1)
        command_name = parts[0].lower() if parts else ""
        argument = parts[1].strip() if len(parts) > 1 else ""

        if command_name in ("exit", "quit", "q"):
            return False

        if command_name == "help":
            for label, description in COMMANDS.items():
                self._rich(f"[dim]{label:<16} {description}[/dim]")

        elif command_name == "agents":
            for name in self._agents:
                if name == self._agent:
                    self._rich(f"[cyan]{name} *[/cyan]")
                else:
                    self._rich(f"[dim]{name}[/dim]")

        elif command_name == "switch":
            if not argument:
                self._rich("[red]usage: /switch <name>[/red]")
            elif argument not in self._agents:
                self._rich(f"[red]unknown agent: {argument}[/red]")
            else:
                self._agent = argument
                self._rich(f"[green]switched to {argument}[/green]")

        elif command_name == "agent":
            state = "active" if self._agent in self._orchestrators else "idle"
            self._rich(
                f"[cyan]{self._agent}[/cyan] [dim]{state}  {self._session_id[:12]}[/dim]"
            )

        elif command_name == "session":
            state = "active" if self._agent in self._orchestrators else "idle"
            self._rich(f"[dim]{self._session_id}  {self._agent}  {state}[/dim]")

        elif command_name == "abort":
            from harness.tools.tools import cancel_all_background_tasks
            orchestrator = self._orchestrators.get(self._agent)
            if orchestrator:
                orchestrator.abort()
            cancel_all_background_tasks()
            self._rich("[yellow]aborted all tasks[/yellow]")

        elif command_name == "history":
            orchestrator = self._orchestrators.get(self._agent)
            if orchestrator:
                history = orchestrator.get_execution_history()
                for entry in (history or [])[-10:]:
                    self._rich(f"[dim]{entry.get('type', '?')}[/dim]")
                if not history:
                    self._rich("[dim]empty[/dim]")
            else:
                self._rich("[dim]no active session[/dim]")

        elif command_name == "sessions":
            sessions = self._list_sessions()
            if not sessions:
                self._rich("[dim]no saved sessions[/dim]")
            else:
                for session in sessions[:10]:
                    sid = session["id"][:12]
                    agent = session["agent"]
                    count = session["messages"]
                    preview = session["preview"]
                    self._rich(f"[cyan]{sid}[/cyan] [dim]{agent}  {count} msgs  {preview}[/dim]")

        elif command_name == "resume":
            target = argument if argument else None
            if not target:
                sessions = self._list_sessions()
                if not sessions:
                    self._rich("[dim]no saved sessions[/dim]")
                    return True
                target = sessions[0]["id"]
            if self._load_session(target):
                self._rich(f"[green]resumed {target[:12]}[/green]")
            else:
                self._rich(f"[red]session not found: {target}[/red]")

        elif command_name == "jobs":
            total = bash_tasks.active_count + web_tasks.active_count + spawned_tasks.active_count
            if total == 0:
                self._rich("[dim]no background tasks[/dim]")
            else:
                if bash_tasks.active_count:
                    self._rich(f"[cyan]bash[/cyan] [dim]{bash_tasks.active_count} running[/dim]")
                    for identifier in bash_tasks.list_active():
                        self._rich(f"[dim]  {identifier}[/dim]")
                if web_tasks.active_count:
                    self._rich(f"[cyan]web_search[/cyan] [dim]{web_tasks.active_count} running[/dim]")
                    for identifier in web_tasks.list_active():
                        self._rich(f"[dim]  {identifier}[/dim]")
                if spawned_tasks.active_count:
                    self._rich(f"[cyan]agents[/cyan] [dim]{spawned_tasks.active_count} running[/dim]")
                    for identifier in spawned_tasks.list_active():
                        self._rich(f"[dim]  {identifier}[/dim]")

        elif command_name == "clear":
            os.system("cls" if os.name == "nt" else "clear")

        else:
            self._rich(f"[red]unknown: /{command_name}[/red]")

        return True

    async def _ask_permission(self, event: StreamEvent):
        """Prompt the user to allow or deny a permission request."""
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
        self._rich(f"[bold yellow]permission required[/bold yellow]{label_suffix}")
        self._rich(f"[dim]{self._format_yaml(permission_data)}[/dim]")

        try:
            await self._confirm_prompt.prompt_async(
                FormattedText([("class:separator", "enter to allow, ^c to deny ")])
            )
            future.set_result(True)
            self._rich("[green]approved[/green]")
        except (KeyboardInterrupt, EOFError):
            future.set_result(False)
            self._rich("[red]denied[/red]")

    def _flush_stream_buffer(self):
        """Render and clear any buffered text as markdown."""
        text = self._stream_buffer.strip()
        if text:
            self._md(text)
            self._stream_buffer = ""

    async def _handle_event(self, event: StreamEvent):
        """Dispatch a single stream event to the appropriate display handler."""
        event_type = event.type

        if event_type == StreamEvent.Type.STATUS:
            code = event.data.get("code", "")
            if code == "thinking" and self._call_counter == 0:
                self._rich("[dim]thinking[/dim]")

        elif event_type == StreamEvent.Type.TEXT_CHUNK:
            text = event.data.get("text", "")
            if text:
                self._streaming = True
                self._stream_buffer += text

        elif event_type == StreamEvent.Type.THINKING:
            text = event.data.get("text", "")
            if text:
                self._rich(f"[italic yellow]{text}[/italic yellow]")

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
            self._rich(f"[bold blue]tool: {tool_name}[/bold blue]{label_suffix}")
            if tool_name == "orchestrate" and "steps" in arguments:
                steps = arguments["steps"]
                displayable.pop("steps", None)
                for step in steps:
                    step_id = step.get("id", "?")
                    agent_name = step.get("agent", "?")
                    dependencies = step.get("depends_on", None)
                    if dependencies is not None and len(dependencies) > 0:
                        self._rich(f"  [dim]{step_id} ({agent_name})[/dim] -> [dim]{', '.join(dependencies)}[/dim]")
                    else:
                        self._rich(f"  [dim]{step_id} ({agent_name})[/dim]")
                if displayable:
                    self._rich(f"[dim]{self._format_yaml(displayable)}[/dim]")
            elif displayable:
                self._rich(f"[dim]{self._format_yaml(displayable)}[/dim]")

        elif event_type == StreamEvent.Type.TOOL_RESULT:
            tool_name = event.data.get("name", "")
            call_id = event.data.get("id", "") or event.data.get("task_id", "")
            call_label = self._resolve_call_label(call_id) if call_id else ""
            result = event.data.get("result", "")
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
            self._rich(f"[bold red]error[/bold red]{label_suffix}")
            self._rich(f"[red]{self._format_yaml(error_data)}[/red]")

        elif event_type == StreamEvent.Type.DONE:
            stop_reason = event.data.get("stop_reason", "")
            if stop_reason == "cancelled":
                self._rich("[yellow]cancelled[/yellow]")
            elif stop_reason == "maximum_iterations":
                self._rich("[yellow]max iterations reached[/yellow]")
            elif stop_reason == "completed":
                self._flush_stream_buffer()
                if not self._streaming:
                    text = event.data.get("text", "").strip()
                    if text:
                        self._md(text)
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
                self._rich(line)

        elif event_type == StreamEvent.Type.BACKGROUND_STARTED:
            call_id = event.data.get("id", "")
            call_label = self._resolve_call_label(call_id) if call_id else ""
            agent_name = event.data.get("agent", "")
            label_suffix = f" {call_label}" if call_label else ""
            self._rich(f"[dim]spawned {agent_name}{label_suffix}[/dim]")

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
            self._rich(f"[dim]\\[done] {agent_id}[/dim]")

    def _background_count(self) -> int:
        return bash_tasks.active_count + web_tasks.active_count + spawned_tasks.active_count

    def _save_session(self):
        """Persist current session conversation to disk."""
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        session_file = SESSIONS_DIR / f"{self._session_id}.json"
        messages = []
        for message in self._shared_conversation:
            try:
                messages.append({"type": type(message).__name__, **message.model_dump()})
            except Exception:
                pass
        session_data = {
            "session_id": self._session_id,
            "agent": self._agent,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "messages": messages,
        }
        session_file.write_text(json.dumps(session_data, default=str))

    def _load_session(self, session_id: str) -> bool:
        """Load a saved session's conversation from disk."""
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
        session_file = SESSIONS_DIR / f"{session_id}.json"
        if not session_file.exists():
            return False
        data = json.loads(session_file.read_text())
        type_map = {"HumanMessage": HumanMessage, "AIMessage": AIMessage, "SystemMessage": SystemMessage, "ToolMessage": ToolMessage}
        self._shared_conversation.clear()
        for entry in data.get("messages", []):
            message_type = entry.pop("type", "")
            cls = type_map.get(message_type)
            if cls:
                try:
                    self._shared_conversation.append(cls(**{k: v for k, v in entry.items() if k in ("content", "tool_call_id", "tool_calls", "additional_kwargs")}))
                except Exception:
                    pass
        self._session_id = session_id
        self._agent = data.get("agent", self._agent)
        self._orchestrators.clear()
        return True

    def _list_sessions(self) -> list[dict]:
        """List saved sessions sorted by most recent first."""
        if not SESSIONS_DIR.exists():
            return []
        sessions = []
        for session_file in SESSIONS_DIR.glob("*.json"):
            try:
                data = json.loads(session_file.read_text())
                human_messages = [m for m in data.get("messages", []) if m.get("type") == "HumanMessage"]
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

    async def _stream_response(self, message: str):
        """Send a message to the orchestrator and display all streamed events."""
        if self._agent not in self._orchestrators:
            self._make_orchestrator()

        self._streaming = False
        self._stream_buffer = ""
        self._state = "busy"
        self._rich("[dim]esc to interrupt[/dim]")

        async with self._streaming_input():
            try:
                async for event in self._orchestrators[self._agent].stream(message):
                    try:
                        await self._handle_event(event)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exception:
                        self._rich(f"[red]event error: {exception}[/red]")
            except asyncio.CancelledError:
                raise
            except Exception as exception:
                self._rich(f"[bold red]stream error: {exception}[/bold red]")
            finally:
                self._state = "ready"
                self._save_session()

    async def run(self):
        self._rich(f"[dim]harness  {self._agent}  {self._session_id[:12]}[/dim]")
        self._rich("[dim]type /help for commands, tab to cycle agents[/dim]")

        while True:
            try:
                bg_count = self._background_count()
                text = await self._prompt.prompt_async(
                    lambda: FormattedText([
                        (f"class:state-{self._state}", f"[{self._state}] "),
                        ("class:agent", self._agent),
                        *([("class:state-busy", f" +{bg_count}")] if bg_count else []),
                        ("class:separator", " > "),
                    ])
                )
            except KeyboardInterrupt:
                continue
            except EOFError:
                break

            text = text.strip()
            if not text:
                continue

            if text.startswith("/"):
                if not self._command(text):
                    break
                continue

            try:
                await self._stream_response(text)
            except KeyboardInterrupt:
                orchestrator = self._orchestrators.get(self._agent)
                if orchestrator:
                    orchestrator.abort()
                self._rich("[yellow]interrupted[/yellow]")

        self._save_session()


async def main():
    repl = HarnessREPL()
    await repl.run()


if __name__ == "__main__":
    asyncio.run(main())
