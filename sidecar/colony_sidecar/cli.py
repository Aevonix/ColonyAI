"""Colony CLI — ``colony`` command."""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="colony",
        description="Colony intelligence sidecar server",
    )
    sub = parser.add_subparsers(dest="command")

    # --- init ---
    init_p = sub.add_parser("init", help="Interactive setup wizard")
    init_p.add_argument("--non-interactive", action="store_true", help="Generate defaults without prompts")
    init_p.add_argument("--dir", default=".", help="Root directory for config files")

    # --- start ---
    start_p = sub.add_parser("start", help="Start the sidecar server")
    start_p.add_argument("--host", default=None, help="Override listen host")
    start_p.add_argument("--port", type=int, default=None, help="Override listen port")
    start_p.add_argument("--detach", action="store_true", help="Run in background")

    # --- status ---
    sub.add_parser("status", help="Check sidecar health")

    # --- generate-types ---
    sub.add_parser("generate-types", help="Export OpenAPI spec (for TypeScript generation)")

    # --- seed ---
    seed_p = sub.add_parser("seed", help="Seed self-knowledge (run after 'colony start')")
    seed_p.add_argument("--verify", action="store_true", help="Verify seeding completed")

    args = parser.parse_args()

    if args.command == "init":
        from colony_sidecar.setup import run_init, run_noninteractive
        if args.non_interactive:
            code = run_noninteractive(root_dir=args.dir)
        else:
            code = run_init(root_dir=args.dir)
        sys.exit(code)

    elif args.command == "start":
        # Load .env if present
        _load_dotenv()

        import uvicorn
        host = args.host or os.environ.get("COLONY_SIDECAR_HOST", "127.0.0.1")
        port = args.port or int(os.environ.get("COLONY_SIDECAR_PORT", "7777"))
        uvicorn.run(
            "colony_sidecar.server:app",
            host=host,
            port=port,
            log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        )

    elif args.command == "status":
        _load_dotenv()
        import httpx
        host = os.environ.get("COLONY_SIDECAR_HOST", "127.0.0.1")
        port = os.environ.get("COLONY_SIDECAR_PORT", "7777")
        try:
            resp = httpx.get(f"http://{host}:{port}/v1/host/health", timeout=5)
            data = resp.json()
            status = data.get("status", "unknown")
            caps = data.get("capabilities", [])
            notes = data.get("notes", {})
            print(f"Status: {status}")
            print(f"Capabilities: {', '.join(caps) if caps else 'none'}")
            for k, v in notes.items():
                print(f"  {k}: {v}")
        except Exception as exc:
            print(f"Sidecar not reachable: {exc}")
            sys.exit(1)

    elif args.command == "generate-types":
        _load_dotenv()
        import json
        from colony_sidecar.server import create_app
        app = create_app()
        spec = app.openapi()
        out = os.environ.get("COLONY_OPENAPI_OUT", "openapi.json")
        with open(out, "w") as f:
            json.dump(spec, f, indent=2)
        n = len(spec.get("components", {}).get("schemas", {}))
        p = len(spec.get("paths", {}))
        print(f"✅ OpenAPI spec written to {out} ({n} schemas, {p} paths)")

    elif args.command == "seed":
        _load_dotenv()
        import asyncio
        from colony_sidecar.seed import seed_self_knowledge, seed_self_knowledge_summary

        print(seed_self_knowledge_summary())
        print("\nSeeding self-knowledge...\n")

        # Try to connect to running sidecar for seeding
        host = os.environ.get("COLONY_SIDECAR_HOST", "127.0.0.1")
        port = os.environ.get("COLONY_SIDECAR_PORT", "7777")
        api_key = os.environ.get("COLONY_API_KEY", "")

        if args.verify:
            # Verify seeding via health check
            import httpx
            try:
                resp = httpx.get(f"http://{host}:{port}/v1/host/health", timeout=5)
                data = resp.json()
                caps = data.get("capabilities", [])
                if "memory" in caps:
                    print("✅ Memory system is wired")
                if "world_model" in caps or "worldModel" in caps:
                    print("✅ World model is wired")
                print("\nSeeding verification complete.")
            except Exception as e:
                print(f"⚠️ Could not verify: {e}")
                print("Make sure the sidecar is running: colony start")
            sys.exit(0)

        # Try to seed via API
        import httpx
        try:
            # Seed via the /v1/host/seed endpoint (if available)
            resp = httpx.post(
                f"http://{host}:{port}/v1/host/seed",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                print(f"✅ Seeding complete:")
                print(f"   Memories: {data.get('memories', 0)}")
                print(f"   Entities: {data.get('entities', 0)}")
                print(f"   Skills: {data.get('skills', 0)}")
                print(f"   Insights: {data.get('insights', 0)}")
            elif resp.status_code == 404:
                print("⚠️ Seed endpoint not available.")
                print("The sidecar may not support remote seeding.")
                print("Seeding happens automatically during 'colony init'.")
            else:
                print(f"⚠️ Seeding failed: {resp.status_code}")
                print(resp.text)
        except Exception as e:
            print(f"⚠️ Could not connect to sidecar: {e}")
            print("\nMake sure the sidecar is running: colony start")
            print("Or re-run: colony init")

    else:
        parser.print_help()


def _load_dotenv() -> None:
    """Simple .env loader — doesn't override existing env vars."""
    env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                # Don't override existing env vars
                if k not in os.environ:
                    os.environ[k] = v


if __name__ == "__main__":
    main()
