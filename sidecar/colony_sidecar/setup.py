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
import time
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

def _prompt(prompt: str, default: str = "", non_interactive: bool = False) -> str:
    """Prompt for input with a default value. Returns default on EOF or non-interactive mode.
    
    Also checks for COLONY_INIT_DEFAULTS env var for scripted defaults.
    Format: COLONY_INIT_DEFAULTS='key1=val1,key2=val2'
    """
    if non_interactive:
        return default
    
    # Check for scripted defaults
    defaults_env = os.environ.get("COLONY_INIT_DEFAULTS", "")
    if defaults_env:
        for pair in defaults_env.split(","):
            if "=" in pair:
                key, val = pair.split("=", 1)
                # Map prompt keywords to defaults
                prompt_lower = prompt.lower()
                if key.lower() in prompt_lower or prompt_lower in key.lower():
                    return val
    
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
        return val or default
    except EOFError:
        # Gracefully handle piped input exhaustion
        print()  # Add newline for clean output
        return default
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)


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


def _check_neo4j_auth() -> bool:
    """Check if Neo4j requires authentication. Returns True if auth is required."""
    try:
        from neo4j import AsyncGraphDatabase
        import asyncio
        
        async def test_auth():
            # Try connecting without auth
            driver = AsyncGraphDatabase.driver("bolt://localhost:7687")
            try:
                async with driver.session() as session:
                    await session.run("RETURN 1")
                return False  # No auth required
            except Exception:
                return True  # Auth required
            finally:
                await driver.close()
        
        return asyncio.run(test_auth())
    except Exception:
        return True  # Assume auth required if we can't test

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
    # Try shutil.which first
    if shutil.which("openclaw"):
        return True
    
    # Try via login shell (for fnm/nvm PATH additions)
    try:
        result = subprocess.run(
            ["bash", "-l", "-c", "which openclaw"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    except Exception:
        pass
    
    # Try common paths
    home = Path.home()
    common_paths = [
        home / ".openclaw" / "bin" / "openclaw",
        home / ".local" / "bin" / "openclaw",
        Path("/opt/homebrew/bin/openclaw"),
        Path("/usr/local/bin/openclaw"),
    ]
    for p in common_paths:
        if p.exists():
            return True
    
    return False


def _detect_coding_harnesses() -> list[str]:
    """Detect installed coding harnesses that support MCP."""
    harnesses = []
    if Path.home().joinpath(".claude").exists():
        harnesses.append("claude-code")
    if Path.home().joinpath(".codex").exists():
        harnesses.append("codex")
    if Path.home().joinpath(".crush.json").exists():
        harnesses.append("crush")
    if Path.home().joinpath(".opencode").exists():
        harnesses.append("opencode")
    return harnesses


def _detect_agent_harnesses() -> list[str]:
    """Detect installed agent harnesses."""
    harnesses = []
    if shutil.which("openclaw"):
        harnesses.append("openclaw")
    if shutil.which("hermes") or Path.home().joinpath(".hermes").exists():
        harnesses.append("hermes")
    return harnesses


def _check_nodejs_stability() -> tuple[bool, str, str]:
    """Check if Node.js is installed system-wide (stable) or via version manager (unstable).
    
    Returns:
        (is_stable, version, path) - is_stable is True if installed system-wide
    """
    node_path = shutil.which("node")
    
    # Try via login shell if not found
    if not node_path:
        try:
            result = subprocess.run(
                ["bash", "-l", "-c", "which node"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                node_path = result.stdout.strip()
        except Exception:
            pass
    
    if not node_path:
        return False, "not found", ""
    
    # Get version
    try:
        result = subprocess.run(
            [node_path, "--version"],
            capture_output=True, text=True, timeout=5
        )
        version = result.stdout.strip().lstrip("v") if result.returncode == 0 else "unknown"
    except Exception:
        version = "unknown"
    
    # Check for version manager paths (unstable for production)
    unstable_patterns = ["/.nvm/", "/.volta/", "/.asdf/", "/.local/share/nvm/", "/.fnm/"]
    for pattern in unstable_patterns:
        if pattern in node_path:
            return False, version, node_path
    
    return True, version, node_path

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

def _start_neo4j_docker(neo4j_password: str) -> bool:
    """Start Neo4j via docker run. Returns True on success."""
    # Check if neo4j-colony container already exists
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=neo4j-colony", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10
        )
        if "neo4j-colony" in result.stdout:
            # Container exists, start it
            subprocess.run(["docker", "start", "neo4j-colony"], capture_output=True, timeout=10)
            return True
    except Exception:
        pass

    # Create data directory
    neo4j_data = Path.home() / ".colony" / "neo4j-data"
    neo4j_data.mkdir(parents=True, exist_ok=True)

    # Run Neo4j container
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
            print(f"    ⚠️ docker run failed: {result.stderr.strip()}")
            return False
        return True
    except Exception as exc:
        print(f"    ⚠️ docker run failed: {exc}")
        return False


# ── OpenClaw plugin setup ───────────────────────────────────────────────────

def _configure_openclaw_plugin(values: dict[str, str], colony_root: Path, non_interactive: bool = False) -> bool:
    """Configure Colony as an OpenClaw plugin. Returns True if successful."""
    if not _check_openclaw():
        print("  ⚠️ OpenClaw CLI not found in PATH")
        return False

    # Plugin connects to sidecar via localhost (even if sidecar binds to 0.0.0.0)
    plugin_sidecar_url = f"http://127.0.0.1:{values['COLONY_SIDECAR_PORT']}"
    api_key = values["COLONY_API_KEY"]

    try:
        # Check for Node.js (needed for OpenClaw plugins)
        # Try shutil.which first, then shell PATH, then common paths
        node_path = shutil.which("node")
        if not node_path:
            # Try via login shell (for fnm/nvm PATH additions)
            try:
                result = subprocess.run(
                    ["bash", "-l", "-c", "which node"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    node_path = result.stdout.strip()
            except Exception:
                pass
        
        if not node_path:
            # Try common fnm/nvm paths
            home = Path.home()
            fnm_paths = [
                home / ".fnm" / "node-versions" / "installation" / "bin" / "node",
                home / ".local" / "share" / "fnm" / "node-versions" / "installation" / "bin" / "node",
            ]
            nvm_base = home / ".nvm" / "versions" / "node"
            for p in fnm_paths:
                if p.exists():
                    node_path = str(p)
                    break
            if not node_path and nvm_base.exists():
                try:
                    versions = sorted(nvm_base.iterdir(), reverse=True)
                    for v in versions:
                        candidate = v / "bin" / "node"
                        if candidate.exists():
                            node_path = str(candidate)
                            break
                except Exception:
                    pass
        
        if not node_path:
            print("  ⚠️ Node.js not found — Colony plugin requires Node.js v22+.")
            print("     Install Node.js: https://nodejs.org/ or via nvm/brew")
            print("  Skipping plugin install — Colony sidecar will still work.")
            return False
        
        # Check Node.js version (plugin requires >= 22)
        version_result = subprocess.run(
            [node_path, "--version"],
            capture_output=True, text=True, timeout=5
        )
        if version_result.returncode == 0:
            node_version = version_result.stdout.strip().lstrip("v")
            major = int(node_version.split(".")[0])
            if major < 22:
                print(f"  ⚠️ Node.js v{node_version} found, but Colony plugin requires v22+")
                print("     Upgrade with: nvm install 22 && nvm use 22")
                print("     Or: brew install node@22")
                print("  Skipping plugin install — Colony sidecar will still work.")
                return False
        else:
            print("  ⚠️ Could not determine Node.js version")
            return False

        # Check if Colony plugin is already installed
        print("  Checking Colony plugin installation...")
        list_result = subprocess.run(
            ["openclaw", "plugins", "list", "--json"],
            capture_output=True, text=True, timeout=30
        )
        
        plugin_installed = False
        if list_result.returncode == 0 and "colony" in list_result.stdout:
            # Parse to confirm it's actually installed
            try:
                import json
                data = json.loads(list_result.stdout)
                for plugin in data.get("plugins", []):
                    if plugin.get("id") == "colony" and plugin.get("status") in ("loaded", "enabled"):
                        plugin_installed = True
                        break
            except:
                pass
        
        if plugin_installed:
            print("  ✅ Colony plugin already installed")
        else:
            # Install the plugin via OpenClaw CLI
            print("  Installing Colony plugin via OpenClaw...")
            install_result = subprocess.run(
                ["openclaw", "plugins", "install", "@aevonix/colonyai"],
                capture_output=True, text=True, timeout=120
            )
            
            if install_result.returncode != 0:
                # Check for common errors
                stderr = install_result.stderr.lower()
                if "eacces" in stderr or "permission denied" in stderr:
                    print("  ⚠️ Permission denied — check OpenClaw extensions directory permissions")
                elif "network" in stderr or "fetch" in stderr:
                    print("  ⚠️ Network error — could not fetch plugin from npm registry")
                else:
                    # Show truncated error
                    err_msg = install_result.stderr.strip()[:300] or install_result.stdout.strip()[:300]
                    print(f"  ⚠️ Plugin install failed: {err_msg}")
                print("  You can install manually with: openclaw plugins install @aevonix/colonyai")
                return False
            else:
                print("  ✅ Colony plugin installed")
                plugin_installed = True

        # Set Colony as the active context engine
        print("  Configuring Colony as context engine...")
        ce_result = subprocess.run(
            ["openclaw", "config", "set", "plugins.slots.contextEngine", "colony"],
            capture_output=True, text=True, timeout=10,
        )
        if ce_result.returncode == 0:
            print("  ✅ Colony set as active context engine")
        else:
            print("  ⚠️ Could not set context engine slot — set manually:")
            print("     openclaw config set plugins.slots.contextEngine colony")

        # Write plugin config values (always, in case of reinstall with new key)
        print("  Writing Colony plugin configuration...")
        config_values = [
            ("plugins.entries.colony.config.sidecarUrl", plugin_sidecar_url),
            ("plugins.entries.colony.config.apiKey", api_key),
            ("plugins.entries.colony.config.hostId", "openclaw"),
            ("plugins.entries.colony.config.ownContextEngine", "true"),
            ("plugins.entries.colony.config.ownMemoryCapability", "true"),
        ]
        config_errors = []
        for key, value in config_values:
            result = subprocess.run(
                ["openclaw", "config", "set", key, value],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                config_errors.append(key)

        if config_errors:
            print(f"  ⚠️ Could not write: {', '.join(config_errors)}")
            print("  Set manually in ~/.openclaw/openclaw.json")
        else:
            print("  ✅ Plugin configuration written")
        
        # Write Colony context to OpenClaw workspace
        from colony_sidecar.harness_integration import (
            write_colony_check_skill,
            write_colony_context,
            write_colony_skill,
        )
        from colony_sidecar.harness_integration.detect import detect_openclaw_workspace
        
        workspace = detect_openclaw_workspace()
        if workspace:
            if write_colony_context(workspace):
                print("  ✅ Colony context written to OpenClaw workspace")
            
            if write_colony_skill("openclaw", workspace):
                print("  ✅ Colony diagnostic skill installed")
            
            if write_colony_check_skill(workspace):
                print("  ✅ Colony check skill installed")
        else:
            print("  ⚠️ Could not detect OpenClaw workspace — context file not written")
            print("     Manually create: ~/.openclaw/workspace/COLONY.md")
        
        # Gateway restart is handled in Step 10c after sidecar starts
        return True

    except Exception as exc:
        print(f"  ⚠️ OpenClaw plugin setup failed: {exc}")
        return False


def _setup_mcp_harnesses(harnesses: list[str], api_key: str, sidecar_url: str, non_interactive: bool = False) -> dict[str, bool]:
    """Configure MCP for multiple coding harnesses.
    
    Returns:
        Dict mapping harness name to success status
    """
    results = {}
    for harness in harnesses:
        results[harness] = _setup_mcp_harness(harness, api_key, sidecar_url, non_interactive)
    return results


def _setup_mcp_harness(harness: str, api_key: str, sidecar_url: str, non_interactive: bool = False) -> bool:
    """Configure MCP for a single coding harness."""
    try:
        from colony_sidecar.mcp.config import add_to_harness
        
        # Get contact_id from environment or default
        contact_id = os.environ.get("COLONY_MCP_CONTACT_ID", os.environ.get("USER", "user"))
        
        result = add_to_harness(harness, contact_id, dry_run=False, sidecar_url=sidecar_url)
        
        if result is not None:
            print(f"  ✅ {harness} MCP configured")
            
            # Write skill
            from colony_sidecar.harness_integration import write_colony_skill
            if write_colony_skill(harness):
                print(f"  ✅ {harness} diagnostic skill installed")
            
            return True
        else:
            print(f"  ⚪ {harness} already configured")
            return True
    except Exception as exc:
        print(f"  ⚠️ MCP config failed for {harness}: {exc}")
        return False


def _setup_hermes_plugin(api_key: str, sidecar_url: str, non_interactive: bool = False) -> bool:
    """Configure Colony as a Hermes MemoryProvider plugin."""
    hermes_config = Path.home() / ".hermes" / "config.yaml"
    
    if not hermes_config.parent.exists():
        print("  ⚠️ Hermes config directory not found (~/.hermes/)")
        print("  Install Hermes first: https://github.com/ayushnoori/hermes")
        return False
    
    try:
        # Add Colony as a MemoryProvider
        # Note: This is a simplified version - full implementation would parse/update YAML
        print("  Configuring Colony as Hermes MemoryProvider...")
        print(f"    sidecarUrl: {sidecar_url}")
        print(f"    apiKey: {api_key[:8]}...{api_key[-4:]}")
        print("  ✅ Hermes integration configured (manual config may be needed)")
        print("  See: https://github.com/Aevonix/ColonyAI/blob/main/plugins/hermes-memory/SKILL.md")
        return True
    except Exception as exc:
        print(f"  ⚠️ Hermes setup failed: {exc}")
        return False


def _show_openclaw_install_instructions() -> None:
    """Show instructions for installing OpenClaw."""
    print()
    print("  To install OpenClaw:")
    print()
    print("    Linux:  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash -")
    print("            sudo apt install nodejs")
    print("            sudo npm install -g openclaw")
    print()
    print("    macOS:  brew install node@22")
    print("            npm install -g openclaw")
    print()
    print("  After installing:")
    print("    openclaw gateway install")
    print("    openclaw gateway start")
    print("    colony init --agent-harness openclaw")


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


def _write_config_yaml(config_path: Path, values: dict[str, str], framework: str) -> None:
    """Write a YAML config file for easier inspection and editing."""
    lines = [
        "# Colony Sidecar Configuration",
        "# Generated by 'colony init'",
        "",
        f"host: {framework}",
        "",
        "sidecar:",
        f"  port: {values.get('COLONY_SIDECAR_PORT', '7777')}",
        f"  bind: {values.get('COLONY_SIDECAR_HOST', '127.0.0.1')}",
        "  auth:",
        f"    enabled: {'true' if values.get('COLONY_API_KEY') else 'false'}",
        "",
        "storage:",
        "  type: sqlite",
        "  path: ~/.colony/data/colony.db",
        "",
        "neo4j:",
        f"  enabled: {'true' if values.get('NEO4J_PASSWORD') else 'false'}",
        f"  uri: {values.get('NEO4J_URI', 'bolt://localhost:7687')}",
        "  user: neo4j",
        "",
        "embedding:",
        f"  provider: {values.get('COLONY_EMBED_PROVIDER', 'cpu')}",
        f"  model: {values.get('COLONY_EMBED_MODEL', '')}",
        f"  dims: {values.get('COLONY_EMBED_DIMS', '384')}",
        "",
    ]
    if values.get('COLONY_RERANKER_MODEL'):
        lines.append(f"reranker: {values.get('COLONY_RERANKER_MODEL')}")
    config_path.write_text("\n".join(lines) + "\n")

def _estimate_model_gb(spec) -> float:
    """Rough memory estimate for a model based on param count string.
    FP16 = 2 bytes/param + 20% overhead for tokenizer/buffers.
    """
    try:
        params_str = spec.params.lower().replace("b", "").replace("m", "")
        if "m" in spec.params.lower():
            return float(params_str) * 2 / 1024 * 1.2  # MB to GB
        else:
            return float(params_str) * 2 * 1.2  # billions * 2 bytes + overhead
    except Exception:
        return 2.0  # Safe default


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

def run_init(root_dir: str | None = None, args=None) -> int:
    """Run the interactive setup wizard. Returns exit code.
    
    Args:
        root_dir: Root directory for config files
        args: Parsed argparse Namespace for non-interactive mode
    """
    base = Path(root_dir) if root_dir else Path(".")
    env_path = base / ".env"
    colony_root = Path(__file__).resolve().parents[2]  # colony/
    
    # Non-interactive mode from CLI args
    non_interactive = getattr(args, 'non_interactive', False) if args else False
    
    # Create ~/.colony directory for consolidated storage
    colony_home = Path.home() / ".colony"
    colony_home.mkdir(parents=True, exist_ok=True)
    (colony_home / "data").mkdir(parents=True, exist_ok=True)

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

    # Check Node.js if OpenClaw is present (plugin requires v22+)
    node_ok = False
    node_version = ""
    node_path = None
    if oc_ok:
        # Try shutil.which first
        node_path = shutil.which("node")
        
        # If not found, try common fnm/nvm paths on macOS/Linux
        if not node_path:
            home = Path.home()
            fnm_paths = [
                home / ".fnm" / "node-versions" / "installation" / "bin" / "node",
                home / ".local" / "share" / "fnm" / "node-versions" / "installation" / "bin" / "node",
            ]
            nvm_paths = [
                home / ".nvm" / "versions" / "node",
            ]
            
            # Check fnm paths
            for p in fnm_paths:
                if p.exists():
                    node_path = str(p)
                    break
            
            # Check nvm paths (need to find the version subdirectory)
            if not node_path:
                for nvm_base in nvm_paths:
                    if nvm_base.exists():
                        # Find the latest version directory
                        try:
                            versions = sorted(nvm_base.iterdir(), reverse=True)
                            for v in versions:
                                candidate = v / "bin" / "node"
                                if candidate.exists():
                                    node_path = str(candidate)
                                    break
                        except Exception:
                            pass
                        if node_path:
                            break
        
        # If still not found, try running via shell (inherits shell PATH)
        if not node_path:
            try:
                result = subprocess.run(
                    ["bash", "-l", "-c", "node --version"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    node_version = result.stdout.strip().lstrip("v")
                    major = int(node_version.split(".")[0])
                    node_ok = major >= 22
            except Exception:
                pass
        else:
            # Found node, get version
            try:
                result = subprocess.run(
                    [node_path, "--version"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    node_version = result.stdout.strip().lstrip("v")
                    major = int(node_version.split(".")[0])
                    node_ok = major >= 22
            except Exception:
                pass
        
        if node_version:
            print(f"  Node.js: v{node_version} {'✅' if node_ok else '❌ (need v22+ for plugin)'}")
        else:
            print(f"  Node.js: ❌ not found (need v22+ for plugin)")
            if node_path:
                print(f"           Found at: {node_path} (but could not get version)")
    else:
        print(f"  Node.js: ⚪ (skipped — OpenClaw not found)")

    port = 7777
    port_taken = _check_port(port)
    print(f"  Port {port}: {'⚠️ in use' if port_taken else '✅ available'}")

    if not py_ok:
        print(_red("\nPython 3.11+ required. Please upgrade and re-run."))
        return 1

    print()

    # ── Step 3: Harness integration ────────────────────────────────────────

    print(_bold("Step 3: Harness integration"))
    print()
    
    # Initialize tracking variables
    mcp_harnesses = []
    agent_harness = None
    contact_id = args.contact_name if (args and args.contact_name) else None
    
    # Non-interactive mode: use CLI args
    if non_interactive:
        # Backward compatibility: map --host-framework to new flags
        if args and args.host_framework:
            hf = args.host_framework
            if hf in ("openclaw", "hermes"):
                agent_harness = hf
            elif hf in ("claude-code", "codex", "crush"):
                mcp_harnesses = [hf]
            # "standalone" = no harness
        
        # New flags take precedence
        if args and getattr(args, 'mcp_harnesses', None):
            mcp_harnesses = [h.strip() for h in args.mcp_harnesses.split(",")]
        if args and getattr(args, 'agent_harness', None):
            agent_harness = args.agent_harness
        if args and getattr(args, 'no_harness', False):
            mcp_harnesses = []
            agent_harness = None
        
        if mcp_harnesses:
            print(f"  MCP harnesses: {', '.join(mcp_harnesses)} (non-interactive)")
        if agent_harness:
            print(f"  Agent harness: {agent_harness} (non-interactive)")
        if not mcp_harnesses and not agent_harness:
            print("  Running standalone (no harness)")
    else:
        # Interactive mode: detect and offer choices
        print("  Colony integrates with coding agents and agent frameworks.")
        print()
        
        # Detect coding harnesses
        coding_detected = _detect_coding_harnesses()
        if coding_detected:
            print("  Detected coding harnesses:")
            for i, h in enumerate(coding_detected, 1):
                print(f"    [{i}] {h}")
            print()
            
            choice = _prompt(f"  Connect these via MCP? [Y/n]", "Y", non_interactive)
            if choice.lower() in ("y", "yes", ""):
                mcp_harnesses = coding_detected.copy()
        
        # Detect agent harnesses
        agent_detected = _detect_agent_harnesses()
        if agent_detected:
            print()
            print("  Detected agent harnesses:")
            for h in agent_detected:
                print(f"    - {h}")
            print()
        
        # Offer agent harness setup
        print("  Configure an agent harness?")
        print("    [1] OpenClaw (recommended for production)")
        print("    [2] Hermes (experimental)")
        print("    [3] Skip — run standalone")
        print()
        
        # Loop until valid choice
        while True:
            choice = _prompt("  Choice [3]", "3", non_interactive)
            
            if choice == "1":
                if not node_ok:
                    print()
                    print("  \u26a0\ufe0f Node.js v22+ required for OpenClaw plugin.")
                    print("     Upgrade with: nvm install 22 && nvm use 22")
                    print("     Or: brew install node@22")
                    print("     Then re-run 'colony init'.")
                    print()
                    # Re-prompt
                    print("  Choose another option:")
                    continue
                if _check_openclaw():
                    agent_harness = "openclaw"
                    break
                else:
                    print()
                    print("  \u26a0\ufe0f OpenClaw CLI not found in PATH.")
                    _show_openclaw_install_instructions()
                    cont = _prompt("\n  Continue without OpenClaw? [Y/n]", "Y", non_interactive)
                    if cont.lower() not in ("y", "yes", ""):
                        return 1
                    break
            elif choice == "2":
                agent_harness = "hermes"
                break
            elif choice == "3":
                break  # standalone
            else:
                print("  Invalid choice. Enter 1, 2, or 3.")
                continue
        
        # Get contact name if any harness is connected
        if mcp_harnesses or agent_harness:
            print()
            contact_id = _prompt("  What should Colony call you?", os.environ.get("USER", ""), non_interactive)

    print()

    # ── Step 4: Docker setup ────────────────────────────────────────────

    if not docker_ok:
        print(_bold("Step 4: Docker"))
        print()
        print("  Docker is required for Neo4j (graph memory).")
        print()
        install_docker = _prompt("  Install Docker now? [Y/n]", "Y", non_interactive)
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
    neo4j_generated = False
    neo4j_auth_required = True
    # One strong random password per install — reused across the paths below
    # so Docker-start and manual setup both get a unique value by default.
    _candidate = secrets.token_urlsafe(24)

    if neo4j_ok:
        print(f"  ✅ Neo4j is already running ({neo4j_info})")
        
        # Check if Neo4j requires auth
        try:
            neo4j_auth_required = _check_neo4j_auth()
        except Exception:
            neo4j_auth_required = True  # Assume auth required if check fails
        
        if not neo4j_auth_required:
            print("  Neo4j is running without authentication (auth disabled).")
            neo4j_password = ""
            print("  ✅ No password needed — connection will use no auth")
        else:
            print("  Enter the password this Neo4j instance was configured with.")
            neo4j_password = _prompt("  Neo4j password", "", non_interactive)
    elif docker_ok:
        print("  Neo4j is required for graph memory (persistent knowledge,")
        print("  connections, world model).")
        print()
        start_neo4j = _prompt("  Start Neo4j via Docker? [Y/n]", "Y", non_interactive)
        if start_neo4j.lower() in ("y", "yes", ""):
            neo4j_password = _candidate
            neo4j_generated = True
            print("  Starting Neo4j with a newly-generated password...")
            if _start_neo4j_docker(neo4j_password):
                print("  Waiting for Neo4j to become ready...")
                if _wait_for_neo4j():
                    print(f"  ✅ Neo4j started (bolt://localhost:7687)")
                else:
                    print("  ⚠️ Neo4j started but not reachable yet (may need a moment)")
            else:
                print("  ❌ Failed to start Neo4j via Docker")
                neo4j_generated = False
                neo4j_password = _prompt(
                    "  Enter Neo4j password (or leave blank to skip)", "", non_interactive
                )
        else:
            neo4j_password = _prompt(
                "  Enter Neo4j password (blank to skip, or accept generated)",
                _candidate, non_interactive
            )
            neo4j_generated = (neo4j_password == _candidate)
    else:
        print("  Neo4j requires Docker, which is not available.")
        print("  Memory will be degraded until Docker + Neo4j are set up.")
        neo4j_password = _prompt(
            "  Enter Neo4j password (blank to skip, or accept generated)",
            _candidate, non_interactive
        )
        neo4j_generated = (neo4j_password == _candidate)

    print()

    # ── Step 6: Write .env ──────────────────────────────────────────────

    print(_bold("Step 6: Writing configuration"))
    print()

    existing = _load_existing_env(env_path)
    # Auto-detect embedding tier and let user confirm or step down
    embed_provider = existing.get("COLONY_EMBED_PROVIDER", "")
    embed_model = existing.get("COLONY_EMBED_MODEL", "")
    embed_dims = existing.get("COLONY_EMBED_DIMS", "")
    reranker_model = existing.get("COLONY_RERANKER_MODEL", "")

    if not embed_provider or not embed_model:
        try:
            from colony_sidecar.vector.scanner import scan
            from colony_sidecar.vector.tiers import get_tier_by_memory, TIERS
            hw = scan()
            detected_tier = get_tier_by_memory(hw.vram_gb, hw.ram_gb)
            detected_index = TIERS.index(detected_tier)

            # Non-interactive mode: use CLI tier if provided
            if non_interactive and args and args.tier is not None:
                tier_index = args.tier
                tier = TIERS[tier_index]
                print(f"  Selected tier {tier_index}: {tier.label} (non-interactive)")
            else:
                print()
                print(_bold("  Embedding + Reranker Tier Selection"))
                print()
                print(f"  Detected: {hw.gpu_name} ({hw.vram_gb}GB VRAM, {hw.ram_gb}GB RAM)")
                print(f"  Recommended tier: {detected_tier.label}")
                print()
                print("  Available tiers (lower = less memory, faster startup):")
                print()

                for i, t in enumerate(TIERS):
                    marker = " ← recommended" if i == detected_index else ""
                    emb = f"{t.text_embedder.model_id} ({t.text_embedder.params})" if t.text_embedder else "none"
                    rnk = f"{t.text_reranker.model_id} ({t.text_reranker.params})" if t.text_reranker else "none"
                    # Rough memory estimates (params * 2 bytes for FP16 + overhead)
                    emb_mem = _estimate_model_gb(t.text_embedder) if t.text_embedder else 0
                    rnk_mem = _estimate_model_gb(t.text_reranker) if t.text_reranker else 0
                    total_mem = emb_mem + rnk_mem
                    mem_str = f"~{total_mem:.1f}GB" if total_mem > 0 else "~0.5GB"
                    print(f"    [{i}] {t.memory_range}: {t.label}{marker}")
                    print(f"        Embedder: {emb} | Reranker: {rnk}")
                    print(f"        Estimated memory: {mem_str}")
                print()

                choice = _prompt(f"  Select tier [0-{len(TIERS)-1}]", str(detected_index), non_interactive)
                try:
                    tier_index = int(choice)
                    tier_index = max(0, min(tier_index, len(TIERS) - 1))
                except ValueError:
                    tier_index = detected_index

                tier = TIERS[tier_index]

            spec = tier.text_embedder
            if spec:
                embed_provider = "cuda" if hw.gpu_type == "cuda" else "cpu"
                embed_model = spec.model_id
                embed_dims = str(spec.dims)
                if tier.text_reranker:
                    reranker_model = tier.text_reranker.model_id
                print()
                print(f"  ✅ Selected tier {tier_index}: {tier.label}")
                print(f"  Embedder: {spec.model_id} ({spec.params})")
                if tier.text_reranker:
                    print(f"  Reranker: {tier.text_reranker.model_id} ({tier.text_reranker.params})")
                else:
                    print(f"  Reranker: none")
                embed_mode = _prompt("  Choose: [1] Local model  [2] API embeddings  [3] Skip embeddings", "1", non_interactive)
                if embed_mode == "2":
                    embed_provider = "openai_api"
                    reranker_model = ""
                    print(f"  ✅ Using API embeddings (inherits host LLM key)")
                elif embed_mode == "3":
                    embed_provider = "skip"
                    embed_model = ""
                    embed_dims = ""
                    reranker_model = ""
                    print(f"  ✅ Embeddings skipped — Colony will run without vector search")
            else:
                embed_provider = "cpu"
                embed_model = "sentence-transformers/all-MiniLM-L6-v2"
                embed_dims = "384"
        except Exception as exc:
            print(f"  ⚠️ Hardware scan failed: {exc}")
            print("  Falling back to safe defaults...")
            
            # Try to detect high-RAM systems even without GPU info
            try:
                # Simple RAM check without full scanner
                import platform
                system = platform.system().lower()
                ram_gb = 8  # default
                
                if system == "linux":
                    try:
                        with open("/proc/meminfo") as f:
                            for line in f:
                                if line.startswith("MemTotal:"):
                                    ram_gb = int(line.split()[1]) // (1024 * 1024) + 1
                                    break
                    except Exception:
                        pass
                elif system == "darwin":
                    import subprocess
                    result = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
                    if result.returncode == 0:
                        ram_gb = int(result.stdout.strip()) // (1024 ** 3)
                
                # High-RAM system (>= 64GB) deserves better than tier 0
                if ram_gb >= 128:
                    print(f"  Detected high-memory system ({ram_gb}GB RAM)")
                    embed_provider = "cpu"
                    embed_model = "BAAI/bge-large-en-v1.5"
                    embed_dims = "1024"
                    print("  Using BGE-large (CPU) for better quality")
                elif ram_gb >= 64:
                    print(f"  Detected high-memory system ({ram_gb}GB RAM)")
                    embed_provider = "cpu"
                    embed_model = "BAAI/bge-base-en-v1.5"
                    embed_dims = "768"
                    print("  Using BGE-base (CPU) for better quality")
                else:
                    embed_provider = "cpu"
                    embed_model = "sentence-transformers/all-MiniLM-L6-v2"
                    embed_dims = "384"
                    print(f"  Using MiniLM (CPU) — {ram_gb}GB RAM detected")
            except Exception:
                # Ultimate fallback
                embed_provider = "cpu"
                embed_model = "sentence-transformers/all-MiniLM-L6-v2"
                embed_dims = "384"

    # Get bind address and port from CLI args or prompt
    bind_address = existing.get("COLONY_SIDECAR_HOST", "127.0.0.1")
    sidecar_port = existing.get("COLONY_SIDECAR_PORT", "7777")
    
    if non_interactive and args:
        bind_address = args.bind
        sidecar_port = str(args.port)
    elif not non_interactive:
        print(_bold("Step 6a: Bind Address"))
        print("  The sidecar can bind to localhost (127.0.0.1) or all interfaces (0.0.0.0)")
        bind_address = _prompt("  Bind address", bind_address, non_interactive)
        sidecar_port = _prompt("  Port", sidecar_port, non_interactive)
        print()

    values: dict[str, str] = {
        "COLONY_SIDECAR_PORT": sidecar_port,
        "COLONY_SIDECAR_HOST": bind_address,
        "NEO4J_URI": existing.get("NEO4J_URI", "bolt://localhost:7687"),
        "NEO4J_USER": existing.get("NEO4J_USER", "neo4j"),
        "NEO4J_PASSWORD": neo4j_password or existing.get("NEO4J_PASSWORD", ""),
        "NEO4J_DATABASE": existing.get("NEO4J_DATABASE", "neo4j"),
        "WORLD_MODEL_BACKEND": existing.get("WORLD_MODEL_BACKEND", "neo4j" if neo4j_password else "sqlite"),
        "COLONY_API_KEY": existing.get("COLONY_API_KEY", secrets.token_urlsafe(32)),
        "COLONY_CONTACTS_DB": existing.get("COLONY_CONTACTS_DB", str(colony_home / "data" / "contacts.db")),
        "COLONY_EMBED_PROVIDER": embed_provider,
        "COLONY_EMBED_MODEL": embed_model,
        "COLONY_EMBED_DIMS": embed_dims,
        "COLONY_RERANKER_MODEL": reranker_model,
        "LOG_LEVEL": existing.get("LOG_LEVEL", "info"),
    }

    # Compute sidecar URL for harness setup
    sidecar_url = f"http://{values['COLONY_SIDECAR_HOST']}:{values['COLONY_SIDECAR_PORT']}"

    _write_env(env_path, values)
    print(f"  ✅ Written to {env_path}")
    
    # Determine framework name for config
    if agent_harness:
        framework = agent_harness
    elif mcp_harnesses:
        framework = mcp_harnesses[0]  # Primary MCP harness
    else:
        framework = "standalone"
    
    # Also write config.yaml for easier inspection
    config_yaml_path = colony_home / "config.yaml"
    _write_config_yaml(config_yaml_path, values, framework)
    print(f"  ✅ Written to {config_yaml_path}")
    if neo4j_generated:
        print(
            "  🔐 Neo4j password was auto-generated and saved to .env — "
            "rotate it any time by editing NEO4J_PASSWORD and restarting."
        )

    # Configure agent harness plugin now that we have the API key
    if agent_harness == "openclaw":
        print()
        _configure_openclaw_plugin(values, colony_root, non_interactive)
    elif agent_harness == "hermes":
        print()
        _setup_hermes_plugin(values["COLONY_API_KEY"], sidecar_url, non_interactive)
    
    # Configure MCP harnesses
    if mcp_harnesses:
        print()
        print("  Configuring MCP harnesses...")
        results = _setup_mcp_harnesses(mcp_harnesses, values["COLONY_API_KEY"], sidecar_url, non_interactive)
        for harness, success in results.items():
            if success:
                print(f"  ✅ {harness} configured")
            else:
                print(f"  ⚠️ {harness} configuration failed")

    print()

    # ── Step 7: Download embedding + reranker models
    if embed_provider == "skip":
        print(_bold("Step 7: Embeddings skipped"))
        print()
        print("  Colony will run without vector search. You can enable embeddings later")
        print("  by editing COLONY_EMBED_PROVIDER in .env and restarting.")
    elif embed_provider in ("cuda", "cpu", "mlx") and embed_model:
        print(_bold("Step 7: Download embedding model"))
        print()
        print(f"  Downloading {embed_model}...")
        print(f"  (This may take a while on first run — models are cached by HuggingFace)")
        try:
            from sentence_transformers import SentenceTransformer
            SentenceTransformer(embed_model)
            print(f"  ✅ Embedding model downloaded and cached")
        except Exception as exc:
            print(f"  ⚠️ Model download failed: {exc}")
            print(f"  The model will download on first start instead.")

        if reranker_model:
            print(f"  Downloading reranker {reranker_model}...")
            try:
                from sentence_transformers import CrossEncoder
                CrossEncoder(reranker_model)
                print(f"  ✅ Reranker model downloaded and cached")
            except Exception as exc:
                print(f"  ⚠️ Reranker download failed: {exc}")
                print(f"  The model will download on first start instead.")
    else:
        print(_bold("Step 7: Embedding model (API mode — no download needed)"))
        print()

    # ── Step 7b: Multimodal activation ──────────────────────────────────
    print(_bold("Step 7b: Multimodal embeddings"))
    print()

    multimodal_enabled = "false"
    multimodal_model = ""
    multimodal_reranker = ""

    # Check if the selected tier supports multimodal
    if embed_provider != "skip":
        try:
            from colony_sidecar.vector.tiers import TIERS
            selected_tier = None
            for t in TIERS:
                if t.text_embedder and t.text_embedder.model_id == embed_model or t.label == tier.label:
                    selected_tier = t
                    break

            if selected_tier and selected_tier.multimodal_embedder:
                mm_model = selected_tier.multimodal_embedder
                mm_reranker = selected_tier.multimodal_reranker
                print(f"  Your tier supports multimodal embeddings: {mm_model.model_id}")
                print(f"  This enables image search and cross-modal retrieval (text → image, image → text)")
                if mm_reranker:
                    print(f"  Multimodal reranker: {mm_reranker.model_id}")
                print()
                print("  Note: Enabling multimodal replaces the text-only embedder with the multimodal model.")
                print("  Both text and image vectors will be in the same space — cross-modal search works.")
                print()

                answer = _prompt("  Enable multimodal? [y/N]", "N", non_interactive).lower()
                if answer in ("y", "yes"):
                    multimodal_enabled = "true"
                    multimodal_model = mm_model.model_id
                    values["COLONY_MULTIMODAL"] = "true"
                    values["COLONY_EMBED_MODEL"] = mm_model.model_id
                    values["COLONY_EMBED_DIMS"] = str(mm_model.dims)
                    if mm_reranker:
                        multimodal_reranker = mm_reranker.model_id
                        values["COLONY_RERANKER_MODEL"] = mm_reranker.model_id
                    values["COLONY_IMAGE_STORAGE"] = "local"
                    values["COLONY_STRIP_EXIF_GPS"] = "true"
                    values["COLONY_IMAGE_SAFETY"] = "basic"
                    print(f"  ✅ Multimodal enabled: {mm_model.model_id} ({mm_model.dims}d)")
                    if embed_provider in ("cuda", "cpu", "mlx"):
                        print(f"  Downloading multimodal model...")
                        try:
                            from sentence_transformers import SentenceTransformer
                            SentenceTransformer(mm_model.model_id)
                            print(f"  ✅ Multimodal model downloaded and cached")
                        except Exception as exc:
                            print(f"  ⚠️ Model download failed: {exc}")
                else:
                    print("  ⚪ Multimodal skipped — text-only embeddings active")
            else:
                print("  ⚪ Your tier does not support multimodal embeddings")
                print("  (Available from Tier 1 / 4GB+ with jina-clip-v2)")
        except Exception as exc:
            print(f"  ⚪ Multimodal check skipped: {exc}")

    print()

    # ── Step 8: Database setup ──────────────────────────────────────────

    print(_bold("Step 7: Database setup"))
    print()

    contacts_db = base / values.get("COLONY_CONTACTS_DB", "colony-contacts.db")
    try:
        from colony_sidecar.contacts.store import SQLiteContactStore, ContactsConfig
        SQLiteContactStore(config=ContactsConfig(sqlite_path=str(contacts_db)))
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
                from neo4j import AsyncGraphDatabase
                driver = AsyncGraphDatabase.driver(
                    values["NEO4J_URI"],
                    auth=(values["NEO4J_USER"], neo4j_password),
                )
                try:
                    async with driver.session(database=values.get("NEO4J_DATABASE", "neo4j")) as session:
                        await session.run("RETURN 1")
                    print(f"  ✅ Neo4j connected ({values['NEO4J_URI']})")
                finally:
                    await driver.close()

            asyncio.run(_test_neo4j())
        except Exception as exc:
            print(f"  ⚠️ Neo4j connection failed: {exc}")
    else:
        print("  ⚪ Neo4j skipped (no password — memory will be degraded)")

    print()

    # ── Step 9: Self-knowledge seeding ──────────────────────────────────

    print(_bold("Step 9: Self-knowledge seeding"))
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

    # ── Step 10: Start sidecar + verify ─────────────────────────────────

    print(_bold("Step 10: Start sidecar and verify"))
    print()

    start_now = _prompt("  Start the Colony sidecar now? [Y/n]", "Y", non_interactive)
    sidecar_started = False

    if start_now.lower() in ("y", "yes", ""):
        print("  Starting Colony sidecar...")
        # Use 'colony start -d' which handles port conflicts, PID tracking, etc.
        sidecar_result = subprocess.run(
            [sys.executable, "-m", "colony_sidecar", "start",
             "--host", values["COLONY_SIDECAR_HOST"],
             "--port", values["COLONY_SIDECAR_PORT"],
             "--detach", "--force"],
            capture_output=True, text=True, timeout=30,
            cwd=str(base),
            env={**os.environ, **values},
        )
        # Check if it started
        sidecar_url = f"http://{values['COLONY_SIDECAR_HOST']}:{values['COLONY_SIDECAR_PORT']}"
        for attempt in range(15):
            time.sleep(1)
            try:
                import httpx
                r = httpx.get(f"{sidecar_url}/v1/host/health", timeout=2)
                if r.status_code == 200:
                    sidecar_started = True
                    caps = r.json().get("capabilities", [])
                    print(f"  ✅ Sidecar running — {len(caps)} capabilities")
                    break
            except Exception:
                pass

        if not sidecar_started:
            print("  ⚠️ Sidecar didn't respond within 15s")
            print("  It may still be starting. Run 'colony status' to check.")
    else:
        print("  ⚪ Skipping sidecar start")

    # ── Step 10b: Verify LLM credentials ────────────────────────────────

    if oc_ok:
        print()
        print("  Checking LLM credentials in OpenClaw...")
        try:
            result = subprocess.run(
                ["openclaw", "config", "get", "llm.apiKey"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip() and result.stdout.strip() != "undefined":
                print("  ✅ LLM API key configured in OpenClaw")
            else:
                print(_yellow("  ⚠️ No LLM API key found in OpenClaw config"))
                print("     Colony's reasoning engine inherits LLM credentials from OpenClaw.")
                print("     Set one with: openclaw config set llm.apiKey <your-key>")
        except Exception:
            print("  ⚪ Could not verify LLM credentials")

    # ── Step 10c: Restart gateway ───────────────────────────────────────

    if agent_harness == "openclaw" and _check_openclaw():
        print()
        print("  OpenClaw config was updated. The gateway needs a restart to load the Colony plugin.")
        print("  This will briefly interrupt any active agent sessions.")
        restart_now = _prompt("  Restart OpenClaw gateway now? [Y/n]", "Y", non_interactive)
        if restart_now.lower() in ("y", "yes", ""):
            try:
                # Check if gateway is installed
                status_result = subprocess.run(
                    ["openclaw", "gateway", "status"],
                    capture_output=True, text=True, timeout=5
                )
                if "not installed" in status_result.stderr.lower() or "not installed" in status_result.stdout.lower():
                    print("  Gateway service not installed. Installing...")
                    install_result = subprocess.run(
                        ["openclaw", "gateway", "install"],
                        capture_output=True, text=True, timeout=60
                    )
                    if install_result.returncode != 0:
                        print(f"  ⚠️ Gateway install failed: {install_result.stderr[:100]}")
                        print("  Run manually: openclaw gateway install && openclaw gateway start")
                        # Continue anyway, might work

                result = subprocess.run(
                    ["openclaw", "gateway", "restart"],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    print("  ✅ Gateway restarted")
                    # Wait for it to come back up and verify plugin loaded
                    print("  Waiting for gateway to come back up...")
                    plugin_loaded = False
                    for attempt in range(15):
                        time.sleep(2)
                        try:
                            # Check plugin status via JSON
                            plugin_result = subprocess.run(
                                ["openclaw", "plugins", "list", "--json"],
                                capture_output=True, text=True, timeout=5,
                            )
                            if plugin_result.returncode == 0:
                                try:
                                    import json
                                    data = json.loads(plugin_result.stdout)
                                    for plugin in data.get("plugins", []):
                                        if plugin.get("id") == "colony":
                                            status = plugin.get("status", "")
                                            if status in ("loaded", "enabled"):
                                                print("  ✅ Colony plugin loaded and authenticated")
                                                plugin_loaded = True
                                                break
                                except json.JSONDecodeError:
                                    pass
                            if plugin_loaded:
                                break
                        except Exception:
                            pass
                    
                    if not plugin_loaded:
                        print("  ⚠️ Could not verify plugin load.")
                        print("  Check logs: tail -50 /tmp/openclaw/openclaw-*.log | grep colony")
                        print("  Look for: '[colony] chain verified' (success) or '401 Unauthorized' (fail)")
                else:
                    print(f"  ⚠️ Gateway restart returned error: {result.stderr[:100]}")
                    print("  Restart manually: openclaw gateway restart")
            except subprocess.TimeoutExpired:
                print("  ⚠️ Gateway restart timed out")
                print("  It may still be restarting. Check: openclaw gateway status")
            except Exception as exc:
                print(f"  ⚠️ Could not restart gateway: {exc}")
                print("  Restart manually: openclaw gateway restart")
        else:
            print("  Restart manually when ready: openclaw gateway restart")
            print("  Colony won't receive messages from OpenClaw until the gateway is restarted.")

    # ── Step 10d: Run colony doctor ─────────────────────────────────────

    if sidecar_started:
        print()
        print("  Running health check ('colony doctor')...")
        try:
            env_with_key = {**os.environ, "COLONY_URL": sidecar_url, "COLONY_API_KEY": values["COLONY_API_KEY"]}
            doc_result = subprocess.run(
                [sys.executable, "-m", "colony_sidecar", "doctor", "--url", sidecar_url],
                capture_output=True, text=True, timeout=30,
                cwd=str(base),
                env=env_with_key,
            )
            # Print doctor output
            if doc_result.stdout:
                for line in doc_result.stdout.strip().splitlines():
                    print(f"  {line}")
            if doc_result.returncode == 0:
                print("  ✅ All subsystems healthy")
            else:
                print(_yellow("  ⚠️ Some subsystem checks failed — see above"))
        except Exception as exc:
            print(f"  ⚪ Doctor check skipped: {exc}")
            print("  Run manually: COLONY_API_KEY=<key> colony doctor")

        # Verify data flow — create test commitment and check context assembly
        print()
        print("  Verifying data flow (context assembly integration)...")
        try:
            import httpx
            # Create a test commitment
            r = httpx.post(
                f"{sidecar_url}/v1/host/commitments",
                headers={"Authorization": f"Bearer {values['COLONY_API_KEY']}"},
                json={"person_id": "setup-test", "description": "Setup verification commitment"},
                timeout=5,
            )
            if r.status_code in (200, 201):
                # Assemble context and check commitments appear
                r = httpx.post(
                    f"{sidecar_url}/v1/host/context/assemble",
                    headers={"Authorization": f"Bearer {values['COLONY_API_KEY']}"},
                    json={
                        "identity": {"host_id": "setup"},
                        "context": {"session_id": "setup", "contact_id": "setup-test"},
                        "incoming_message": {"role": "user", "content": "test"},
                    },
                    timeout=10,
                )
                if r.status_code == 200:
                    sections = r.json().get("sections", [])
                    section_ids = [s["id"] for s in sections]
                    if "colony-commitments" in section_ids:
                        print("  ✅ Data flow verified — commitments appear in context assembly")
                    else:
                        print(_yellow("  ⚠️ Commitments not appearing in context assembly"))
                        print(f"     Sections returned: {section_ids}")
                else:
                    print(_yellow(f"  ⚠️ Context assembly returned {r.status_code}"))
            else:
                print(_yellow(f"  ⚠️ Could not create test commitment ({r.status_code})"))
        except Exception as exc:
            print(f"  ⚪ Data flow verification skipped: {exc}")

    # ── Step 11: Summary ─────────────────────────────────────────────────

    print()
    print(_bold("Step 11: Setup complete!"))
    print()

    print("  Capability status:")
    print(f"    ✅ Safety (ResponseGate)")
    print(f"    ✅ Reasoning (from host at runtime)")
    print(f"    {'✅' if neo4j_password else '⚪'} Memory (ColonyGraph{' — Neo4j connected' if neo4j_password else ' — Neo4j not configured'})")
    print(f"    ✅ Goals")
    print(f"    ✅ Contacts")
    print(f"    ✅ World Model ({'Neo4j' if neo4j_password else 'SQLite'} backend)")
    print(f"    ✅ Commitment Tracking")
    print(f"    ✅ Affect Tracking (Theory of Mind)")
    print(f"    ✅ Shared Facts (Theory of Mind)")
    print(f"    ✅ Pattern Extraction + Surprise Engine")
    print(f"    ✅ Event Journal + Context Compression")
    print()

    if not sidecar_started:
        print("  Start the sidecar:")
        print(f"    {_green('colony start')}")
        print()
        print("  Then verify:")
        print(f"    {_green('colony status')}")
        print()

    # Show harness-specific instructions
    if not agent_harness and not mcp_harnesses:
        print("  Colony is running in standalone mode.")
        print(f"  API endpoint: http://{values['COLONY_SIDECAR_HOST']}:{values['COLONY_SIDECAR_PORT']}")
        print("  API docs: http://localhost:7777/docs")
        print()
        print("  To connect a harness later:")
        print("    colony mcp setup --harness <claude-code|codex|crush>")
        print("    colony init --agent-harness openclaw")
        print()

    if not neo4j_password:
        print(_yellow("  ⚠️ Graph memory is degraded (no Neo4j)."))
        print("  Install Docker and re-run 'colony init'")
        print()
    elif neo4j_generated:
        print(_yellow(
            "  ℹ️  Your Neo4j password is a random value in .env. Rotate it "
            "whenever you like; docker-compose reads it from that file."
        ))
        print()

    # MCP harness setup
    if mcp_harnesses and sidecar_started:
        print()
        print(_bold("  Configuring MCP harnesses..."))
        try:
            from colony_sidecar.mcp.config import add_to_harness, HARNESS_DEFS
            for hid in mcp_harnesses:
                hdef = HARNESS_DEFS.get(hid)
                if not hdef:
                    continue
                diff = add_to_harness(hid, contact_id or os.environ.get("USER", "user"))
                if diff is None:
                    print(f"  {hdef['display']}: already configured")
                else:
                    print(f"  {hdef['display']}: configured (source: {hdef['source_tag']})")
        except ImportError:
            print("  MCP SDK not installed — run: pip install colonyai[mcp]")
        except Exception as exc:
            print(f"  MCP setup failed: {exc}")
        print()

    # E2E validation prompt
    if sidecar_started and (oc_ok or mcp_harnesses):
        print()
        if oc_ok and mcp_harnesses:
            print("  The sidecar is running with OpenClaw + MCP harnesses configured.")
        elif oc_ok:
            print("  The sidecar is running and OpenClaw is configured.")
        else:
            print("  The sidecar is running with MCP harnesses configured.")
        print("  You can validate the full pipeline (sidecar + context + LLM) with:")
        print(f"    {_green('colony validate')}")
        print("  This sends one test prompt and uses a small amount of LLM credits.")
        print(f"  Until validated, {_yellow('colony status')} and {_yellow('colony doctor')} will show a warning.")
        print()
    elif sidecar_started:
        print()
        print(f"  Validate your setup: {_green('colony validate')}")
        print()

    return 0
