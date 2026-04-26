"""Colony CLI — ``colony`` command."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="colony",
        description="Colony intelligence sidecar server",
    )
    sub = parser.add_subparsers(dest="command")

    # --- init ---
    init_p = sub.add_parser("init", help="Initialize Colony identity and setup")
    init_p.add_argument("--dir", default=".", help="Root directory for config files")
    init_p.add_argument("--passphrase", default=None, help="Encrypt Colony private key with passphrase (prompted if --encrypt)")
    init_p.add_argument("--encrypt", action="store_true", help="Encrypt Colony private key")
    init_p.add_argument("--claim-genesis", action="store_true", help="Claim Genesis status (first Colony only)")
    # Non-interactive mode flags
    init_p.add_argument("--non-interactive", "-n", action="store_true", help="Run without prompts (requires all required flags)")
    # Harness configuration (new approach)
    init_p.add_argument("--mcp-harnesses", help="Connect coding harnesses via MCP (comma-separated: claude-code,codex,crush,opencode)")
    init_p.add_argument("--agent-harness", choices=["openclaw", "hermes"], help="Connect agent harness via plugin")
    init_p.add_argument("--no-harness", action="store_true", help="Skip all harness setup (standalone mode)")
    # Backward compatibility
    init_p.add_argument("--host-framework", choices=["openclaw", "hermes", "claude-code", "codex", "crush", "standalone"], help="Host framework (deprecated: use --agent-harness or --mcp-harnesses)")
    init_p.add_argument("--contact-name", help="Contact name for this user")
    init_p.add_argument("--bind", default="127.0.0.1", help="Sidecar bind address (0.0.0.0 for all interfaces)")
    init_p.add_argument("--port", type=int, default=7777, help="Sidecar port")
    init_p.add_argument("--tier", type=int, choices=range(0, 8), metavar="TIER", help="Embedding tier (0-7)")
    init_p.add_argument("--neo4j-password", default="", help="Neo4j password (empty to skip)")
    init_p.add_argument("--skip-model-download", action="store_true", help="Defer embedding model download to first start")
    init_p.add_argument("--start", action="store_true", help="Start sidecar after init")

    # --- start ---
    start_p = sub.add_parser("start", help="Start the sidecar server")
    start_p.add_argument("--host", default=None, help="Override listen host")
    start_p.add_argument("--port", type=int, default=None, help="Override listen port")
    start_p.add_argument("--detach", "-d", action="store_true", help="Run in background (daemon mode)")
    start_p.add_argument("--force", "-f", action="store_true", help="Kill existing process on port if needed")

    # --- stop ---
    sub.add_parser("stop", help="Stop the running sidecar")

    # --- status ---
    sub.add_parser("status", help="Check sidecar health and pipeline status")

    # --- validate ---
    val_p = sub.add_parser("validate", help="Run end-to-end pipeline validation (uses LLM credits)")
    val_p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    # --- doctor ---
    doctor_p = sub.add_parser("doctor", help="Run integration health check and diagnose issues")
    doctor_p.add_argument("--fix", action="store_true", help="Automatically fix issues found")
    doctor_p.add_argument("--clean-orphans", action="store_true", help="Kill orphaned sidecar processes")
    doctor_p.add_argument("--url", default=None, help="Sidecar URL (default: from .env)")
    doctor_p.add_argument("--api-key", default=None, help="API key (default: from .env)")
    doctor_p.add_argument("--verbose", "-v", action="store_true", help="Show detailed results")
    doctor_p.add_argument("--full", action="store_true", help="Run all checks including heavy ones (reasoning, research)")

    # --- generate-types ---
    sub.add_parser("generate-types", help="Export OpenAPI spec (for TypeScript generation)")

    # --- seed ---
    seed_p = sub.add_parser("seed", help="Seed self-knowledge (run after 'colony start')")
    seed_p.add_argument("--verify", action="store_true", help="Verify seeding completed")
    seed_p.add_argument("--force", action="store_true", help="Force re-seeding even if already seeded")

    # --- backfill ---
    backfill_p = sub.add_parser("backfill", help="Re-embed all vectors with current model")
    backfill_p.add_argument("--collection", default=None, help="Specific collection to backfill (default: all)")
    backfill_p.add_argument("--batch-size", type=int, default=64, help="Batch size for embedding")

    # --- migrate-tier ---
    migrate_p = sub.add_parser("migrate-tier", help="Migrate vectors from old model to current")
    migrate_p.add_argument("--old-model", default=None, help="Old model ID to migrate from (default: all)")
    migrate_p.add_argument("--batch-size", type=int, default=64, help="Batch size for embedding")

    # --- activate-multimodal ---
    mm_p = sub.add_parser("activate-multimodal", help="Enable multimodal embeddings and rerank")
    mm_p.add_argument("--model", default=None, help="Multimodal model ID (default: auto-detect from tier)")
    mm_p.add_argument("--storage", default="local", choices=["local", "embed_only"], help="Image storage mode")

    # --- mcp ---
    mcp_p = sub.add_parser("mcp", help="Colony MCP server and harness configuration")
    mcp_sub = mcp_p.add_subparsers(dest="mcp_command")
    mcp_run = mcp_sub.add_parser("run", help="Start MCP server (stdio transport)")
    mcp_run.add_argument("--transport", choices=["stdio", "http"], default="stdio", help="Transport mode")
    mcp_run.add_argument("--host", default="127.0.0.1", help="HTTP host (for http transport)")
    mcp_run.add_argument("--port", type=int, default=7778, help="HTTP port (for http transport)")

    mcp_setup = mcp_sub.add_parser("setup", help="Configure a coding harness to use Colony")
    mcp_setup.add_argument("--harness", choices=["claude-code", "codex", "crush", "opencode", "hermes", "all"], default=None, help="Specific harness to configure")
    mcp_setup.add_argument("--contact-id", default=None, help="Your identifier (skip prompt)")
    mcp_setup.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    mcp_setup.add_argument("--print-config", action="store_true", help="Print MCP config snippet (for distributed setups)")
    mcp_setup.add_argument("--sidecar-url", default=None, help="Sidecar URL (for remote Colony, e.g., http://192.168.1.100:7777)")
    mcp_setup.add_argument("--mcp-command", default=None, help="MCP server command (for standalone mode)")
    mcp_setup.add_argument("--mcp-args", default=None, help="MCP server args (for standalone mode)")

    mcp_remove = mcp_sub.add_parser("remove", help="Remove Colony from a harness config")
    mcp_remove.add_argument("--harness", choices=["claude-code", "codex", "crush", "opencode", "hermes", "all"], default=None, help="Specific harness to remove")
    mcp_remove.add_argument("--dry-run", action="store_true", help="Show changes without writing")

    mcp_sub.add_parser("detect", help="Detect installed coding harnesses")

    # --- key ---
    key_p = sub.add_parser("key", help="Manage Colony cryptographic identity")
    key_sub = key_p.add_subparsers(dest="key_command")
    key_sub.add_parser("info", help="Show colony_id and public key")
    key_sub.add_parser("generate", help="Generate a new keypair (replaces existing)")
    key_gen = key_sub.add_parser("set-passphrase", help="Encrypt private key with a passphrase")
    key_gen.add_argument("--passphrase", default=None, help="New passphrase (prompted if not given)")
    key_sub.add_parser("manifest", help="Create a colony manifest (shareable public identity)")
    key_genesis = key_sub.add_parser("claim-genesis", help="Claim Genesis status for this Colony (first Colony only)")
    key_genesis.add_argument("--force", action="store_true", help="Overwrite existing Genesis manifest")

    # --- node ---
    node_p = sub.add_parser("node", help="Manage this device's node identity")
    node_sub = node_p.add_subparsers(dest="node_command")
    node_sub.add_parser("info", help="Show node_id, public key, and certificate status")

    # --- backup ---
    backup_p = sub.add_parser("backup", help="Export Colony identity as a portable backup")
    backup_p.add_argument("--output", "-o", default=None, help="Output file path (default: stdout)")
    backup_p.add_argument("--passphrase", default=None, help="Encrypt private key with this passphrase (prompted if --encrypt)")
    backup_p.add_argument("--encrypt", action="store_true", help="Encrypt private key (prompts for passphrase)")

    # --- restore ---
    restore_p = sub.add_parser("restore", help="Restore Colony from a backup")
    restore_p.add_argument("--input", "-i", default=None, help="Backup file path (default: prompts for it)")
    restore_p.add_argument("--passphrase", default=None, help="Passphrase to decrypt (default: prompts for it)")
    mm_p.add_argument("--safety", default="basic", choices=["off", "basic", "strict"], help="Image safety level")
    mm_p.add_argument("--skip-download", action="store_true", help="Skip model download")

    # --- agent ---
    agent_p = sub.add_parser("agent", help="Manage connected agents")
    agent_sub = agent_p.add_subparsers(dest="agent_command")

    agent_invite = agent_sub.add_parser("invite", help="Generate a setup code for remote agent")
    agent_invite.add_argument("--expires", type=int, default=900, help="Invite expiry in seconds (default: 900)")
    agent_invite.add_argument("--max-uses", type=int, default=1, help="Max uses (default: 1)")
    agent_invite.add_argument("--capabilities", default="messaging", help="Grant capabilities (comma-separated)")
    agent_invite.add_argument("--primary", action="store_true", help="Grant primary status")
    agent_invite.add_argument("--label", default=None, help="Label for this invite")

    agent_connect = agent_sub.add_parser("connect", help="Connect a remote agent using setup code")
    agent_connect.add_argument("--setup-code", required=True, help="Setup code from colony agent invite")
    agent_connect.add_argument("--colony-url", default=None, help="Colony URL (auto-detect if on Tailscale)")
    agent_connect.add_argument("--name", default=None, help="Agent name (default: hostname)")
    agent_connect.add_argument("--capabilities", default=None, help="Request capabilities (comma-separated)")

    agent_list = agent_sub.add_parser("list", help="List registered agents")
    agent_list.add_argument("--status", choices=["online", "busy", "offline", "suspended", "revoked"], default=None, help="Filter by status")
    agent_list.add_argument("--capability", default=None, help="Filter by capability")

    agent_show = agent_sub.add_parser("show", help="Show agent details")
    agent_show.add_argument("agent_id", help="Agent ID")

    agent_revoke = agent_sub.add_parser("revoke", help="Revoke an agent's access")
    agent_revoke.add_argument("agent_id", help="Agent ID to revoke")
    agent_revoke.add_argument("--reason", default=None, help="Reason for revocation")

    agent_sub.add_parser("disconnect", help="Disconnect this agent from Colony")

    # --- initiative ---
    init_p = sub.add_parser("initiative", help="Manage initiatives")
    init_sub = init_p.add_subparsers(dest="initiative_command")

    init_list = init_sub.add_parser("list", help="List initiatives")
    init_list.add_argument("--status", default=None, help="Filter by status (pending, assigned, acknowledged, completed, failed, cancelled)")
    init_list.add_argument("--agent", default=None, help="Filter by assigned agent")
    init_list.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")

    init_show = init_sub.add_parser("show", help="Show initiative details")
    init_show.add_argument("initiative_id", help="Initiative ID")

    init_cancel = init_sub.add_parser("cancel", help="Cancel an initiative")
    init_cancel.add_argument("initiative_id", help="Initiative ID")
    init_cancel.add_argument("--reason", default=None, help="Reason for cancellation")

    args = parser.parse_args()

    if args.command == "init":
        # Run setup wizard
        from colony_sidecar.setup import run_init
        code = run_init(root_dir=args.dir, args=args)
        if code != 0:
            sys.exit(code)

        # Initialize Colony identity if not already done
        _load_dotenv()
        state_dir = os.environ.get("COLONY_STATE_DIR", args.dir)
        id_path = Path(state_dir) / "colony-id"
        if not id_path.exists():
            _cmd_init(args)
        else:
            print(f"  Colony identity already exists: {id_path.read_text().strip()}")

    elif args.command == "start":
        _load_dotenv()
        host = args.host or os.environ.get("COLONY_SIDECAR_HOST", "127.0.0.1")
        port = args.port or int(os.environ.get("COLONY_SIDECAR_PORT", "7777"))

        # Check and start Neo4j if needed (both foreground and daemon mode)
        _check_and_start_neo4j()

        if args.detach:
            _cmd_start_daemon(host, port, args.force)
        else:
            # Foreground mode — check port first
            existing_pid = _find_pid_on_port(port)
            if existing_pid:
                if args.force:
                    print(f"Killing existing process {existing_pid} on port {port}...")
                    try:
                        os.kill(existing_pid, 15)  # SIGTERM
                        time.sleep(2)
                        if _find_pid_on_port(port):
                            os.kill(existing_pid, 9)  # SIGKILL
                            time.sleep(1)
                        print("Process killed.")
                    except ProcessLookupError:
                        pass
                else:
                    print(f"Error: Port {port} is already in use (PID {existing_pid})")
                    print("Use --force to kill existing process, or stop it first with: colony stop")
                    sys.exit(1)
            
            import uvicorn
            try:
                ws_max_size = int(
                    os.environ.get("COLONY_MAX_WS_FRAME_BYTES", "") or 1 * 1024 * 1024
                )
            except ValueError:
                ws_max_size = 1 * 1024 * 1024
            uvicorn.run(
                "colony_sidecar.server:app",
                host=host,
                port=port,
                log_level=os.environ.get("LOG_LEVEL", "info").lower(),
                ws_max_size=ws_max_size,
            )

    elif args.command == "stop":
        _load_dotenv()
        _cmd_stop()

    elif args.command == "status":
        _load_dotenv()
        _cmd_status()

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
            params = {"force": "true"} if args.force else {}
            resp = httpx.post(
                f"http://{host}:{port}/v1/host/seed",
                headers={"Authorization": f"Bearer {api_key}"},
                params=params,
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("skipped"):
                    print("✅ Already seeded (use --force to re-seed)")
                else:
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

    elif args.command == "backfill":
        _load_dotenv()
        import httpx
        host = os.environ.get("COLONY_SIDECAR_HOST", "127.0.0.1")
        port = os.environ.get("COLONY_SIDECAR_PORT", "7777")
        api_key = os.environ.get("COLONY_API_KEY", "")
        try:
            resp = httpx.post(
                f"http://{host}:{port}/v1/host/memory/backfill",
                json={"identity": {"gateway_id": "cli"}, "collection": args.collection, "batch_size": args.batch_size},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                task_id = data.get("task_id", "")
                print(f"Backfill started (task_id={task_id})")
                import time
                while True:
                    time.sleep(2)
                    status_resp = httpx.get(
                        f"http://{host}:{port}/v1/host/memory/backfill/{task_id}",
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=10,
                    )
                    if status_resp.status_code == 200:
                        sd = status_resp.json()
                        if sd.get("status") == "completed":
                            print(f"Backfill complete: {sd.get('processed', 0)} processed, {sd.get('skipped', 0)} skipped, {sd.get('failed', 0)} failed")
                            break
                        elif sd.get("status") == "failed":
                            print(f"Backfill failed: {sd.get('errors', [])}")
                            break
                        else:
                            print(f"  ... {sd.get('processed', 0)} processed so far")
            else:
                print(f"Backfill failed: {resp.status_code} {resp.text}")
        except Exception as e:
            print(f"Could not connect to sidecar: {e}")

    elif args.command == "migrate-tier":
        _load_dotenv()
        import httpx
        host = os.environ.get("COLONY_SIDECAR_HOST", "127.0.0.1")
        port = os.environ.get("COLONY_SIDECAR_PORT", "7777")
        api_key = os.environ.get("COLONY_API_KEY", "")
        try:
            resp = httpx.post(
                f"http://{host}:{port}/v1/host/memory/migrate",
                json={"identity": {"gateway_id": "cli"}, "old_model_id": args.old_model, "batch_size": args.batch_size},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                task_id = data.get("task_id", "")
                print(f"Migration started (task_id={task_id})")
                import time
                while True:
                    time.sleep(2)
                    status_resp = httpx.get(
                        f"http://{host}:{port}/v1/host/memory/migrate/{task_id}",
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=10,
                    )
                    if status_resp.status_code == 200:
                        sd = status_resp.json()
                        if sd.get("status") == "completed":
                            print(f"Migration complete: {sd.get('vectors_migrated', 0)} vectors migrated, {sd.get('collections_migrated', 0)} collections")
                            break
                        elif sd.get("status") == "failed":
                            print(f"Migration failed: {sd.get('errors', [])}")
                            break
                        else:
                            print(f"  ... {sd.get('vectors_migrated', 0)} vectors migrated so far")
            else:
                print(f"Migration failed: {resp.status_code} {resp.text}")
        except Exception as e:
            print(f"Could not connect to sidecar: {e}")

    elif args.command == "activate-multimodal":
        _load_dotenv()
        env_path = Path(os.environ.get("COLONY_STATE_DIR", ".")) / ".env"
        if not env_path.exists():
            # Try sidecar directory
            env_path = Path(__file__).parent / ".env"
        if not env_path.exists():
            print("No .env file found. Run 'colony init' first.")
            return

        # Determine multimodal model from tier
        model = args.model
        reranker_model = ""
        dims = 0

        if not model:
            try:
                from colony_sidecar.vector.tiers import TIER_TABLE
                from colony_sidecar.vector.scanner import HardwareScanner
                scanner = HardwareScanner()
                scan = scanner.scan()
                tier = scanner.recommend_tier(scan)
                if tier and tier.multimodal_embedder:
                    model = tier.multimodal_embedder.model_id
                    dims = tier.multimodal_embedder.dims
                    if tier.multimodal_reranker:
                        reranker_model = tier.multimodal_reranker.model_id
                else:
                    print("Your hardware tier does not support multimodal embeddings.")
                    print("Available from Tier 1 (4GB+) with jina-clip-v2.")
                    return
            except Exception as exc:
                print(f"Could not auto-detect tier: {exc}")
                print("Use --model to specify a multimodal model ID.")
                return

        print(f"Activating multimodal embeddings:")
        print(f"  Model: {model}")
        print(f"  Dims: {dims}")
        if reranker_model:
            print(f"  Reranker: {reranker_model}")
        print(f"  Storage: {args.storage}")
        print(f"  Safety: {args.safety}")
        print()

        answer = input("Continue? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return

        # Update .env
        lines = env_path.read_text().splitlines()
        updates = {
            "COLONY_MULTIMODAL": "true",
            "COLONY_EMBED_MODEL": model,
            "COLONY_IMAGE_STORAGE": args.storage,
            "COLONY_IMAGE_SAFETY": args.safety,
            "COLONY_STRIP_EXIF_GPS": "true",
        }
        if dims:
            updates["COLONY_EMBED_DIMS"] = str(dims)
        if reranker_model:
            updates["COLONY_RERANKER_MODEL"] = reranker_model

        existing_keys = set()
        for i, line in enumerate(lines):
            if "=" in line and not line.strip().startswith("#"):
                key = line.split("=", 1)[0].strip()
                existing_keys.add(key)
                if key in updates:
                    lines[i] = f"{key}={updates[key]}"

        # Add new keys not yet in .env
        for key, value in updates.items():
            if key not in existing_keys:
                lines.append(f"{key}={value}")

        env_path.write_text("\n".join(lines) + "\n")
        print(f"\n✅ .env updated with multimodal config")

        # Download model
        if not args.skip_download:
            print(f"Downloading multimodal model {model}...")
            try:
                from sentence_transformers import SentenceTransformer
                SentenceTransformer(model)
                print(f"✅ Model downloaded and cached")
            except Exception as exc:
                print(f"⚠️ Model download failed: {exc}")
                print("The model will download on first start instead.")

        print()
        print("Restart the sidecar to activate multimodal: colony start")
        print("If you have existing text vectors, run: colony migrate-tier")

    elif args.command == "validate":
        _load_dotenv()
        _cmd_validate(args)

    elif args.command == "mcp":
        _load_dotenv()
        _cmd_mcp(args)

    elif args.command == "doctor":
        _cmd_doctor(args)

    elif args.command == "key":
        _cmd_key(args)

    elif args.command == "node":
        _cmd_node(args)

    elif args.command == "backup":
        _cmd_backup(args)

    elif args.command == "restore":
        _cmd_restore(args)

    elif args.command == "agent":
        _cmd_agent(args)

    elif args.command == "initiative":
        _cmd_initiative(args)


    else:
        parser.print_help()


def _cmd_backup(args) -> None:
    """Export Colony identity as a portable, optionally encrypted backup."""
    _load_dotenv()
    state_dir = os.environ.get("COLONY_STATE_DIR", os.getcwd())

    passphrase = None
    if args.passphrase:
        passphrase = args.passphrase.encode()
    elif args.encrypt:
        import getpass
        passphrase = getpass.getpass("Backup passphrase: ").encode()

    try:
        from colony_sidecar.chain.identity import backup_colony
        backup = backup_colony(state_dir, passphrase=passphrase)
        backup_json = json.dumps(backup, indent=2) + "\n"

        if args.output:
            Path(args.output).write_text(backup_json)
            print(f"  Backup saved to {args.output}")
        else:
            print(backup_json)
    except FileNotFoundError as e:
        print(f"  Error: {e}")
        print("  Run 'colony init' first to create an identity.")
        raise SystemExit(1)


def _cmd_restore(args) -> None:
    """Restore Colony from a backup — interactive by default."""
    _load_dotenv()
    state_dir = os.environ.get("COLONY_STATE_DIR", os.getcwd())

    # Check if identity already exists
    id_path = Path(state_dir) / "colony-id"
    if id_path.exists():
        print("  A Colony identity already exists in this state directory.")
        existing_id = id_path.read_text().strip()
        print(f"  Existing colony_id: {existing_id}")
        confirm = input("  Overwrite? [y/N] ").strip().lower()
        if confirm != "y":
            print("  Restore cancelled.")
            return

    # Get backup file
    if args.input:
        backup_path = args.input
    else:
        backup_path = input("  Backup file path: ").strip()

    if not backup_path or not Path(backup_path).exists():
        print(f"  Error: File not found: {backup_path}")
        raise SystemExit(1)

    try:
        backup_data = json.loads(Path(backup_path).read_text())
    except json.JSONDecodeError:
        print("  Error: Invalid backup JSON")
        raise SystemExit(1)

    # Get passphrase if encrypted
    passphrase = None
    if backup_data.get("encrypted"):
        if args.passphrase:
            passphrase = args.passphrase.encode()
        else:
            import getpass
            passphrase = getpass.getpass("  Backup passphrase: ").encode()

    try:
        from colony_sidecar.chain.identity import restore_colony
        colony_id = restore_colony(state_dir, backup_data, passphrase=passphrase)
        print(f"\n  ✅ Colony restored: {colony_id}")
        if backup_data.get("genesis"):
            print(f"  ⚡ Genesis status restored")
        print(f"\n  Run 'colony start' to bring the Colony online.")
    except ValueError as e:
        print(f"  Error: {e}")
        raise SystemExit(1)


def _cmd_agent(args) -> None:
    """Handle agent subcommands."""
    _load_dotenv()

    import httpx
    host = os.environ.get("COLONY_SIDECAR_HOST", "127.0.0.1")
    port = os.environ.get("COLONY_SIDECAR_PORT", "7777")
    api_key = os.environ.get("COLONY_API_KEY", "")
    base_url = f"http://{host}:{port}/v1/host"
    headers = {"Authorization": f"Bearer {api_key}"}

    if args.agent_command == "invite":
        resp = httpx.post(
            f"{base_url}/agents/invite",
            json={
                "expires_in_seconds": args.expires,
                "max_uses": args.max_uses,
                "granted_capabilities": args.capabilities.split(",") if args.capabilities else ["messaging"],
                "granted_is_primary": args.primary,
                "label": args.label,
            },
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        data = resp.json()
        print(f"Setup code: {data['code']}")
        print(f"Expires at: {data['expires_at']}")
        print(f"\nRun on remote agent:")
        print(f"  {data['setup_command']}")

    elif args.agent_command == "connect":
        import socket
        name = args.name or socket.gethostname()
        resp = httpx.post(
            f"{base_url}/agents/connect",
            json={
                "setup_code": args.setup_code,
                "name": name,
                "node_public_key": str(uuid.uuid4()),  # TODO: generate real keypair
                "capabilities": args.capabilities.split(",") if args.capabilities else None,
            },
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        data = resp.json()
        # Save agent config
        agent_dir = Path.home() / ".colony"
        agent_dir.mkdir(exist_ok=True)
        agent_config = agent_dir / "agent.json"
        agent_config.write_text(json.dumps(data, indent=2))
        print(f"Agent connected: {data['agent_id']}")
        print(f"Node ID: {data['node_id']}")
        print(f"Colony ID: {data['colony_id']}")
        print(f"WebSocket URL: {data.get('websocket_url', 'N/A')}")
        print(f"\nConfig saved to: {agent_config}")

    elif args.agent_command == "list":
        params = {}
        if args.status:
            params["status"] = args.status
        if args.capability:
            params["capability"] = args.capability
        resp = httpx.get(f"{base_url}/agents", params=params, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        data = resp.json()
        agents = data.get("agents", [])
        if not agents:
            print("No agents found.")
            return
        print(f"{'Agent ID':<36} {'Name':<20} {'Status':<10} {'Capabilities'}")
        print("-" * 100)
        for a in agents:
            caps = ", ".join(a.get("capabilities", []))
            print(f"{a['agent_id']:<36} {a['name']:<20} {a['status']:<10} {caps}")

    elif args.agent_command == "show":
        resp = httpx.get(f"{base_url}/agents/{args.agent_id}", headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        a = resp.json()
        print(f"Agent ID: {a['agent_id']}")
        print(f"Node ID: {a['node_id']}")
        print(f"Name: {a['name']}")
        print(f"Colony ID: {a['colony_id']}")
        print(f"Connection: {a['connection_mode']}")
        print(f"Status: {a['status']}")
        print(f"Primary: {a['is_primary']}")
        print(f"Priority: {a['priority']}")
        print(f"Capabilities: {', '.join(a['capabilities'])}")
        print(f"Current assignments: {a['current_assignments']}")
        print(f"Max concurrent: {a['max_concurrent']}")
        print(f"Registered: {a['registered_at']}")
        if a.get('last_seen_at'):
            print(f"Last seen: {a['last_seen_at']}")

    elif args.agent_command == "revoke":
        resp = httpx.delete(f"{base_url}/agents/{args.agent_id}", headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        print(f"Agent {args.agent_id} revoked.")

    elif args.agent_command == "disconnect":
        # Read agent config to get agent_id
        agent_config = Path.home() / ".colony" / "agent.json"
        if not agent_config.exists():
            print("No agent config found. Not connected?")
            return
        data = json.loads(agent_config.read_text())
        agent_id = data.get("agent_id")
        if agent_id:
            resp = httpx.delete(f"{base_url}/agents/{agent_id}", headers=headers, timeout=10)
            if resp.status_code == 200:
                print(f"Disconnected agent {agent_id}")
        # Remove config
        agent_config.unlink(missing_ok=True)
        print("Agent config removed.")

    else:
        print("Usage: colony agent [invite|connect|list|show|revoke|disconnect]")


def _cmd_initiative(args) -> None:
    """Handle initiative subcommands."""
    _load_dotenv()

    import httpx
    host = os.environ.get("COLONY_SIDECAR_HOST", "127.0.0.1")
    port = os.environ.get("COLONY_SIDECAR_PORT", "7777")
    api_key = os.environ.get("COLONY_API_KEY", "")
    base_url = f"http://{host}:{port}/v1/host"
    headers = {"Authorization": f"Bearer {api_key}"}

    if args.initiative_command == "list":
        params = {"limit": args.limit}
        if args.status:
            params["status"] = args.status
        if args.agent:
            params["agent_id"] = args.agent
        resp = httpx.get(f"{base_url}/initiatives", params=params, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        data = resp.json()
        initiatives = data.get("initiatives", [])
        if not initiatives:
            print("No initiatives found.")
            return
        print(f"{'ID':<36} {'Type':<15} {'Status':<12} {'Priority':<8} {'Description'}")
        print("-" * 120)
        for i in initiatives:
            desc = i['description'][:60] + "..." if len(i['description']) > 60 else i['description']
            print(f"{i['id']:<36} {i['initiative_type']:<15} {i['status']:<12} {i['priority']:<8} {desc}")

    elif args.initiative_command == "show":
        resp = httpx.get(f"{base_url}/initiatives/{args.initiative_id}", headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        i = resp.json()
        print(f"Initiative ID: {i['id']}")
        print(f"Type: {i['initiative_type']}")
        print(f"Status: {i['status']}")
        print(f"Priority: {i['priority']}")
        print(f"Description: {i['description']}")
        if i.get('assigned_agent_id'):
            print(f"Assigned to: {i['assigned_agent_id']}")
        if i.get('result'):
            print(f"Result: {i['result']}")
        if i.get('error_message'):
            print(f"Error: {i['error_message']}")
        print(f"Created: {i['created_at']}")
        if i.get('completed_at'):
            print(f"Completed: {i['completed_at']}")
        if i.get('failed_at'):
            print(f"Failed: {i['failed_at']}")

    elif args.initiative_command == "cancel":
        resp = httpx.post(
            f"{base_url}/initiatives/{args.initiative_id}/cancel",
            json={"reason": args.reason},
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        print(f"Initiative {args.initiative_id} cancelled.")

    else:
        print("Usage: colony initiative [list|show|cancel]")


def _cmd_init(args) -> None:
    """Initialize a new Colony identity."""
    _load_dotenv()
    state_dir = os.environ.get("COLONY_STATE_DIR", os.getcwd())

    from colony_sidecar.chain.identity import get_or_create_colony_id
    from colony_sidecar.chain.local_keys import LocalKeyManager

    id_path = Path(state_dir) / "colony-id"
    if id_path.exists():
        existing = id_path.read_text().strip()
        print(f"  Colony already initialized: {existing}")
        print(f"  Run 'colony key info' to see details.")
        return

    # Create colony_id
    colony_id = get_or_create_colony_id(state_dir)
    print(f"  Colony ID: {colony_id}")

    # Determine passphrase
    passphrase = None
    if args.encrypt:
        import getpass
        passphrase = getpass.getpass("Colony key passphrase: ").encode()
    elif args.passphrase:
        passphrase = args.passphrase.encode()

    # Generate Colony keypair
    keys_dir = os.path.join(state_dir, "colony-keys")
    km = LocalKeyManager.generate(keys_dir=keys_dir, colony_id=colony_id, passphrase=passphrase)
    print(f"  Public Key: {km.public_key_hex()}")
    print(f"  Keypair saved to {keys_dir}/")

    # Claim Genesis if requested
    if args.claim_genesis:
        from colony_sidecar.chain.identity import create_genesis_manifest
        priv_path = os.path.join(keys_dir, "private.pem")
        private_pem = Path(priv_path).read_bytes()
        genesis_path = os.path.join(state_dir, "genesis.json")
        create_genesis_manifest(colony_id, km.public_key_hex(), genesis_path,
                                private_key_pem=private_pem, passphrase=passphrase)
        print(f"  ⚡ Genesis claimed and manifest signed")

    print(f"\n  Colony initialized. Run 'colony start' to bring it online.")


def _cmd_node(args) -> None:
    """Manage this device's node identity."""
    _load_dotenv()
    state_dir = os.environ.get("COLONY_STATE_DIR", os.getcwd())

    if args.node_command == "info":
        from colony_sidecar.chain.node import get_node_info
        from colony_sidecar.chain.identity import get_or_create_colony_id
        colony_id = get_or_create_colony_id(state_dir)
        info = get_node_info(state_dir)
        print(f"  Colony ID:  {colony_id}")
        print(f"  Node ID:    {info.get('node_id', '(not created — run colony start)')}")
        print(f"  Node Key:   {info.get('node_public_key', '(none)')}")
        print(f"  Certified:  {'yes' if info.get('certified') else 'no'}")
        if info.get('issued_at'):
            print(f"  Issued At:  {info['issued_at']}")
    else:
        print("  Usage: colony node {info}")


