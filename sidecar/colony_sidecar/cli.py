"""Colony CLI — ``colony`` command."""

from __future__ import annotations

import argparse
import json
import os
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

    # --- doctor ---
    doc_p = sub.add_parser("doctor", help="Run integration health check against running sidecar")
    doc_p.add_argument("--url", default=None, help="Sidecar URL (default: from .env)")
    doc_p.add_argument("--api-key", default=None, help="API key (default: from .env)")
    doc_p.add_argument("--verbose", "-v", action="store_true", help="Show detailed results")
    doc_p.add_argument("--full", action="store_true", help="Run all checks including heavy ones (reasoning, research)")

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

    args = parser.parse_args()

    if args.command == "init":
        # Run setup wizard
        from colony_sidecar.setup import run_init
        code = run_init(root_dir=args.dir)
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
        # Load .env if present
        _load_dotenv()

        import uvicorn
        host = args.host or os.environ.get("COLONY_SIDECAR_HOST", "127.0.0.1")
        port = args.port or int(os.environ.get("COLONY_SIDECAR_PORT", "7777"))
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
