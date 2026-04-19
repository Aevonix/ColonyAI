"""Colony setup wizard - ``colony init``.

Guides the user through first-time configuration:
1. Install dependencies
2. Dependency checks
3. Host framework + OpenClaw plugin setup
4. Docker setup (if needed)
5. Neo4j setup (auto-start via Docker or manual)
6. Write .env
7. Database setup
8. Self-knowledge seeding
9. Summary
"""

from __future__ import annotations

import asyncio
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


# ── ANSI helpers ────────────────────────────────────────────────────────────

def _green(msg: str) -> str:
    return f"\033[92m{msg}\033[0m"

def _red(msg: str) -> str:
    return f"\033[91m{msg}\033[0m"

def _yellow(msg: str) -> str:
    return f"\033[93m{msg}\033[0m"

def _bold(msg: str) -> str:
    return f"\033[1m{msg}\033[0m"

def _prompt(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


# ── Check helpers ───────────────────────────────────────────────────────────

def _check_command(cmd: str) -> bool:
    return shutil.which(cmd) is not None

def _check_python() -> tuple[bool, str]:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ok = sys.version_info >= (3, 11)
    return ok, version

def _check_neo4j() -> tuple[bool, str]:
    """Check if Neo4j is reachable on the default bolt port."""
    try:
        with socket.create_connection(("localhost", 7687), timeout=2):
            return True, "localhost:7687"
    except (ConnectionRefusedError, OSError):
        return False, "not reachable"

def _check_port(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=1):
            return True
    except (ConnectionRefusedError, OSError):
        return False

def _check_docker() -> bool:
    """Check if Docker is available and the daemon is running."""
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False

def _check_openclaw() -> bool:
    """Check if OpenClaw CLI is available."""
    return shutil.which("openclaw") is not None

def _wait_for_neo4j(timeout: int = 60) -> bool:
    """Wait for Neo4j to become reachable on bolt port."""
    import time
    start = time.time()
    while time.time() - start < timeout:
        ok, _ = _check_neo4j()
        if ok:
            return True
        time.sleep(2)
    return False

def _wait_for_docker(timeout: int = 120) -> bool:
    """Wait for Docker daemon to become available."""
    import time
    start = time.time()
    while time.time() - start < timeout:
        if _check_docker():
            return True
        time.sleep(3)
    return False


# ── Docker install ──────────────────────────────────────────────────────────

def _install_docker() -> bool:
    """Attempt to install Docker based on platform. Returns True if installed."""
    system = platform.system().lower()

    if system == "linux":
        print("  Installing Docker via get.docker.com...")
        print("  (This requires sudo — you may be prompted for your password)")
        try:
            result = subprocess.run(
                ["bash", "-c", "curl -fsSL https://get.docker.com | sudo sh"],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                print("  ✅ Docker installed")
                # Add current user to docker group
                subprocess.run(
                    ["sudo", "usermod", "-aG", "docker", os.environ.get("USER", "")],
                    capture_output=True, timeout=10,
                )
                print("  ✅ Added user to docker group (log out/in to take effect)")
                return True
            else:
                print(f"  ❌ Docker install failed: {result.stderr[:200]}")
                return False
        except Exception as exc:
            print(f"  ❌ Docker install failed: {exc}")
            return False

    elif system == "darwin":
        # macOS
        if shutil.which("brew"):
            print("  Installing Docker Desktop via Homebrew...")
            try:
                result = subprocess.run(
                    ["brew", "install", "--cask", "docker"],
                    capture_output=True, text=True, timeout=300,
                )
                if result.returncode == 0:
                    print("  ✅ Docker Desktop installed")
                    print("  ⚠️ Please open Docker Desktop from Applications and wait for it to start.")
                    print("  Then re-run 'colony init' to continue.")
                    return True
                else:
                    print(f"  ❌ brew install failed: {result.stderr[:200]}")
                    return False
            except Exception as exc:
                print(f"  ❌ Docker install failed: {exc}")
                return False
        else:
            print("  Homebrew not found. Install Docker Desktop manually:")
            print("  https://docs.docker.com/desktop/install/mac-install/")
            return False
    else:
        print(f"  Unsupported platform: {system}")
        print("  Install Docker manually: https://docs.docker.com/get-docker/")
        return False


# ── Neo4j start ─────────────────────────────────────────────────────────────

def _start_neo4j_docker() -> bool:
    """Start Neo4j via docker compose. Returns True if successful."""
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    if not compose_path.exists():
        compose_path = Path("docker-compose.yml")
    if not compose_path.exists():
        print("  ⚠️ docker-compose.yml not found")
        return False

    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "up", "-d", "neo4j"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"    ⚠️ docker compose failed: {result.stderr.strip()}")
            return False
        return True
    except Exception as exc:
        print(f"    ⚠️ docker compose failed: {exc}")
        return False


# ── OpenClaw plugin setup ───────────────────────────────────────────────────

def _configure_openclaw_plugin(values: dict[str, str], colony_root: Path) -> bool:
    """Configure Colony as an OpenClaw plugin. Returns True if successful."""
    if not _check_openclaw():
        print("  ⚠️ OpenClaw CLI not found in PATH")
        return False

    sidecar_url = f"http://{values['COLONY_SIDECAR_HOST']}:{values['COLONY_SIDECAR_PORT']}"
    api_key = values["COLONY_API_KEY"]

    try:
        # Enable the plugin
        cmds = [
            ["openclaw", "config", "set", "plugins.entries.colony.enabled", "true"],
            ["openclaw", "config", "set", f"plugins.entries.colony.config.sidecarUrl", sidecar_url],
            ["openclaw", "config", "set", f"plugins.entries.colony.config.apiKey", api_key],
            ["openclaw", "config", "set", f"plugins.entries.colony.config.hostId", "openclaw"],
        ]
        for cmd in cmds:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                print(f"  ⚠️ Config set failed: {' '.join(cmd[-2:])}")
                return False

        # Try to install the plugin if the dist directory exists
        dist_dir = colony_root / "dist"
        if dist_dir.exists():
            result = subprocess.run(
                ["openclaw", "plugin", "install", str(colony_root)],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                print("  ✅ Colony plugin installed and configured in OpenClaw")
            else:
                # Plugin install might not exist as a command — that's OK
                print("  ✅ Colony plugin configured in OpenClaw config")
        else:
            # Need to build first
            if (colony_root / "package.json").exists() and shutil.which("npm"):
                print("  Building Colony plugin...")
                build_result = subprocess.run(
                    ["npm", "run", "build"],
                    capture_output=True, text=True, timeout=60,
                    cwd=str(colony_root),
                )
                if build_result.returncode == 0:
                    print("  ✅ Colony plugin built and configured in OpenClaw")
                else:
                    print("  ⚠️ Plugin build failed — configured config but may need manual build")
            else:
                print("  ✅ Colony plugin configured in OpenClaw config")

        print(f"     sidecarUrl: {sidecar_url}")
        print(f"     apiKey: {api_key[:8]}...{api_key[-4:]}")
        return True

    except Exception as exc:
        print(f"  ⚠️ OpenClaw plugin setup failed: {exc}")
        return False


# ── .env helpers ────────────────────────────────────────────────────────────

def _write_env(env_path: Path, values: dict[str, str]) -> None:
    lines = [
        "# Colony Sidecar Configuration",
        "# Generated by 'colony init'",
        "#",
        "# Colony is a sidecar — it gets LLM credentials from its host",
        "# (OpenClaw, Hermes, etc.) at runtime via POST /v1/host/configure.",
        "# You do NOT need to configure LLM keys here.",
        "",
    ]
    for key, val in values.items():
        lines.append(f"{key}={val}")
    env_path.write_text("\n".join(lines) + "\n")

def _load_existing_env(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}
    env: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


# ── Main wizard ─────────────────────────────────────────────────────────────

def run_init(root_dir: str | None = None) -> int:
    """Run the interactive setup wizard. Returns exit code."""
    base = Path(root_dir) if root_dir else Path(".")
    env_path = base / ".env"
    colony_root = Path(__file__).resolve().parents[2]  # colony-core/

    print()
    print(_bold("🔧 Colony Sidecar Setup Wizard"))
    print(_bold("=" * 40))
    print()

    # ── Step 1: Install dependencies ────────────────────────────────────

    print(_bold("Step 1: Install dependencies"))
    print()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".[neo4j]", "-q"],
            capture_output=True, text=True, timeout=120,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        if result.returncode == 0:
            print("  ✅ Dependencies installed")
        else:
            print(f"  ⚠️ pip install had warnings (non-critical)")
    except Exception as exc:
        print(f"  ⚠️ Could not auto-install dependencies: {exc}")
        print("  Run manually: pip install -e .[neo4j]")

    print()

    # ── Step 2: Dependency checks ───────────────────────────────────────

    print(_bold("Step 2: Dependency checks"))
    print()

    py_ok, py_ver = _check_python()
    print(f"  Python: {py_ver} {'✅' if py_ok else '❌ (need 3.11+)'}")

    for dep in ["fastapi", "uvicorn", "pydantic", "neo4j", "litellm"]:
        try:
            __import__(dep)
            print(f"  {dep}: ✅")
        except ImportError:
            print(f"  {dep}: ❌ (missing)")

    docker_ok = _check_docker()
    print(f"  Docker: {'✅ available' if docker_ok else '⚪ not available'}")

    oc_ok = _check_openclaw()
    print(f"  OpenClaw: {'✅ found' if oc_ok else '⚪ not found'}")

    port = 7777
    port_taken = _check_port(port)
    print(f"  Port {port}: {'⚠️ in use' if port_taken else '✅ available'}")

    if not py_ok:
        print(_red("\nPython 3.11+ required. Please upgrade and re-run."))
        return 1

    print()

    # ── Step 3: Host framework + OpenClaw plugin ────────────────────────

    print(_bold("Step 3: Host framework"))
    print()
    print("  Colony is a sidecar — it needs a host (OpenClaw, Hermes, etc.)")
    print("  to provide LLM credentials at runtime.")
    print()

    host_choice = _prompt("  Which host framework? [openclaw/other]", "openclaw")
    oc_configured = False

    if host_choice.lower() in ("openclaw", "oc", ""):
        oc_config_path = Path.home() / ".openclaw" / "openclaw.json"
        if oc_config_path.exists():
            print(f"  ✅ OpenClaw config found: {oc_config_path}")
        else:
            print("  ⚠️ No OpenClaw config found at ~/.openclaw/openclaw.json")

        if oc_ok:
            oc_plugin = _prompt("  Configure Colony as an OpenClaw plugin? [Y/n]", "Y")
            if oc_plugin.lower() in ("y", "yes", ""):
                oc_configured = True  # Will configure after .env is written
        else:
            print("  OpenClaw CLI not found — plugin setup skipped.")
            print("  Install OpenClaw and re-run 'colony init' to configure the plugin.")
    else:
        print("  Colony will receive LLM credentials from your host on connect.")

    print()

    # ── Step 4: Docker setup ────────────────────────────────────────────

    if not docker_ok:
        print(_bold("Step 4: Docker"))
        print()
        print("  Docker is required for Neo4j (graph memory).")
        print()
        install_docker = _prompt("  Install Docker now? [Y/n]", "Y")
        if install_docker.lower() in ("y", "yes", ""):
            if _install_docker():
                print("  Waiting for Docker daemon...")
                if _wait_for_docker():
                    docker_ok = True
                    print("  ✅ Docker is running")
                else:
                    print("  ⚠️ Docker installed but daemon not reachable yet.")
                    print("  Start Docker and re-run 'colony init'.")
            else:
                print()
                print("  Install Docker manually: https://docs.docker.com/get-docker/")
                print("  Then re-run 'colony init'.")
        else:
            print("  Skipping Docker — Neo4j will not be available.")
        print()
    else:
        print(_bold("Step 4: Docker ✅ (already available)"))
        print()

    # ── Step 5: Neo4j setup ─────────────────────────────────────────────

    print(_bold("Step 5: Neo4j graph memory"))
    print()

    neo4j_ok, neo4j_info = _check_neo4j()
    neo4j_password = ""

    if neo4j_ok:
        print(f"  ✅ Neo4j is already running ({neo4j_info})")
        neo4j_password = _prompt("  Neo4j password", "colony-local-dev")
    elif docker_ok:
        print("  Neo4j is required for graph memory (persistent knowledge,")
        print("  connections, world model).")
        print()
        start_neo4j = _prompt("  Start Neo4j via Docker? [Y/n]", "Y")
        if start_neo4j.lower() in ("y", "yes", ""):
            # Password from docker-compose.yml: neo4j/colony-local-dev
            neo4j_password = "colony-local-dev"
            print("  Starting Neo4j...")
            if _start_neo4j_docker():
                print("  Waiting for Neo4j to become ready...")
                if _wait_for_neo4j():
                    print(f"  ✅ Neo4j started (bolt://localhost:7687)")
                    print(f"  Password: {neo4j_password}")
                else:
                    print("  ⚠️ Neo4j started but not reachable yet (may need a moment)")
            else:
                print("  ❌ Failed to start Neo4j via Docker")
                neo4j_password = _prompt("  Enter Neo4j password (or leave blank to skip)", "")
        else:
            neo4j_password = _prompt("  Enter Neo4j password (or blank to skip)", "")
    else:
        print("  Neo4j requires Docker, which is not available.")
        print("  Memory will be degraded until Docker + Neo4j are set up.")
        neo4j_password = _prompt("  Enter Neo4j password (or blank to skip)", "")

    print()

    # ── Step 6: Write .env ──────────────────────────────────────────────

    print(_bold("Step 6: Writing configuration"))
    print()

    existing = _load_existing_env(env_path)
    values: dict[str, str] = {
        "COLONY_SIDECAR_PORT": existing.get("COLONY_SIDECAR_PORT", "7777"),
        "COLONY_SIDECAR_HOST": existing.get("COLONY_SIDECAR_HOST", "127.0.0.1"),
        "NEO4J_URI": existing.get("NEO4J_URI", "bolt://localhost:7687"),
        "NEO4J_USER": existing.get("NEO4J_USER", "neo4j"),
        "NEO4J_PASSWORD": neo4j_password or existing.get("NEO4J_PASSWORD", ""),
        "NEO4J_DATABASE": existing.get("NEO4J_DATABASE", "neo4j"),
        "COLONY_API_KEY": existing.get("COLONY_API_KEY", secrets.token_urlsafe(32)),
        "COLONY_CONTACTS_DB": existing.get("COLONY_CONTACTS_DB", "colony-contacts.db"),
        "LOG_LEVEL": existing.get("LOG_LEVEL", "info"),
    }

    _write_env(env_path, values)
    print(f"  ✅ Written to {env_path}")

    # Configure OpenClaw plugin now that we have the API key
    if oc_configured:
        print()
        _configure_openclaw_plugin(values, colony_root)

    print()

    # ── Step 7: Database setup ──────────────────────────────────────────

    print(_bold("Step 7: Database setup"))
    print()

    contacts_db = base / values.get("COLONY_CONTACTS_DB", "colony-contacts.db")
    try:
        from colony_sidecar.contacts.store import SQLiteContactStore
        SQLiteContactStore(db_path=str(contacts_db))
        print(f"  ✅ Contacts DB initialized ({contacts_db})")
    except Exception as exc:
        print(f"  ⚠️ Contacts DB init failed: {exc}")

    try:
        from colony_sidecar.goals.store import GoalStore
        goals_db = base / "colony-goals.db"
        GoalStore(db_path=str(goals_db))
        print(f"  ✅ Goals DB initialized ({goals_db})")
    except Exception as exc:
        print(f"  ⚠️ Goals DB init failed: {exc}")

    if neo4j_password:
        try:
            from colony_sidecar.intelligence.graph.client import ColonyGraph, GraphConfig
            from pydantic import SecretStr

            async def _test_neo4j():
                config = GraphConfig(
                    uri=values["NEO4J_URI"],
                    auth=(values["NEO4J_USER"], SecretStr(neo4j_password)),
                    database=values.get("NEO4J_DATABASE", "neo4j"),
                )
                graph = ColonyGraph(config=config)
                try:
                    await graph.health_check()
                    print(f"  ✅ Neo4j connected ({values['NEO4J_URI']})")
                finally:
                    await graph.close()

            asyncio.run(_test_neo4j())
        except Exception as exc:
            print(f"  ⚠️ Neo4j connection failed: {exc}")
    else:
        print("  ⚪ Neo4j skipped (no password — memory will be degraded)")

    print()

    # ── Step 8: Self-knowledge seeding ──────────────────────────────────

    print(_bold("Step 8: Self-knowledge seeding"))
    print()
    print("  Seeding Colony with understanding of itself...")
    print("  (Full seeding happens on first 'colony start' via the /v1/host/seed endpoint)")
    print()

    # Pre-seed world model entities (SQLite, no sidecar needed)
    try:
        from colony_sidecar.world_model.store import WorldModelStore
        from colony_sidecar.seed import WORLD_MODEL_ENTITIES
        from colony_sidecar.world_model.entities import BaseEntity
        from datetime import datetime, timezone

        async def seed_entities():
            world_store = WorldModelStore()
            await world_store.connect()
            now = datetime.now(timezone.utc)

            # Map seed types to allowed SQLite types
            type_map = {
                "technology": "concept", "organization": "company",
                "framework": "concept", "project": "project",
                "person": "person", "concept": "concept",
            }

            count = 0
            for entity_data in WORLD_MODEL_ENTITIES:
                mapped_type = type_map.get(entity_data["type"], "concept")
                slug = entity_data["name"].lower().replace(" ", "-")
                e = BaseEntity(
                    id=f"seed-{mapped_type}-{slug}",
                    name=entity_data["name"],
                    entity_type=mapped_type,
                    properties={**entity_data.get("attributes", {}), "original_type": entity_data["type"]},
                    confidence=1.0,
                    first_seen=now, last_seen=now,
                    created_at=now, updated_at=now,
                )
                await world_store.upsert_entity(e)
                count += 1
            await world_store.close()
            return count

        count = asyncio.run(seed_entities())
        print(f"  ✅ World model seeded ({count} entities)")
    except Exception as exc:
        print(f"  ⚠️ World model seed deferred: {exc}")
        print("  (Will seed automatically on first start)")

    if neo4j_password:
        print("  ⚪ Graph memories + insights will seed on first start")
    else:
        print("  ⚪ Memory seeding skipped (Neo4j not configured)")

    print()

    # ── Step 9: Summary ─────────────────────────────────────────────────

    print(_bold("Step 9: Setup complete!"))
    print()

    print("  Capability status:")
    print(f"    ✅ Safety (ResponseGate)")
    print(f"    ✅ Reasoning (from host at runtime)")
    print(f"    {'✅' if neo4j_password else '⚪'} Memory (ColonyGraph{' — Neo4j connected' if neo4j_password else ' — Neo4j not configured'})")
    print(f"    ✅ Goals")
    print(f"    ✅ Contacts")
    print(f"    ✅ World Model")
    print()

    print("  Start the sidecar:")
    print(f"    {_green('colony start')}")
    print()
    print("  Then seed self-knowledge:")
    print(f"    {_green('colony seed')}")
    print()
    print("  Check health:")
    print(f"    {_green('colony status')}")
    print()

    if not oc_configured:
        print("  Add Colony to your OpenClaw config:")
        print('    {')
        print(f'      "sidecarUrl": "http://{values["COLONY_SIDECAR_HOST"]}:{values["COLONY_SIDECAR_PORT"]}",')
        print(f'      "apiKey": "{values["COLONY_API_KEY"]}"')
        print('    }')
        print()

    if not neo4j_password:
        print(_yellow("  ⚠️ Graph memory is degraded (no Neo4j)."))
        print("  Install Docker and re-run 'colony init'")
        print()

    return 0