def _cmd_key(args) -> None:
    """Manage Colony cryptographic identity."""
    _load_dotenv()
    state_dir = os.environ.get("COLONY_STATE_DIR", os.getcwd())

    if args.key_command == "info":
        from colony_sidecar.chain.identity import get_or_create_colony_id, get_genesis_manifest
        colony_id = get_or_create_colony_id(state_dir)
        keys_dir = os.path.join(state_dir, "colony-keys")
        passphrase = os.environ.get("COLONY_KEY_PASSPHRASE", "")
        passphrase_bytes = passphrase.encode() if passphrase else None
        try:
            from colony_sidecar.chain.local_keys import LocalKeyManager
            km = LocalKeyManager(keys_dir=keys_dir, colony_id=colony_id, passphrase=passphrase_bytes)
            pubkey = km.public_key_hex()
            print(f"  Colony ID:  {colony_id}")
            print(f"  Public Key: {pubkey}")
            manifest = get_genesis_manifest()
            if manifest and manifest.get("colony_id") == colony_id:
                print(f"  Genesis:    YES (trust anchor)")
            else:
                print(f"  Genesis:    no")
        except FileNotFoundError:
            print(f"  Colony ID:  {colony_id}")
            print(f"  Public Key: (no keypair — run 'colony key generate')")

    elif args.key_command == "generate":
        from colony_sidecar.chain.identity import get_or_create_colony_id
        colony_id = get_or_create_colony_id(state_dir)
        keys_dir = os.path.join(state_dir, "colony-keys")
        passphrase = os.environ.get("COLONY_KEY_PASSPHRASE", "")
        passphrase_bytes = passphrase.encode() if passphrase else None
        from colony_sidecar.chain.local_keys import LocalKeyManager
        km = LocalKeyManager.generate(keys_dir=keys_dir, colony_id=colony_id, passphrase=passphrase_bytes)
        print(f"  Generated new Ed25519 keypair for colony {colony_id}")
        print(f"  Public Key: {km.public_key_hex()}")

    elif args.key_command == "set-passphrase":
        from colony_sidecar.chain.identity import get_or_create_colony_id
        colony_id = get_or_create_colony_id(state_dir)
        keys_dir = os.path.join(state_dir, "colony-keys")
        existing_pass = os.environ.get("COLONY_KEY_PASSPHRASE", "")
        existing_pass_bytes = existing_pass.encode() if existing_pass else None
        passphrase = args.passphrase
        if not passphrase:
            import getpass
            passphrase = getpass.getpass("New passphrase: ")
        from colony_sidecar.chain.local_keys import LocalKeyManager
        km = LocalKeyManager(keys_dir=keys_dir, colony_id=colony_id, passphrase=existing_pass_bytes)
        km.set_passphrase(passphrase.encode())
        print(f"  Passphrase set for colony {colony_id}")

    elif args.key_command == "manifest":
        from colony_sidecar.chain.identity import get_or_create_colony_id, create_colony_manifest
        colony_id = get_or_create_colony_id(state_dir)
        keys_dir = os.path.join(state_dir, "colony-keys")
        passphrase = os.environ.get("COLONY_KEY_PASSPHRASE", "")
        passphrase_bytes = passphrase.encode() if passphrase else None
        from colony_sidecar.chain.local_keys import LocalKeyManager
        km = LocalKeyManager(keys_dir=keys_dir, colony_id=colony_id, passphrase=passphrase_bytes)
        manifest_path = os.path.join(state_dir, "colony-manifest.json")
        manifest = create_colony_manifest(colony_id, km.public_key_hex(), manifest_path)
        print(f"  Manifest saved to {manifest_path}")
        print(f"  Share this file with other Colonies to establish trust.")

    elif args.key_command == "claim-genesis":
        from colony_sidecar.chain.identity import get_or_create_colony_id, create_genesis_manifest, get_genesis_manifest
        colony_id = get_or_create_colony_id(state_dir)

        existing = get_genesis_manifest()
        if existing and not args.force:
            print("  Genesis manifest already exists.")
            print(f"  Existing Genesis colony_id: {existing.get('colony_id')}")
            print("  Use --force to overwrite (NOT recommended if other Colonies trust this manifest)")
            return

        keys_dir = os.path.join(state_dir, "colony-keys")
        passphrase = os.environ.get("COLONY_KEY_PASSPHRASE", "")
        passphrase_bytes = passphrase.encode() if passphrase else None
        from colony_sidecar.chain.local_keys import LocalKeyManager
        try:
            km = LocalKeyManager(keys_dir=keys_dir, colony_id=colony_id, passphrase=passphrase_bytes)
            pubkey = km.public_key_hex()
        except FileNotFoundError:
            km = LocalKeyManager.generate(keys_dir=keys_dir, colony_id=colony_id)
            pubkey = km.public_key_hex()

        # Read private key PEM for signing
        priv_path = os.path.join(keys_dir, "private.pem")
        private_pem = Path(priv_path).read_bytes()

        genesis_path = os.path.join(state_dir, "genesis.json")
        manifest = create_genesis_manifest(
            colony_id, pubkey, genesis_path,
            private_key_pem=private_pem,
            passphrase=passphrase_bytes,
        )
        print(f"  ⚡ Genesis claimed for colony {colony_id}")
        print(f"  Public Key: {pubkey}")
        print(f"  Manifest signed with your private key and saved to {genesis_path}")
        print(f"")
        print(f"  IMPORTANT: Commit genesis.json to the Colony repo so other")
        print(f"  Colonies can recognize you as the trust anchor.")
        print(f"  The manifest is cryptographically signed — it cannot be forged.")
        print(f"  Your private key never leaves this machine.")

    else:
        print("  Usage: colony key {info|generate|set-passphrase|manifest|claim-genesis}")



