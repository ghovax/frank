import asyncio
import json
from pathlib import Path

import httpx
from httpx import ASGITransport
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from agentic_harness.server.app import app as _server_app
from agentic_harness.server.app import startup as _server_startup


HISTORY_FILE = Path.home() / ".agentic_harness_history"
_TEMPLATES_DIR = Path(__file__).parent / "repl" / "templates"


def _render_template(name: str, **variables: str) -> str:
    path = _TEMPLATES_DIR / f"{name}.tpl"
    if not path.exists():
        return ""
    content = path.read_text()
    for key, value in variables.items():
        content = content.replace(f"{{{{ {key} }}}}", value)
    return content


class _AgentCompleter(Completer):
    def __init__(self, agents: list[str], commands: list[str]):
        self._agents = agents
        self._commands = commands

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/agent "):
            prefix = text[7:]
            for agent in self._agents:
                if agent.startswith(prefix):
                    yield Completion(agent, start_position=-len(prefix))
        elif text.startswith("/"):
            for command in self._commands:
                if command.startswith(text):
                    yield Completion(command, start_position=-len(text))


class _StreamRenderer:
    def __init__(self):
        self.text = ""
        self._rows: list[Text] = []

    def add_text(self, chunk: str) -> None:
        self.text += chunk
        self._rebuild()

    def add_thinking(self, text: str) -> None:
        self._rows.append(Text(text, style="dim italic"))
        self._rebuild()

    def add_tool_call(self, name: str, summary: str) -> None:
        self._rows.append(Text(f"\n  {name}  {summary}", style="bold cyan"))
        self._rebuild()

    def add_subagent_tool_call(self, agent: str, name: str, summary: str) -> None:
        self._rows.append(Text(f"\n  [{agent}] {name}  {summary}", style="cyan"))
        self._rebuild()

    def add_tool_result(self, summary: str) -> None:
        self._rows.append(Text(f"  {summary}", style="dim"))
        self._rebuild()

    def add_error(self, message: str) -> None:
        self._rows.append(Text(f"\n  {message}", style="bold red"))
        self._rebuild()

    def add_subagent_text(self, agent: str, chunk: str) -> None:
        self._rows.append(Text(f"[{agent}] {chunk}", style="dim"))
        self._rebuild()

    def add_subagent_done(self, agent: str) -> None:
        self._rows.append(Text(f"[{agent}] done", style="dim green"))
        self._rebuild()

    def _rebuild(self) -> None:
        content = []
        if self.text:
            content.append(Text(self.text.rstrip()))
        content.extend(self._rows)
        self._renderable = Panel(Group(*content) if content else Text(""))

    def append_status(self, text: str) -> None:
        self._rows.append(Text(text, style="dim"))
        self._rebuild()

    def __rich__(self):
        return self._renderable


