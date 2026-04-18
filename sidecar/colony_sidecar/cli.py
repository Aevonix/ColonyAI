"""Colony sidecar CLI — ``colony-sidecar`` command."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="colony-sidecar",
        description="Colony intelligence sidecar server",
    )
    sub = parser.add_subparsers(dest="command")

    start_p = sub.add_parser("start", help="Start the sidecar server")
    start_p.add_argument("--host", default="127.0.0.1")
    start_p.add_argument("--port", type=int, default=7777)
    start_p.add_argument("--detach", action="store_true", help="Run in background")

    sub.add_parser("status", help="Check sidecar status")

    args = parser.parse_args()

    if args.command == "start":
        import uvicorn
        uvicorn.run(
            "colony_sidecar.server:app",
            host=args.host,
            port=args.port,
            log_level="info",
        )
    elif args.command == "status":
        import httpx
        try:
            resp = httpx.get("http://127.0.0.1:7777/v1/host/health", timeout=5)
            print(resp.json())
        except Exception as exc:
            print(f"Sidecar not reachable: {exc}")
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