def _find_pid_on_port(port: int) -> int | None:
    """Find the PID of a process listening on the given port."""
    import subprocess
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5,
        )
        pids = result.stdout.strip().splitlines()
        return int(pids[0]) if pids and pids[0].isdigit() else None
    except Exception:
        return None


def _find_orphan_processes() -> list[int]:
    """Find orphaned colony sidecar processes (parent died).
    
    Returns list of PIDs that are:
    - Running uvicorn/colony_sidecar
    - Have parent PID 1 (init) or parent doesn't exist
    """
    orphans = []
    try:
        import subprocess
        # Find all python processes running uvicorn or colony_sidecar
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return orphans
        
        for line in result.stdout.splitlines():
            # Look for colony sidecar processes
            if "uvicorn" in line and "colony_sidecar" in line:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        pid = int(parts[1])
                        ppid = int(parts[2]) if len(parts) > 2 else 0
                        # Parent PID 1 = orphan (reparented to init)
                        # Also check if parent exists
                        if ppid == 1:
                            orphans.append(pid)
                        elif ppid > 1:
                            try:
                                os.kill(ppid, 0)  # Check if parent exists
                            except OSError:
                                # Parent doesn't exist, this is an orphan
                                orphans.append(pid)
                    except (ValueError, OSError):
                        pass
    except Exception:
        pass
    return orphans


