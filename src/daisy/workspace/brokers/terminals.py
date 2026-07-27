"""Terminal domain: the PTY-backed TerminalSession and its manager, plus terminal-state
persistence."""

from __future__ import annotations

from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
from fastapi import HTTPException
from daisy.base.sqlite_lock import sqlite_write_lock
from daisy.locations.executor import SshExecutor
from pathlib import Path
from typing import Any
import asyncio
import fcntl
import hashlib
import os
import platform
import pty
import pwd
import signal
import struct
import termios
from daisy.workspace import state
from daisy.workspace.database import SessionRecord, TerminalStateRecord


def _terminal_directory(session_id: str, working_directory: str) -> Path:
    directory = working_directory.strip()
    if session_id and state.session_factory is not None:
        database_session = state.session_factory()
        try:
            record = database_session.get(SessionRecord, session_id)
            if record is not None:
                directory = record.runtime_working_directory or record.working_directory or directory
        finally:
            database_session.close()
    path = Path(directory or str(Path.home())).expanduser()
    if not path.is_absolute():
        path = Path.home() / path
    resolved = path.resolve(strict=False)
    if not resolved.is_dir():
        raise ValueError(f"Terminal directory does not exist: {resolved}")
    return resolved


def _shell_command() -> list[str]:
    # The user's real login shell comes from the passwd database — the same source
    # `login` uses — not from $SHELL, which reflects whatever shell happened to launch
    # this server and would be wrong on a remote/shared host.
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell
    except (KeyError, OSError):
        shell = ""
    if shell and Path(shell).exists():
        return [shell, "-l"]
    if platform.system() == "Darwin" and Path("/bin/zsh").exists():
        return ["/bin/zsh", "-l"]
    if Path("/bin/bash").exists():
        return ["/bin/bash", "-l"]
    return ["/bin/sh", "-l"]


def _login_base_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    try:
        record = pwd.getpwuid(os.getuid())
        environment["HOME"] = record.pw_dir
        environment["USER"] = record.pw_name
        environment["LOGNAME"] = record.pw_name
        if record.pw_shell:
            environment["SHELL"] = record.pw_shell
    except (KeyError, OSError):
        # No passwd entry (unusual): fall back to the interpreter's notion of $HOME.
        environment["HOME"] = str(Path.home())
    # The default PATH `login` seeds; the login shell and its rc files (e.g. macOS
    # path_helper, home-manager) immediately rebuild the real one on top of it.
    environment["PATH"] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    # The PTY is an xterm-compatible emulator, so advertise it as one.
    environment["TERM"] = "xterm-256color"
    environment["COLORTERM"] = "truecolor"
    return environment


def _resize_pty(master_fd: int, rows: int, columns: int) -> None:
    safe_rows = max(1, min(int(rows or 24), 200))
    safe_columns = max(1, min(int(columns or 80), 400))
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", safe_rows, safe_columns, 0, 0))


async def _read_pty(master_fd: int) -> bytes:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bytes] = loop.create_future()

    def on_ready() -> None:
        if future.done():
            return
        try:
            future.set_result(os.read(master_fd, 8192))
        except OSError as exception:
            future.set_exception(exception)

    loop.add_reader(master_fd, on_ready)
    try:
        return await future
    finally:
        loop.remove_reader(master_fd)


def _terminal_context_identifier(session_id: str, directory: Path) -> str:
    if session_id:
        return session_id
    digest = hashlib.sha256(str(directory).encode("utf-8")).hexdigest()[:16]
    return f"working-directory:{digest}"


def _load_terminal_state(terminal_context: str, terminal_key: str) -> str:
    if state.session_factory is None:
        return ""
    database_session = state.session_factory()
    try:
        record = database_session.get(TerminalStateRecord, (terminal_context, terminal_key))
        return record.scrollback if record is not None and record.scrollback else ""
    finally:
        database_session.close()


