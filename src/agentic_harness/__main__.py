import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8822


class ServerManager:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self._host = host
        self._port = port

    def _kill_existing(self) -> None:
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{self._port}"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                pids = result.stdout.strip().split()
                for pid in pids:
                    os.kill(int(pid), signal.SIGKILL)
                time.sleep(0.5)
        except Exception:
            pass

    def start(self) -> None:
        self._kill_existing()
        config = uvicorn.Config(
            "agentic_harness.server.app:app",
            host=self._host,
            port=self._port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self._wait_ready()

    def _wait_ready(self, timeout: float = 10) -> None:
        url = f"http://{self._host}:{self._port}/agents"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                httpx.get(url, timeout=1)
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError(
            f"Server did not start within {timeout}s on http://{self._host}:{self._port}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agentic-harness",
        description="Configurable LangChain agent with tools, permissions, sub-agents, and terminal interface",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    server_parser = sub.add_parser("server", help="Run the API server")
    server_parser.add_argument("--host", default=DEFAULT_HOST, help=f"Host (default: {DEFAULT_HOST})")
    server_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT})")

    interface_parser = sub.add_parser("interface", help="Run the terminal interface (starts embedded server)")
    interface_parser.add_argument("--host", default=DEFAULT_HOST, help=f"Embedded server host (default: {DEFAULT_HOST})")
    interface_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Embedded server port (default: {DEFAULT_PORT})")

    args = parser.parse_args()

    if args.command == "server":
        from agentic_harness.server.app import run_server
        run_server(host=args.host, port=args.port)

    elif args.command == "interface":
        print("Starting embedded server...")
        ServerManager(host=args.host, port=args.port).start()
        server_url = f"http://{args.host}:{args.port}"

        tui_directory = Path(__file__).resolve().parent.parent.parent / "tui"
        tui_entry = tui_directory / "src" / "index.tsx"

        print(f"Launching TUI against {server_url}...")
        tui_process = subprocess.Popen(
            ["bun", "run", str(tui_entry)],
            cwd=str(tui_directory),
            env={
                **os.environ,
                "AH_SERVER_URL": server_url,
            },
        )

        def _forward_signal(signum: int, _frame: object) -> None:
            if tui_process.poll() is None:
                tui_process.send_signal(signum)

        original_sigint = signal.signal(signal.SIGINT, _forward_signal)
        original_sigterm = signal.signal(signal.SIGTERM, _forward_signal)

        try:
            tui_process.wait()
        except KeyboardInterrupt:
            tui_process.wait()
        finally:
            signal.signal(signal.SIGINT, original_sigint)
            signal.signal(signal.SIGTERM, original_sigterm)

        sys.exit(tui_process.returncode)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