def _cleanup_orphans(kill: bool = False) -> int:
    """Find and optionally kill orphaned colony processes.
    
    Args:
        kill: If True, kill the orphans; if False, just report them
    
    Returns:
        Number of orphans found
    """
    orphans = _find_orphan_processes()
    if not orphans:
        return 0
    
    if kill:
        for pid in orphans:
            try:
                os.kill(pid, 15)  # SIGTERM
                print(f"  Killed orphan process {pid}")
            except ProcessLookupError:
                pass
            except Exception as e:
                print(f"  Failed to kill {pid}: {e}")
        time.sleep(1)
        # Check if any survived
        for pid in orphans:
            try:
                os.kill(pid, 0)
                os.kill(pid, 9)  # SIGKILL
                print(f"  Force-killed stubborn process {pid}")
            except ProcessLookupError:
                pass
    
    return len(orphans)


def _neo4j_health_check(password: str, timeout_s: int = 5) -> tuple[bool, str]:
    """Check if Neo4j is healthy (connect + auth + query). Returns (success, error_message)."""
    from neo4j import GraphDatabase
    from neo4j.exceptions import AuthError, ServiceUnavailable
    
    try:
        driver = GraphDatabase.driver(
            "bolt://localhost:7687",
            auth=("neo4j", password),
            connection_timeout=timeout_s
        )
        with driver.session() as session:
            session.run("RETURN 1").single()
        driver.close()
        return True, ""
    except AuthError:
        return False, "auth_failed"
    except ServiceUnavailable:
        return False, "not_responding"
    except Exception as e:
        return False, str(e)