def _save_terminal_state(terminal_context: str, terminal_key: str, directory: Path, scrollback: str) -> None:
    if state.session_factory is None:
        return
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            now = datetime.now(timezone.utc).isoformat()
            record = database_session.get(TerminalStateRecord, (terminal_context, terminal_key))
            if record is None:
                record = TerminalStateRecord(
                    session_id=terminal_context,
                    terminal_key=terminal_key,
                    working_directory=str(directory),
                    scrollback=scrollback,
                    created_at=now,
                    updated_at=now,
                )
                database_session.add(record)
            else:
                record.working_directory = str(directory)
                record.scrollback = scrollback
                record.updated_at = now
            database_session.commit()
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _list_terminal_states(terminal_context: str) -> list[dict[str, str]]:
    """Persisted terminals for a context, ordered by creation so the client can rebuild
    a stable set of tabs. Runs off the event loop (synchronous history.db read)."""
    if state.session_factory is None:
        return []
    database_session = state.session_factory()
    try:
        records = (
            database_session.query(TerminalStateRecord)
            .filter(TerminalStateRecord.session_id == terminal_context)
            .order_by(TerminalStateRecord.created_at.asc(), TerminalStateRecord.terminal_key.asc())
            .all()
        )
    finally:
        database_session.close()
    entries = [
        {
            "terminal_key": record.terminal_key,
            "working_directory": record.working_directory,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
        for record in records
    ]
    return entries


def _delete_terminal_state(terminal_context: str, terminal_key: str) -> None:
    if state.session_factory is None:
        return
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            record = database_session.get(TerminalStateRecord, (terminal_context, terminal_key))
            if record is not None:
                database_session.delete(record)
                database_session.commit()
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


class TerminalSession:
    def __init__(
        self,
        terminal_context: str,
        terminal_key: str,
        directory: Path,
        rows: int,
        columns: int,
        persisted_scrollback: str,
        remote_host_alias: str = "",
    ):
        self.terminal_context = terminal_context
        self.terminal_key = terminal_key
        self.directory = directory
        # When set, the terminal is an interactive login shell on this remote host (over
        # multiplexed SSH), in `directory` on that machine, rather than a local PTY.
        self.remote_host_alias = remote_host_alias
        self.master_fd = -1
        self.pid = -1
        self.rows = rows
        self.columns = columns
        self.exit_code: int | None = None
        self.exit_signal: int | None = None
        self._buffer: deque[str] = deque()
        self._buffer_characters = 0
        self._maximum_buffer_characters = 200_000
        self._subscribers: set[asyncio.Queue] = set()
        self._reader_task: asyncio.Task | None = None
        self._persist_task: asyncio.Task | None = None
        self._closed = False
        if persisted_scrollback:
            self._append_scrollback(persisted_scrollback)

    @property
    def exited(self) -> bool:
        return self.exit_code is not None or self.exit_signal is not None

    @property
    def running(self) -> bool:
        return self.pid > 0 and not self.exited and not self._closed

    async def start(self) -> None:
        if self.running:
            return
        environment = _login_base_environment()
        if self.remote_host_alias:
            # Remote terminal: ssh to the host and start an interactive login shell in the
            # location's base dir. The `cd` happens on the remote (never locally).
            command = SshExecutor(self.remote_host_alias).terminal_argv(str(self.directory))
        else:
            command = _shell_command()
        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                if not self.remote_host_alias:
                    os.chdir(self.directory)
                os.execvpe(command[0], command, environment)
            except BaseException:
                os._exit(127)
        self.pid = pid
        self.master_fd = master_fd
        self.exit_code = None
        self.exit_signal = None
        _resize_pty(self.master_fd, self.rows, self.columns)
        self._reader_task = asyncio.create_task(self._read_loop())

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        snapshot = self.scrollback()
        if snapshot:
            queue.put_nowait({"type": "output", "data": snapshot})
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def resize(self, rows: int, columns: int) -> None:
        self.rows = max(1, min(int(rows or 24), 200))
        self.columns = max(1, min(int(columns or 80), 400))
        if self.master_fd >= 0 and self.running:
            _resize_pty(self.master_fd, self.rows, self.columns)

    def write(self, data: str) -> None:
        if not data or self.master_fd < 0 or not self.running:
            return
        os.write(self.master_fd, data.encode("utf-8", errors="replace"))

    def scrollback(self) -> str:
        return "".join(self._buffer)

    async def close(self) -> None:
        self._closed = True
        if self._persist_task is not None:
            self._persist_task.cancel()
            await asyncio.gather(self._persist_task, return_exceptions=True)
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        if self.pid > 0 and self.exit_code is None and self.exit_signal is None:
            with suppress(ProcessLookupError):
                os.killpg(self.pid, signal.SIGHUP)
            with suppress(Exception):
                await asyncio.to_thread(os.waitpid, self.pid, 0)
        if self.master_fd >= 0:
            with suppress(OSError):
                os.close(self.master_fd)
            self.master_fd = -1
        await self.persist()

    async def persist(self) -> None:
        await asyncio.to_thread(
            _save_terminal_state,
            self.terminal_context,
            self.terminal_key,
            self.directory,
            self.scrollback(),
        )

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    chunk = await _read_pty(self.master_fd)
                except OSError:
                    break
                if not chunk:
                    break
                data = chunk.decode("utf-8", errors="replace")
                self._append_scrollback(data)
                self._broadcast({"type": "output", "data": data})
                self._schedule_persist()
        finally:
            if not self._closed:
                await self._record_exit()
                self._broadcast({
                    "type": "exit",
                    "exit_code": self.exit_code,
                    "exit_signal": self.exit_signal,
                })
            await self.persist()

    async def _record_exit(self) -> None:
        if self.pid <= 0 or self.exited:
            return
        try:
            waited_pid, status = await asyncio.to_thread(os.waitpid, self.pid, os.WNOHANG)
            if waited_pid == 0:
                waited_pid, status = await asyncio.to_thread(os.waitpid, self.pid, 0)
        except ChildProcessError:
            self.exit_code = 0
            return
        if os.WIFEXITED(status):
            self.exit_code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self.exit_signal = os.WTERMSIG(status)
        else:
            self.exit_code = 0

    def _append_scrollback(self, data: str) -> None:
        self._buffer.append(data)
        self._buffer_characters += len(data)
        while self._buffer_characters > self._maximum_buffer_characters and self._buffer:
            removed = self._buffer.popleft()
            self._buffer_characters -= len(removed)

    def _broadcast(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    def _schedule_persist(self) -> None:
        if self._persist_task is not None and not self._persist_task.done():
            return

        async def delayed_persist() -> None:
            await asyncio.sleep(1)
            await self.persist()

        self._persist_task = asyncio.create_task(delayed_persist())


class TerminalSessionManager:
    def __init__(self):
        self._sessions: dict[tuple[str, str], TerminalSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        session_id: str,
        directory: Path,
        rows: int,
        columns: int,
        terminal_key: str = "main",
        remote_host_alias: str = "",
    ) -> TerminalSession:
        terminal_context = _terminal_context_identifier(session_id, directory)
        key = (terminal_context, terminal_key)
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and existing.running:
                existing.resize(rows, columns)
                return existing
            persisted_scrollback = await asyncio.to_thread(_load_terminal_state, terminal_context, terminal_key)
            session = TerminalSession(terminal_context, terminal_key, directory, rows, columns, persisted_scrollback, remote_host_alias=remote_host_alias)
            self._sessions[key] = session
            await session.start()
            return session

    def live_keys(self, terminal_context: str) -> set[str]:
        return {
            terminal_key
            for (identifier, terminal_key), session in self._sessions.items()
            if identifier == terminal_context and session.running
        }

    async def close_one(self, terminal_context: str, terminal_key: str) -> None:
        key = (terminal_context, terminal_key)
        async with self._lock:
            session = self._sessions.pop(key, None)
        if session is not None:
            await session.close()

    async def close_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)


async def _terminal_context_for_request(session_id: str, working_directory: str) -> str:
    """Resolve the identifier a context's terminals are stored under. For a persisted
    session the identifier is the context id itself, so we can answer even when the
    working directory no longer exists; otherwise it is derived from the resolved
    directory (which must exist)."""
    try:
        directory = await asyncio.to_thread(_terminal_directory, session_id, working_directory)
    except ValueError:
        if session_id:
            return session_id
        raise HTTPException(status_code=400, detail="Terminal directory does not exist.")
    return _terminal_context_identifier(session_id, directory)
