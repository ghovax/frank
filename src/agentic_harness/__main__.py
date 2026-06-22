import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="agentic-harness",
        description="Configurable LangChain agent with tools, permissions, and terminal interface",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    server_parser = sub.add_parser("server", help="Run the API server")
    server_parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    server_parser.add_argument("--port", type=int, default=8822, help="Port to bind (default: 8822)")

    interface_parser = sub.add_parser("interface", help="Run the terminal interface")
    interface_parser.add_argument(
        "--server-url",
        default="http://127.0.0.1:8822",
        help="Server URL (default: http://127.0.0.1:8822)",
    )

    args = parser.parse_args()

    if args.command == "server":
        from agentic_harness.server.app import run_server
        run_server(host=args.host, port=args.port)

    elif args.command == "interface":
        from agentic_harness.interface.app import run_interface
        run_interface(server_url=args.server_url)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