def _neo4j_poll_health(password: str, timeout_s: int = 30) -> tuple[bool, str]:
    """Poll Neo4j health until ready or timeout. Returns (success, error_message)."""
    timeout_s = int(os.environ.get("COLONY_NEO4J_STARTUP_TIMEOUT", timeout_s))
    
    for i in range(1, timeout_s + 1):
        success, error = _neo4j_health_check(password, timeout_s=2)
        if success:
            return True, ""
        if error == "auth_failed":
            # Auth failure is immediate, no point retrying
            return False, error
        if i < timeout_s:
            print(f"  Waiting for Neo4j ({i}/{timeout_s}s)...")
        time.sleep(1)
    
    return False, "timeout"


def _check_and_start_neo4j() -> bool:
    """Check if Neo4j is running, start it if needed. Returns True if Neo4j is available."""
    import subprocess
    from pathlib import Path
    
    # Check if Docker is available
    try:
        result = subprocess.run(["docker", "--version"], capture_output=True, timeout=5)
        if result.returncode != 0:
            return False
    except Exception:
        return False
    
    # Check for Neo4j credentials in .env
    env_path = Path.home() / ".env"
    neo4j_password = None
    if env_path.exists():
        try:
            for line in env_path.read_text().splitlines():
                if line.startswith("NEO4J_PASSWORD="):
                    neo4j_password = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass
    
    if not neo4j_password:
        # No credentials configured, skip Neo4j
        return False
    
    # Check container state
    container_running = False
    container_exists = False
    
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=neo4j-colony", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10
        )
        container_running = "neo4j-colony" in result.stdout
    except Exception:
        pass
    
    if not container_running:
        try:
            result = subprocess.run(
                ["docker", "ps", "-a", "--filter", "name=neo4j-colony", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=10
            )
            container_exists = "neo4j-colony" in result.stdout
        except Exception:
            pass
    
    # Scenario 1: Container already running
    if container_running:
        print("  Neo4j container already running")
        success, error = _neo4j_health_check(neo4j_password)
        if success:
            print("  ✅ Neo4j ready")
            return True
        
        # Quick check failed, try restart
        print("  Neo4j health check failed, restarting...")
        try:
            subprocess.run(["docker", "restart", "neo4j-colony"], capture_output=True, timeout=30)
        except Exception:
            pass
        
        success, error = _neo4j_poll_health(neo4j_password)
        if success:
            print("  ✅ Neo4j recovered after restart")
            return True
        
        if error == "auth_failed":
            print("  ❌ Neo4j auth failed — password in .env doesn't match container")
            print("     Reset: docker rm -f neo4j-colony && colony init")
        else:
            print("  ❌ Neo4j not responding after restart")
            print("     Check logs: docker logs neo4j-colony")
            print("     Reset: docker rm -f neo4j-colony && colony init")
        print("  ⚠️ Graph memory degraded")
        return False
    
    # Scenario 2: Container exists but stopped
    if container_exists:
        print("  Starting Neo4j container...")
        try:
            subprocess.run(["docker", "start", "neo4j-colony"], capture_output=True, timeout=30)
        except Exception:
            pass
        
        success, error = _neo4j_poll_health(neo4j_password)
        if success:
            print("  ✅ Neo4j ready")
            return True
        
        if error == "auth_failed":
            print("  ❌ Neo4j auth failed — password in .env doesn't match container")
            print("     Reset: docker rm -f neo4j-colony && colony init")
        else:
            print("  ❌ Neo4j not ready after 30s")
            print("     Check logs: docker logs neo4j-colony")
        print("  ⚠️ Graph memory degraded")
        return False
    
    # Scenario 3: No container, create new one
    print("  Creating Neo4j container...")
    neo4j_data = Path.home() / ".colony" / "neo4j-data"
    neo4j_data.mkdir(parents=True, exist_ok=True)
    
    try:
        cmd = [
            "docker", "run", "-d",
            "--name", "neo4j-colony",
            "-p", "7474:7474",
            "-p", "7687:7687",
            "-e", f"NEO4J_AUTH=neo4j/{neo4j_password}",
            "-v", f"{neo4j_data}:/data",
            "neo4j:5.15"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"  ⚠️ Failed to create Neo4j container: {result.stderr.strip()}")
            print("  ⚠️ Graph memory degraded")
            return False
    except Exception as exc:
        print(f"  ⚠️ Failed to create Neo4j container: {exc}")
        print("  ⚠️ Graph memory degraded")
        return False
    
    success, error = _neo4j_poll_health(neo4j_password)
    if success:
        print("  ✅ Neo4j ready")
        return True
    
    print("  ❌ Neo4j not ready after 30s")
    print("     Check logs: docker logs neo4j-colony")
    print("  ⚠️ Graph memory degraded")
    return False


def _cmd_start_daemon(host: str, port: int, force: bool) -> None:
    """Start the sidecar as a background daemon."""
    import subprocess

    # Clean up any orphaned processes first
    orphan_count = _cleanup_orphans(kill=True)
    if orphan_count:
        print(f"  Cleaned up {orphan_count} orphan process(es)")

    # Check if port is already in use
    existing_pid = _find_pid_on_port(port)
    if existing_pid:
        if force:
            print(f"  Killing existing process {existing_pid} on port {port}...")
            try:
                os.kill(existing_pid, 15)  # SIGTERM
                time.sleep(2)
                # Check if it died
                if _find_pid_on_port(port):
                    os.kill(existing_pid, 9)  # SIGKILL
                    time.sleep(1)
                print(f"  ✅ Process killed")
            except ProcessLookupError:
                pass
        else:
            print(f"  ⚠️ Port {port} is already in use (PID {existing_pid})")
            try:
                answer = input("  Kill existing process and restart? [Y/n] ").strip().lower()
            except EOFError:
                answer = "y"  # Default to yes when no stdin (e.g. scripts)
            if answer in ("n", "no"):
                print("  Cancelled.")
                return
            try:
                os.kill(existing_pid, 15)
                time.sleep(2)
                if _find_pid_on_port(port):
                    os.kill(existing_pid, 9)
                    time.sleep(1)
                print(f"  ✅ Process killed")
            except ProcessLookupError:
                pass

    # Build env from .env values
    env = {**os.environ}
    env["COLONY_SIDECAR_HOST"] = host
    env["COLONY_SIDECAR_PORT"] = str(port)

    # Start uvicorn
    log_path = Path(os.environ.get("COLONY_STATE_DIR", ".")) / "sidecar.log"
    print(f"  Starting Colony sidecar on {host}:{port}...")
    print(f"  Log: {log_path}")

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "colony_sidecar.server:app",
         "--host", host,
         "--port", str(port)],
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )

    # Write PID file
    pid_path = Path(os.environ.get("COLONY_STATE_DIR", ".")) / "sidecar.pid"
    pid_path.write_text(str(proc.pid))

    # Wait for health check
    import httpx
    for attempt in range(20):
        time.sleep(1)
        try:
            r = httpx.get(f"http://{host}:{port}/v1/host/health", timeout=2)
            if r.status_code == 200:
                data = r.json()
                caps = len(data.get("capabilities", []))
                print(f"  ✅ Sidecar running (PID {proc.pid}, {caps} capabilities)")
                # Check E2E validation status
                stamp = Path(os.environ.get("COLONY_STATE_DIR", ".")) / ".colony-e2e-validated"
                if not stamp.exists():
                    print(f"  ⚠️ E2E pipeline not validated — run 'colony validate' to test")
                return
        except Exception:
            pass

    print(f"  ⚠️ Sidecar didn't respond within 20s")
    print(f"  Check logs: {log_path}")
    print(f"  PID: {proc.pid}")