class Repl:
    def __init__(self, host: str, port: int):
        self._host = host
        self._port = port
        self._base_url = f"http://{host}:{port}"
        self._session_id: str | None = None
        self._agent = "main"
        self._agents: list[str] = []
        self._console = Console()
        self._session_prompt = PromptSession(
            history=FileHistory(str(HISTORY_FILE)),
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=ASGITransport(app=_server_app),
            base_url=self._base_url,
        )

    async def _refresh_agents(self) -> None:
        async with self._client() as client:
            response = await client.get("/agents", timeout=5)
            self._agents = response.json().get("agents", [])

    def _make_completer(self) -> _AgentCompleter:
        return _AgentCompleter(self._agents, ["/agent", "/help", "/exit", "/sessions"])

    async def run(self) -> None:
        await _server_startup()
        await self._refresh_agents()
        self._console.print(_render_template("startup", server_url=self._base_url))

        while True:
            try:
                prompt_text = await self._session_prompt.prompt_async(
                    f"\x1b[1m{self._agent}\x1b[0m> ",
                    completer=self._make_completer(),
                )
                prompt_text = prompt_text.strip()
                if not prompt_text:
                    continue
                if prompt_text.startswith("/"):
                    await self._handle_command(prompt_text)
                else:
                    await self._send_message(prompt_text)
            except KeyboardInterrupt:
                continue
            except EOFError:
                break

    async def _handle_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if command == "/exit":
            raise EOFError()
        elif command == "/help":
            self._console.print(_render_template("help"))
        elif command == "/sessions":
            if self._session_id:
                self._console.print(f"Session: {self._session_id}")
            else:
                self._console.print("No active session")
        elif command == "/agent":
            if not arg:
                self._console.print(f"Current agent: {self._agent}")
                return
            await self._refresh_agents()
            if arg in self._agents:
                self._agent = arg
                self._session_id = None
                self._console.print(f"Switched to agent: {arg}")
            else:
                self._console.print(f"Unknown agent: {arg}")
                table = Table(show_header=False, box=None, padding=(0, 1))
                table.add_column(style="dim")
                for agent in self._agents:
                    table.add_row(agent)
                self._console.print(table)
        else:
            self._console.print(f"Unknown command: {command}")

    async def _send_message(self, text: str) -> None:
        payload: dict = {"message": text, "agent": self._agent}
        if self._session_id:
            payload["session_id"] = self._session_id

        self._console.print(Rule(style="dim"))
        self._console.print(Text(f"  {text}", style="bold"))

        renderer = _StreamRenderer()
        permission_prompt_active = False

        try:
            with Live(
                renderer, console=self._console, refresh_per_second=10,
                vertical_overflow="visible",
            ) as live:
                async with self._client() as client:
                    async with client.stream(
                        "POST",
                        "/chat",
                        json=payload,
                        timeout=None,
                    ) as response:
                        async for event_type, data in _iter_sse(response):
                            await self._handle_event(
                                event_type, data, client, renderer, live,
                            )
            self._console.print()
        except KeyboardInterrupt:
            self._console.print("\n[yellow]Aborting...[/]")
            if self._session_id:
                try:
                    async with self._client() as abort_client:
                        await abort_client.post(
                            f"/chat/{self._session_id}/abort",
                            timeout=5,
                        )
                except Exception:
                    pass
            self._console.print("[yellow]Cancelled[/]")

    async def _handle_event(
        self, event_type: str, data: dict, client: httpx.AsyncClient,
        renderer: _StreamRenderer, live: Live,
    ) -> None:
        if event_type == "session":
            self._session_id = data.get("session_id", self._session_id)

        elif event_type == "text_chunk":
            renderer.add_text(data.get("text", ""))
            live.update(renderer)

        elif event_type == "thinking":
            renderer.add_thinking(data.get("text", ""))
            live.update(renderer)

        elif event_type == "tool_call":
            name = data.get("name", "")
            args = data.get("arguments", {})
            renderer.add_tool_call(name, _summarize_tool_call(name, args))
            live.update(renderer)

        elif event_type == "tool_result":
            renderer.add_tool_result(_summarize_result(data.get("result")))
            live.update(renderer)

        elif event_type == "error":
            renderer.add_error(data.get("message", ""))
            live.update(renderer)

        elif event_type == "agent_text_chunk":
            chunk = data.get("text", "")
            if chunk:
                agent = data.get("agent_id", data.get("step_id", ""))
                renderer.add_subagent_text(agent, chunk)
                live.update(renderer)

        elif event_type == "agent_tool_call":
            name = data.get("name", "")
            args = data.get("arguments", {})
            agent = data.get("agent_id", data.get("step_id", ""))
            renderer.add_subagent_tool_call(agent, name, _summarize_tool_call(name, args))
            live.update(renderer)

        elif event_type == "agent_thinking":
            renderer.add_thinking(data.get("text", ""))
            live.update(renderer)

        elif event_type == "agent_done":
            agent = data.get("agent_id", "")
            renderer.add_subagent_done(agent)
            live.update(renderer)

        elif event_type == "background_started":
            task = data.get("task_id", "")
            renderer.append_status(f"Spawned {task}")
            live.update(renderer)

        elif event_type == "status":
            code = data.get("code", "")
            if code == "waiting_background":
                renderer.append_status("Waiting for background tasks...")
                live.update(renderer)

        elif event_type == "done":
            reason = data.get("stop_reason", "")
            if reason == "cancelled":
                renderer.add_error("Cancelled")
            elif reason == "maximum_iterations":
                renderer.add_error("Reached maximum iterations")
            live.update(renderer)

        elif event_type == "permission_request":
            live.stop()
            self._console.print()
            self._console.print(Panel(
                Text(f"{data.get('command', '')}", style="bold"),
                title="Permission required",
                subtitle=f"risk: {data.get('risk', 'low')}",
                border_style="yellow",
            ))
            just = data.get("justification", "")
            if just:
                self._console.print(f"  Reason: {just}")
            try:
                decision = await PromptSession().prompt_async("Allow? [Y/n] ")
            except (KeyboardInterrupt, EOFError):
                decision = "n"
            allowed = decision.lower() not in ("n", "no", "0")
            request_id = data.get("request_id", "")
            try:
                await client.post(
                    f"/chat/{self._session_id}/permission",
                    json={"request_id": request_id, "decision": "allow" if allowed else "deny"},
                    timeout=5,
                )
            except Exception as exc:
                self._console.print(f"Error: {exc}")
            live.start()
            live.update(renderer)


async def _iter_sse(response):
    event_type = None
    async for line in response.aiter_lines():
        if not line:
            continue
        if line.startswith("event: "):
            event_type = line[7:].strip()
        elif line.startswith("data: ") and event_type:
            yield event_type, json.loads(line[6:])
            event_type = None


def _summarize_tool_call(name: str, args: dict) -> str:
    if name == "bash":
        return args.get("command", "")[:80]
    elif name == "spawn_agent":
        agent = args.get("agent", "main")
        prompt = args.get("prompt", "")[:60]
        return f"agent={agent} prompt=\"{prompt}\""
    elif name == "orchestrate":
        steps = args.get("steps", [])
        return f"{len(steps)} steps"
    elif name == "write_tasks":
        tasks = args.get("tasks", [])
        return f"{len(tasks)} tasks"
    elif name == "update_task":
        task = args.get("task_id", "")
        status = args.get("status", "")
        return f"{task} -> {status}"
    return json.dumps(args)[:80]


def _summarize_result(result) -> str:
    if isinstance(result, dict):
        code = result.get("code", "")
        if code == "bash_completed":
            size = result.get("size", 0)
            return f"bash completed ({size} bytes)" if size else "bash completed"
        elif code == "background_started":
            return f"background: {result.get('task_identifier', '')}"
        elif code == "orchestration_completed":
            results = result.get("results", [])
            return f"orchestration completed: {len(results)} results"
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            return f"result: {json.dumps(parsed)[:80]}"
        except (json.JSONDecodeError, TypeError):
            pass
    text = str(result)[:80]
    return f"result: {text}" if text else "empty"


def run_repl(host: str = "127.0.0.1", port: int = 8822) -> None:
    asyncio.run(Repl(host, port).run())
