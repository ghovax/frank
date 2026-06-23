import argparse
import sys


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8822


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agentic-harness",
        description="Configurable LangChain agent with tools, permissions, and sub-agents",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    server_parser = sub.add_parser("server", help="Run the API server")
    server_parser.add_argument("--host", default=DEFAULT_HOST, help=f"Host (default: {DEFAULT_HOST})")
    server_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT})")

    repl_parser = sub.add_parser("repl", help="Run the interactive REPL")
    repl_parser.add_argument("--host", default=DEFAULT_HOST, help=f"Server host (default: {DEFAULT_HOST})")
    repl_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Server port (default: {DEFAULT_PORT})")

    args = parser.parse_args()

    if args.command == "server":
        from agentic_harness.server.app import run_server
        run_server(host=args.host, port=args.port)
    elif args.command == "repl":
        from agentic_harness.repl import run_repl
        run_repl(host=args.host, port=args.port)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