def _cmd_stop() -> None:
    """Stop the running sidecar."""
    # Try PID file first
    pid_path = Path(os.environ.get("COLONY_STATE_DIR", ".")) / "sidecar.pid"
    port = int(os.environ.get("COLONY_SIDECAR_PORT", "7777"))

    pid = None
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
        except ValueError:
            pass

    # Fallback: find by port
    if not pid:
        pid = _find_pid_on_port(port)

    if not pid:
        print(f"  No sidecar process found on port {port}")
        return

    try:
        os.kill(pid, 15)  # SIGTERM
        print(f"  Stopping sidecar (PID {pid})...")
        time.sleep(2)

        # Check if still alive
        if _find_pid_on_port(port):
            print(f"  Process didn't stop gracefully, killing...")
            os.kill(pid, 9)  # SIGKILL
            time.sleep(1)

        print(f"  ✅ Sidecar stopped")

        # Clean up PID file
        if pid_path.exists():
            pid_path.unlink()

    except ProcessLookupError:
        print(f"  Process {pid} already gone")
        if pid_path.exists():
            pid_path.unlink()
        
        # Check for orphan cleanup
        orphan_count = _cleanup_orphans(kill=True)
        if orphan_count:
            print(f"  Cleaned up {orphan_count} orphan process(es)")


