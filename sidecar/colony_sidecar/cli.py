"""Colony CLI — ``colony`` command."""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="colony",
        description="Colony intelligence sidecar server",
    )
    sub = parser.add_subparsers(dest="command")

    # --- init ---
    init_p = sub.add_parser("init", help="Interactive setup wizard")
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
    mm_p.add_argument("--safety", default="basic", choices=["off", "basic", "strict"], help="Image safety level")
    mm_p.add_argument("--skip-download", action="store_true", help="Skip model download")

    args = parser.parse_args()

    if args.command == "init":
        from colony_sidecar.setup import run_init
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

    elif command == "doctor":
        _cmd_doctor(args)

    else:
        parser.print_help()


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