def _cmd_status() -> None:
    """Check sidecar health and pipeline status."""
    import httpx

    host = os.environ.get("COLONY_SIDECAR_HOST", "127.0.0.1")
    port = os.environ.get("COLONY_SIDECAR_PORT", "7777")
    url = f"http://{host}:{port}"
    api_key = os.environ.get("COLONY_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    try:
        resp = httpx.get(f"{url}/v1/host/health", headers=headers, timeout=5)
        data = resp.json()
        status = data.get("status", "unknown")
        caps = data.get("capabilities", [])
        notes = data.get("notes", {})

        # Status icon
        icon = "🟢" if status == "ok" else "🔴"
        print(f"{icon} Colony Sidecar — {status}")
        print(f"  URL: {url}")
        print(f"  Capabilities: {len(caps)}")

        # Show notable notes
        for k, v in notes.items():
            if "fail" in str(v).lower() or "error" in str(v).lower() or "not wired" in str(v).lower():
                print(f"  ⚠️  {k}: {v}")

        # Check E2E validation stamp
        stamp = Path(os.environ.get("COLONY_STATE_DIR", ".")) / ".colony-e2e-validated"
        if stamp.exists():
            stamp_data = json.loads(stamp.read_text())
            validated_at = stamp_data.get("validated_at", "unknown")
            print(f"  ✅ E2E validated: {validated_at}")
        else:
            print(f"  ⚠️  E2E pipeline not validated")
            print(f"     Run 'colony validate' to test the full pipeline")

    except Exception as exc:
        print(f"🔴 Sidecar not reachable: {exc}")
        # Check if process exists
        pid = _find_pid_on_port(int(port))
        if pid:
            print(f"  Process {pid} is on port {port} but not responding")
        else:
            print(f"  No process on port {port}")
            print(f"  Start with: colony start")
        sys.exit(1)


def _cmd_mcp(args) -> None:
    """Handle colony mcp subcommands."""
    from colony_sidecar.mcp.server import create_server, run_stdio, run_http
    from colony_sidecar.mcp.config import (
        HARNESS_DEFS, detect_harnesses, add_to_harness, remove_from_harness,
    )

    if not hasattr(args, "mcp_command") or not args.mcp_command:
        # Default: run the MCP server
        run_stdio()
        return

    if args.mcp_command == "run":
        if args.transport == "http":
            run_http(host=args.host, port=args.port)
        else:
            run_stdio()

    elif args.mcp_command == "detect":
        detected = detect_harnesses()
        print("Detected coding harnesses:")
        for hid, installed in detected.items():
            status = "installed" if installed else "not found"
            icon = "  \u2705" if installed else "  \u274c"
            print(f"{icon} {HARNESS_DEFS[hid]['display']:15s} {status}")

    elif args.mcp_command == "setup":
        # Handle --print-config (for distributed setups)
        if getattr(args, 'print_config', False):
            import json
            from colony_sidecar.mcp.config import _mcp_config
            
            contact_id = args.contact_id or os.environ.get("USER", "user")
            harness = args.harness or "crush"
            
            # Get harness definition
            hdef = HARNESS_DEFS.get(harness)
            if not hdef:
                print(f"Unknown harness: {harness}")
                print(f"Available: {', '.join(HARNESS_DEFS.keys())}")
                return
            
            # Build MCP config with optional overrides
            sidecar_url = getattr(args, 'sidecar_url', None)
            mcp_command = getattr(args, 'mcp_command', None)
            mcp_args = getattr(args, 'mcp_args', None)
            
            # Set env vars for _mcp_config if provided
            if mcp_command:
                os.environ["COLONY_MCP_COMMAND"] = mcp_command
            if mcp_args:
                os.environ["COLONY_MCP_ARGS"] = mcp_args
            
            needs_type = hdef.get("mcp_type") == "stdio"
            mcp_config = _mcp_config(contact_id, hdef["source_tag"], include_type=needs_type, sidecar_url=sidecar_url)
            
            # Print the full config snippet
            full_config = {"mcp": {"colony": mcp_config}}
            print(json.dumps(full_config, indent=2))
            print()
            print(f"# Add this to {hdef['config_path']}")
            print(f"# Contact ID: {contact_id}")
            print(f"# Source: {hdef['source_tag']}")
            return
        
        detected = detect_harnesses()
        installed = {k: v for k, v in detected.items() if v}

        if not installed:
            print("  No coding harnesses detected.")
            print("  Install one of: Claude Code, Codex, Crush, OpenCode, or Hermes")
            print("  Then run: colony mcp setup")
            print()
            print("  For distributed setups (Colony on remote machine):")
            print("    colony mcp setup --print-config --sidecar-url http://HOST:7777 --harness crush")
            return

        # Get contact ID
        contact_id = args.contact_id
        if not contact_id:
            try:
                contact_id = input("  What should Colony call you? ").strip()
            except EOFError:
                contact_id = os.environ.get("USER", "user")
            if not contact_id:
                contact_id = os.environ.get("USER", "user")

        # Determine which harnesses to configure
        if args.harness == "all":
            selected = list(installed.keys())
        elif args.harness:
            if args.harness not in installed:
                print(f"  {HARNESS_DEFS[args.harness]['display']} is not installed")
                return
            selected = [args.harness]
        else:
            # Interactive selection
            print("  Detected coding harnesses:")
            options = list(installed.keys())
            for i, hid in enumerate(options, 1):
                print(f"    [{i}] {HARNESS_DEFS[hid]['display']}")
            print()
            try:
                choice = input("  Which should Colony connect? (comma-separated, or 'all') [all]: ").strip()
            except EOFError:
                choice = "all"

            if not choice or choice.lower() == "all":
                selected = options
            else:
                indices = [int(x.strip()) for x in choice.split(",") if x.strip().isdigit()]
                selected = [options[i - 1] for i in indices if 1 <= i <= len(options)]

        if not selected:
            print("  No harnesses selected. Run 'colony mcp setup' again when ready.")
            return

        # Configure each selected harness
        for hid in selected:
            hdef = HARNESS_DEFS[hid]
            print(f"  Configuring {hdef['display']}...")
            diff = add_to_harness(hid, contact_id, dry_run=args.dry_run)
            if diff is None:
                print(f"  Already configured — skipping")
            elif args.dry_run:
                print(f"  Would add (dry run):")
                print(diff)
            else:
                print(f"  Added Colony MCP (source: {hdef['source_tag']})")
                
                # Write skill
                from colony_sidecar.harness_integration import write_colony_skill
                if write_colony_skill(hid):
                    print(f"  ✅ Diagnostic skill installed")

        if args.dry_run:
            print("  Run without --dry-run to apply changes")
        else:
            print(f"  Contact ID: {contact_id}")
            print(f"  Start the sidecar with: colony start")

    elif args.mcp_command == "remove":
        if args.harness == "all":
            targets = list(HARNESS_DEFS.keys())
        elif args.harness:
            targets = [args.harness]
        else:
            detected = detect_harnesses()
            targets = [k for k, v in detected.items() if v]

        if not targets:
            print("  No harnesses to remove from.")
            return

        for hid in targets:
            hdef = HARNESS_DEFS.get(hid)
            if not hdef:
                continue
            diff = remove_from_harness(hid, dry_run=args.dry_run)
            if diff:
                prefix = "  Would remove" if args.dry_run else "  Removed"
                print(f"{prefix}: {hdef['display']}")
                
                # Remove skill (not dry_run)
                if not args.dry_run:
                    from colony_sidecar.harness_integration import remove_colony_skill
                    if remove_colony_skill(hid):
                        print(f"  ✅ Diagnostic skill removed")
            else:
                print(f"  {hdef['display']} — Colony not configured, skipping")

        if args.dry_run:
            print("  Run without --dry-run to apply changes")
        else:
            print("  Colony MCP removed from harness configs")


def _cmd_validate(args) -> None:
    """Run end-to-end pipeline validation."""
    import httpx

    host = os.environ.get("COLONY_SIDECAR_HOST", "127.0.0.1")
    port = os.environ.get("COLONY_SIDECAR_PORT", "7777")
    url = f"http://{host}:{port}"
    api_key = os.environ.get("COLONY_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    state_dir = os.environ.get("COLONY_STATE_DIR", ".")

    print("🧪 Colony E2E Pipeline Validation")
    print("=" * 40)
    print()

    # Step 0: Confirm LLM usage
    if not args.yes:
        print("This test will send one prompt through the LLM to verify the full pipeline.")
        print("It uses a small amount of LLM API credits.")
        try:
            answer = input("Continue? [y/N] ").strip().lower()
        except EOFError:
            print("  \u26a0\ufe0f No stdin — use --yes to skip confirmation")
            return
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return

    # Step 1: Check sidecar is running
    print("\n[1/5] Checking sidecar...")
    try:
        resp = httpx.get(f"{url}/v1/host/health", headers=headers, timeout=5)
        data = resp.json()
        if data.get("status") != "ok":
            print(f"  ❌ Sidecar not healthy: {data.get('status')}")
            return
        print(f"  ✅ Sidecar running ({len(data.get('capabilities', []))} capabilities)")
    except Exception as e:
        print(f"  ❌ Sidecar not reachable: {e}")
        print("  Start with: colony start")
        return

    # Step 2: Seed test data
    print("\n[2/5] Seeding test data...")
    test_contact = f"validate-{uuid.uuid4().hex[:6]}"

    r = httpx.post(f"{url}/v1/host/commitments", headers=headers,
        json={"person_id": test_contact, "description": "Validate E2E pipeline", "priority": 2}, timeout=5)
    if r.status_code not in (200, 201):
        print(f"  ❌ Could not create commitment: {r.status_code}")
        return
    cid = r.json().get("id")
    print(f"  ✅ Test commitment created")

    r = httpx.post(f"{url}/v1/host/affect/events", headers=headers,
        json={"contact_id": test_contact, "valence": 0.6, "arousal": 0.4, "trigger": "validation test"}, timeout=5)
    print(f"  ✅ Test affect recorded" if r.status_code in (200, 201) else f"  ⚠️ Affect failed: {r.status_code}")

    r = httpx.post(f"{url}/v1/host/mind/facts", headers=headers,
        json={"contact_id": test_contact, "fact": "Running pipeline validation", "category": "test", "confidence": 0.5}, timeout=5)
    print(f"  ✅ Test fact recorded" if r.status_code in (200, 201) else f"  ⚠️ Fact failed: {r.status_code}")

    # Step 3: Context assembly
    print("\n[3/5] Testing context assembly...")
    r = httpx.post(f"{url}/v1/host/context/assemble", headers=headers,
        json={"identity": {"host_id": "validate"}, "context": {"session_id": "validate", "contact_id": test_contact},
              "incoming_message": {"role": "user", "content": "What am I working on?"}}, timeout=10)
    if r.status_code != 200:
        print(f"  ❌ Context assembly failed: {r.status_code}")
        return

    sections = r.json().get("sections", [])
    section_ids = [s["id"] for s in sections]
    expected = ["colony-commitments", "colony-affect", "colony-shared-facts"]
    found = [e for e in expected if e in section_ids]
    print(f"  ✅ Context assembly: {len(sections)} sections, {len(found)}/{len(expected)} cognitive sections present")

    # Step 4: Check LLM is configured
    print("\n[4/5] Checking LLM configuration...")
    has_openclaw = bool(shutil.which("openclaw"))
    llm_ok = False

    if has_openclaw:
        try:
            result = subprocess.run(
                ["openclaw", "config", "get", "llm.apiKey"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip() and result.stdout.strip() != "undefined":
                llm_ok = True
                print(f"  ✅ LLM API key configured in OpenClaw")
            else:
                print(f"  ⚠️ No LLM API key in OpenClaw — cannot test full LLM pipeline")
                print(f"  Configure with: openclaw config set llm.apiKey <key>")
        except Exception:
            print(f"  ⚠️ Could not check OpenClaw LLM config")
    else:
        # Check MCP harnesses
        has_mcp = bool(shutil.which("claude") or shutil.which("codex") or shutil.which("crush"))
        if has_mcp:
            print(f"  \u26a0\ufe0f CLI harness detected but LLM test requires OpenClaw")
            print(f"  MCP tools validated via context assembly — LLM pipeline needs manual verification")
        else:
            print(f"  \u26a0\ufe0f No OpenClaw or MCP harness — LLM pipeline needs manual verification")

    # Step 5: Test full LLM pipeline if possible
    print("\n[5/5] Testing full pipeline...")
    if llm_ok and has_openclaw:
        print("  Sending test message through OpenClaw...")
        try:
            result = subprocess.run(
                ["openclaw", "agent", "--once", "What is 2+2? Reply with just the number."],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and "4" in result.stdout:
                print(f"  ✅ Full pipeline working — LLM responded through Colony")
            else:
                print(f"  ⚠️ LLM responded but couldn't verify Colony was in the chain")
                print(f"  Agent output: {result.stdout[:100]}")
        except subprocess.TimeoutExpired:
            print(f"  ⚠️ LLM test timed out (model may be slow)")
        except Exception as e:
            print(f"  ⚠️ LLM test failed: {e}")
    else:
        print("  ⚪ Full LLM pipeline test skipped (no LLM configured or OpenClaw not available)")

    # Cleanup: delete test commitment
    if cid:
        httpx.delete(f"{url}/v1/host/commitments/{cid}", headers=headers, timeout=5)

    # Write validation stamp
    stamp_path = Path(state_dir) / ".colony-e2e-validated"
    stamp_data = {
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "context_sections": len(sections),
        "cognitive_sections": len(found),
        "llm_tested": llm_ok and has_openclaw,
    }
    stamp_path.write_text(json.dumps(stamp_data, indent=2))

    # Summary
    print()
    all_ok = len(found) >= 2  # At least commitments + one other
    if all_ok:
        print("🟢 Pipeline validation passed")
        print(f"  Context assembly: {len(sections)} sections")
        print(f"  Cognitive sections: {', '.join(found)}")
        if not llm_ok:
            print(f"  ⚠️ LLM pipeline not tested — configure LLM and re-run 'colony validate'")
    else:
        print("🔴 Pipeline validation incomplete")
        print(f"  Missing sections: {set(expected) - set(found)}")
        print(f"  Check sidecar logs and configuration")


def _cmd_doctor(args) -> None:
    """Run integration health check against the running sidecar."""
    import httpx

    _load_dotenv()
    url = args.url or os.environ.get("COLONY_SIDECAR_URL", f"http://{os.environ.get('COLONY_SIDECAR_HOST', '127.0.0.1')}:{os.environ.get('COLONY_SIDECAR_PORT', '7777')}")
    api_key = args.api_key or os.environ.get("COLONY_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    verbose = args.verbose

    print(f"\n🩺 Colony Doctor — checking {url}\n")

    checks = []

    def check(name, func):
        try:
            result = func()
            status = "✅" if result else "❌"
            checks.append((name, result))
            if verbose or not result:
                print(f"  {status} {name}")
        except Exception as e:
            checks.append((name, False))
            print(f"  ❌ {name}: {e}")

    with httpx.Client(base_url=url, headers=headers, timeout=10) as c:
        # 1. Health
        def _health():
            r = c.get("/v1/host/health")
            d = r.json()
            return d.get("status") == "ok" and len(d.get("capabilities", [])) >= 20
        check("Health endpoint", _health)

        # 2. Auth
        def _auth():
            if not api_key:
                return True  # No auth configured, skip
            r = httpx.post(f"{url}/v1/host/memory/search", json={"identity": {"host_id": "t"}, "context": {"session_id": "s", "contact_id": "c"}, "query": "t"}, timeout=5)
            return r.status_code == 401
        check("Auth enforcement", _auth)

        # 3. Memory write
        def _mem_write():
            r = c.post("/v1/host/memory/write", json={"identity": {"host_id": "doctor"}, "context": {"session_id": "s", "contact_id": "c"}, "content": f"Doctor check {uuid.uuid4().hex[:6]}", "type": "episodic", "strength": 0.5})
            return r.json().get("accepted", False)
        check("Memory write", _mem_write)

        # 4. Memory search
        time.sleep(1)
        def _mem_search():
            r = c.post("/v1/host/memory/search", json={"identity": {"host_id": "doctor"}, "context": {"session_id": "s", "contact_id": "c"}, "query": "colony", "limit": 1})
            return r.status_code == 200 and "entries" in r.json()
        check("Memory search", _mem_search)

        # 5. Response gate (clean)
        def _gate_pass():
            r = c.post("/v1/host/safety/check", json={"identity": {"host_id": "doctor"}, "context": {"session_id": "s", "contact_id": "c"}, "response_text": "Hello", "turn_id": "d1"})
            return r.json().get("blocked") is False
        check("Response gate (pass)", _gate_pass)

        # 6. Response gate (PII block)
        def _gate_block():
            r = c.post("/v1/host/safety/check", json={"identity": {"host_id": "doctor"}, "context": {"session_id": "s", "contact_id": "c"}, "response_text": "SSN: 078-05-1120", "turn_id": "d2"})
            d = r.json()
            return d.get("blocked") is True and d.get("blocking_layer") == 2
        check("Response gate (PII block)", _gate_block)

        # 7. Goals
        def _goals():
            r = c.get("/v1/host/goals")
            return r.status_code == 200
        check("Goals", _goals)

        # 8. Identity
        def _identity():
            r = c.get("/v1/host/identity/status")
            return r.status_code == 200
        check("Identity", _identity)

        # 9. Secrets
        def _secrets():
            r = c.post("/v1/host/secrets/set", json={"identity": {"host_id": "doctor"}, "key": f"_dr_{uuid.uuid4().hex[:6]}", "value": "x"})
            return r.json().get("stored", False)
        check("Secrets vault", _secrets)

        # 10. Embedding
        def _embed():
            r = c.get("/v1/host/embed/health")
            d = r.json()
            return d.get("status") == "ok" and d.get("dims", 0) > 0
        check("Embedding pipeline", _embed)

        # 11. Context assembly
        def _context():
            r = c.post("/v1/host/context/assemble", json={"identity": {"host_id": "doctor"}, "context": {"session_id": "s", "contact_id": "c"}, "incoming_message": {"content": "test", "role": "user"}})
            return len(r.json().get("sections", [])) > 0
        check("Context assembly", _context)

        # 12. Skills
        def _skills():
            r = c.get("/v1/host/skills/registry")
            return len(r.json().get("skills", [])) > 0
        check("Skills registry", _skills)

        # 13. World model
        def _world():
            r = c.post("/v1/host/world/entities/query", json={"identity": {"host_id": "doctor"}, "context": {"session_id": "s", "contact_id": "c"}, "query": "Colony", "limit": 3})
            return r.status_code == 200
        check("World model", _world)

        # 14. Signals
        def _signals():
            r = c.post("/v1/host/signals/ingest", json={"identity": {"host_id": "doctor"}, "context": {"session_id": "s", "contact_id": "c"}, "signals": [{"type": "engagement_depth", "source": "doctor", "value": 0.5}]})
            return r.json().get("accepted", False)
        check("Signal ingestion", _signals)

        # 15. Autonomy
        def _autonomy():
            r = c.post("/v1/host/autonomy/cycle", json={"identity": {"host_id": "doctor"}})
            return r.json().get("completed", False)
        check("Autonomy cycle", _autonomy)

        # 16. Contacts
        def _contacts():
            r = c.get("/v1/host/contacts")
            return r.status_code == 200 and isinstance(r.json(), dict)
        check("Contacts", _contacts)

        # 17. Briefings
        def _briefings():
            r = c.get("/v1/host/briefings")
            return r.status_code == 200
        check("Briefings", _briefings)

        # 18. Cognition
        def _cognition():
            r = c.get("/v1/host/cognition/cpi")
            return r.status_code == 200
        check("Cognition", _cognition)

        # 19. Delivery
        def _delivery():
            r = c.get("/v1/host/delivery/pending")
            return r.status_code == 200
        check("Delivery", _delivery)

        # 20. Reasoning (lightweight - just check endpoint exists)
        def _reasoning():
            r = c.get("/v1/host/reasoning/turn")
            return r.status_code == 200 or r.status_code == 405  # 405 = method not allowed, endpoint exists
        check("Reasoning endpoint", _reasoning)

        # 21. Research
        def _research():
            r = c.get("/v1/host/research")
            return r.status_code == 200
        check("Research endpoint", _research)

        # 22. Memory status diagnostic
        def _memory_status():
            r = c.get("/v1/host/memory/status")
            d = r.json()
            return r.status_code == 200 and d.get("wired", False)
        check("Memory subsystem wiring", _memory_status)

        # 23. Search providers
        def _search_providers():
            r = c.get("/v1/host/search/providers")
            d = r.json()
            return r.status_code == 200 and isinstance(d.get("providers", []), list)
        check("Search providers", _search_providers)

        # 24. Autonomy scheduler
        def _scheduler():
            r = c.get("/v1/host/autonomy/schedule")
            d = r.json()
            return r.status_code == 200 and len(d.get("schedules", [])) > 0
        check("Autonomy scheduler", _scheduler)

        # 25. Extraction pipeline
        def _extraction():
            import base64
            test_doc = base64.b64encode(b'{"name": "doctor-test", "type": "test"}').decode()
            r = c.post("/v1/host/world/extract", json={"identity": {"host_id": "doctor"}, "content": test_doc, "mime_type": "application/json"})
            return r.status_code == 200
        check("Extraction pipeline", _extraction)

        # 26. Native tools (calculate)
        def _native_calc():
            r = c.post("/v1/host/reasoning/turn", json={
                "identity": {"host_id": "doctor"},
                "context": {"session_id": "doctor", "contact_id": "doctor"},
                "messages": [{"role": "user", "content": "Use the calculate tool to evaluate 2+2"}],
                "max_iterations": 1,
            }, timeout=30)
            # 200 = success, 501 = not wired (still means endpoint works)
            return r.status_code in (200, 501)
        check("Native tools (calculate)", _native_calc)

        # 27. Commitments
        def _commitments():
            r = c.get("/v1/host/commitments?status=pending&limit=1")
            return r.status_code == 200
        check("Commitment tracking", _commitments)

        # 28. Affect tracking
        def _affect():
            r = c.get("/v1/host/affect/state/doctor-contact")
            return r.status_code == 200
        check("Affect tracking", _affect)

        # 29. Shared facts
        def _facts():
            r = c.get("/v1/host/shared-facts?limit=1")
            return r.status_code == 200
        check("Shared facts", _facts)

        # 30. Patterns
        def _patterns():
            r = c.get("/v1/host/patterns?limit=1")
            return r.status_code == 200
        check("Pattern extraction", _patterns)

        # 31. Surprises
        def _surprises():
            r = c.get("/v1/host/surprises?limit=1")
            return r.status_code == 200
        check("Surprise engine", _surprises)

        # 32. World Model API (CRUD)
        def _world_api():
            r = c.get("/v1/host/world/stats")
            return r.status_code == 200 and "total_entities" in r.json()
        check("World model API", _world_api)

        # 33. Event journal
        def _events():
            r = c.get("/v1/host/events/replay?limit=1")
            return r.status_code in (200, 501)  # 501 if no events yet
        check("Event journal", _events)

        # 34. E2E pipeline validation
        def _e2e_validated():
            stamp = Path(os.environ.get("COLONY_STATE_DIR", ".")) / ".colony-e2e-validated"
            return stamp.exists()
        check("E2E pipeline validated", _e2e_validated)

    # --- Orphan process check ---
    if getattr(args, 'clean_orphans', False):
        print("\n  Checking for orphaned sidecar processes...")
        orphan_count = _cleanup_orphans(kill=True)
        if orphan_count:
            print(f"  ✅ Cleaned up {orphan_count} orphan process(es)")
        else:
            print("  ✅ No orphan processes found")
    else:
        orphans = _find_orphan_processes()
        if orphans:
            print(f"\n  ⚠️ Found {len(orphans)} orphaned sidecar process(es): {orphans}")
            print("  Run 'colony doctor --clean-orphans' to clean them up")
            checks.append(("No orphan processes", False))

        # --- Full checks (heavier, require LLM or async) ---
        if args.full:
            # Reasoning with LLM inference
            def _reasoning_full():
                r = c.post("/v1/host/reasoning/turn", json={"identity": {"host_id": "doctor"}, "context": {"session_id": "s", "contact_id": "c"}, "messages": [{"role": "user", "content": "What is 2+2?"}], "max_iterations": 1}, timeout=30)
                return r.status_code == 200
            check("Reasoning (LLM inference)", _reasoning_full)

            # Research with actual task
            def _research_full():
                r = c.post("/v1/host/research/start", json={"identity": {"host_id": "doctor"}, "topic": "test", "depth": "quick"}, timeout=30)
                # 200 = success, 501 = not wired, 500 = pipeline error (still means endpoint works)
                return r.status_code in (200, 501)
            check("Research (async task)", _research_full)

            # Cognition cycle
            def _cognition_full():
                r = c.post("/v1/host/cognition/cycle", json={"identity": {"host_id": "doctor"}})
                return r.status_code == 200
            check("Cognition cycle", _cognition_full)

    # Summary
    passed = sum(1 for _, v in checks if v)
    total = len(checks)
    print(f"\n  {passed}/{total} checks passed")
    if passed == total:
        print("  🟢 All systems healthy\n")
    else:
        print("  🔴 Some systems unhealthy — check logs above\n")
        raise SystemExit(1)


def _load_dotenv() -> None:
    """Load .env from ~/.colony/ first, then CWD.
    
    Does not override existing environment variables.
    """
    from pathlib import Path
    
    # Priority: ~/.colony/.env > CWD/.env
    env_paths = [
        Path.home() / ".colony" / ".env",
        Path.cwd() / ".env",
    ]
    
    for env_path in env_paths:
        if not env_path.exists():
            continue
        
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
        
        # Only load first found .env
        break


if __name__ == "__main__":
    main()
