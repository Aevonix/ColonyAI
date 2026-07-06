"""Colony setup wizard - ``colony init``.

Guides the user through first-time configuration:
1. Install dependencies
2. Dependency checks
3. Harness integration (Hermes plugin / MCP)
4. Docker setup (if needed)
5. Neo4j setup (auto-start via Docker or manual)
6. Write .env
7. Database setup
8. Autonomy & approvals (owner contact, approval policy, gates, home channel)
9. Self-knowledge seeding
10. Start sidecar + verify (10e: schedule agent workers via crontab)
11. Summary
12. Health check (colony doctor)
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


# ── Docker Status Enum ───────────────────────────────────────────────────────

from enum import Enum


class DockerStatus(str, Enum):
    """Docker installation/runtime status."""

    # Good states
    RUNNING = "running"  # Docker daemon is running

    # Install states
    NOT_INSTALLED = "not_installed"  # No docker binary found
    DESKTOP_INSTALLED_NOT_RUNNING = "desktop_not_running"  # macOS: Docker Desktop app exists, daemon off

    # Runtime states
    INSTALLED_NOT_RUNNING = "not_running"  # docker binary exists, daemon off
    PERMISSION_DENIED = "permission_denied"  # Linux: user not in docker group

    # Alternative runtimes
    COLIMA_INSTALLED = "colima"  # macOS: Colima installed
    ORBSTACK_INSTALLED = "orbstack"  # macOS: OrbStack installed
    PODMAN_INSTALLED = "podman"  # Linux: Podman installed (docker-compatible)

    # Errors
    ERROR = "error"  # Unexpected error


# ── ANSI helpers ────────────────────────────────────────────────────────────

def _green(msg: str) -> str:
    return f"\033[92m{msg}\033[0m"

def _red(msg: str) -> str:
    return f"\033[91m{msg}\033[0m"

def _yellow(msg: str) -> str:
    return f"\033[93m{msg}\033[0m"

def _bold(msg: str) -> str:
    return f"\033[1m{msg}\033[0m"

def _prompt(prompt: str, default: str = "", non_interactive: bool = False, ask=None) -> str:
    """Prompt for input with a default value. Returns default on EOF or non-interactive mode.

    Also checks for COLONY_INIT_DEFAULTS env var for scripted defaults.
    Format: COLONY_INIT_DEFAULTS='key1=val1,key2=val2'

    ``ask`` is an injectable input callable (defaults to ``input``) so tests
    can script answers without monkeypatching stdin. UX is unchanged.
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
        val = (ask or input)(f"{prompt}{suffix}: ").strip()
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

def _check_docker() -> tuple[DockerStatus, str]:
    """Check Docker installation and runtime status.

    Returns:
        (status, message) where status is DockerStatus enum
        and message is human-readable details
    """
    system = platform.system().lower()

    # Check for docker binary
    docker_path = shutil.which("docker")

    if not docker_path:
        # Docker binary not in PATH
        # On macOS, check if Docker Desktop app exists
        if system == "darwin":
            if Path("/Applications/Docker.app").exists():
                return DockerStatus.DESKTOP_INSTALLED_NOT_RUNNING, "Docker Desktop installed but not in PATH"
            if Path("/Applications/OrbStack.app").exists():
                return DockerStatus.ORBSTACK_INSTALLED, "OrbStack installed"

        # Check for alternative runtimes
        if shutil.which("colima"):
            return DockerStatus.COLIMA_INSTALLED, "Colima installed (run 'colima start')"

        if system == "linux" and shutil.which("podman"):
            return DockerStatus.PODMAN_INSTALLED, "Podman installed"

        return DockerStatus.NOT_INSTALLED, "Docker not found"

    # Docker binary exists — check if daemon is running
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode == 0:
            return DockerStatus.RUNNING, "Docker daemon running"

        stderr = result.stderr or ""

        # Parse specific error conditions
        if "Cannot connect to the Docker daemon" in stderr:
            # Daemon not running
            # Check if this is Docker Desktop on macOS
            if system == "darwin":
                if Path("/Applications/Docker.app").exists():
                    return DockerStatus.DESKTOP_INSTALLED_NOT_RUNNING, "Docker Desktop installed but not running"

            return DockerStatus.INSTALLED_NOT_RUNNING, "Docker installed but daemon not running"

        if "permission denied" in stderr.lower():
            return DockerStatus.PERMISSION_DENIED, "Permission denied — add user to 'docker' group"

        # Unknown error
        return DockerStatus.ERROR, f"docker info failed: {stderr[:100]}"

    except subprocess.TimeoutExpired:
        return DockerStatus.ERROR, "docker info timed out"
    except Exception as e:
        return DockerStatus.ERROR, str(e)

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
    unstable_patterns = ["/.nvm/", "/.volta/", "/.asdf/", "/.local/share/nvm/", "/.fnm/", "/.local/share/mise/", "/.local/share/rtx/"]
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
        status, _ = _check_docker()
        if status == DockerStatus.RUNNING:
            return True
        time.sleep(3)
    return False


# ── Docker Desktop Detection (macOS) ───────────────────────────────────────────


def _detect_docker_desktop() -> dict:
    """Detect Docker Desktop installation on macOS.

    Returns dict with:
        - installed: bool
        - path: Optional[Path]
        - version: Optional[str]
        - running: bool
    """
    if platform.system() != "Darwin":
        return {"installed": False}

    result = {
        "installed": False,
        "path": None,
        "version": None,
        "running": False,
    }

    app_path = Path("/Applications/Docker.app")
    if not app_path.exists():
        return result

    result["installed"] = True
    result["path"] = app_path

    # Check if running (look for Docker.app process)
    try:
        check = subprocess.run(
            ["pgrep", "-x", "Docker"],
            capture_output=True,
            timeout=2,
        )
        result["running"] = check.returncode == 0
    except Exception:
        pass

    # Get version from Info.plist
    try:
        plist_path = app_path / "Contents" / "Info.plist"
        if plist_path.exists():
            pl_result = subprocess.run(
                ["plutil", "-extract", "CFBundleShortVersionString", "raw", str(plist_path)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if pl_result.returncode == 0:
                result["version"] = pl_result.stdout.strip()
    except Exception:
        pass

    return result


def _start_docker_desktop() -> bool:
    """Start Docker Desktop on macOS.

    Returns:
        True if started successfully
    """
    if platform.system() != "Darwin":
        return False

    app_path = Path("/Applications/Docker.app")
    if not app_path.exists():
        return False

    try:
        subprocess.run(
            ["open", "-a", "Docker"],
            capture_output=True,
            timeout=5,
        )
        return True
    except Exception:
        return False


# ── Linux-Specific Handling ────────────────────────────────────────────────────


def _check_docker_group() -> bool:
    """Check if current user is in docker group (Linux only).

    Returns:
        True if user has docker group membership
    """
    if platform.system() != "Linux":
        return True  # Not applicable

    try:
        import grp
        import os

        docker_group = grp.getgrnam("docker")
        user_groups = os.getgroups()

        return docker_group.gr_gid in user_groups
    except KeyError:
        # docker group doesn't exist
        return False
    except Exception:
        return False


def _suggest_docker_group_fix() -> str:
    """Return instructions for adding user to docker group."""
    user = os.environ.get("USER", "your_username")
    return f"""
  Permission denied — your user is not in the 'docker' group.

  To fix, run:
    sudo usermod -aG docker {user}

  Then log out and log back in for changes to take effect.
  Or run: newgrp docker
"""


def _start_docker_daemon_linux() -> bool:
    """Attempt to start Docker daemon on Linux.

    Returns:
        True if daemon started successfully
    """
    # Try systemd first
    if shutil.which("systemctl"):
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "start", "docker"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass

    # Try service command
    if shutil.which("service"):
        try:
            result = subprocess.run(
                ["sudo", "service", "docker", "start"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass

    return False


def _check_alternative_runtimes() -> tuple[bool, str] | None:
    """Check for alternative Docker-compatible runtimes.

    Returns:
        (runtime_name, start_command) or None
    """
    system = platform.system().lower()

    # macOS alternatives
    if system == "darwin":
        if shutil.which("colima"):
            return ("Colima", "colima start")
        if Path("/Applications/OrbStack.app").exists():
            return ("OrbStack", "open -a OrbStack")

    # Linux alternatives
    if system == "linux":
        if shutil.which("podman"):
            return ("Podman", "podman system service")

    return None


# ── Docker install ──────────────────────────────────────────────────────────


def _install_docker() -> bool:
    """Attempt to install Docker based on platform. Returns True if installed."""
    system = platform.system().lower()

    if system == "linux":
        # Check if Docker is actually already installed but just not detected
        docker_path = shutil.which("docker")
        if docker_path:
            print(f"  ⚠️ Docker binary found at {docker_path} but not working.")
            print("  This might be a configuration issue.")
            return False

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
                user = os.environ.get("USER", "")
                if user:
                    subprocess.run(
                        ["sudo", "usermod", "-aG", "docker", user],
                        capture_output=True, timeout=10,
                    )
                    print("  ✅ Added user to docker group (log out/in or run 'newgrp docker')")
                return True
            else:
                # Check if it failed because Docker is already installed
                combined = (result.stderr + result.stdout).lower()
                if "already installed" in combined or "already the newest" in combined:
                    print("  ✅ Docker already installed")
                    return True
                print(f"  ❌ Docker install failed: {result.stderr[:200]}")
                return False
        except Exception as exc:
            print(f"  ❌ Docker install failed: {exc}")
            return False

    elif system == "darwin":
        # Check if Docker Desktop is already installed
        if Path("/Applications/Docker.app").exists():
            print("  ✅ Docker Desktop already installed")
            print("  Starting Docker Desktop...")
            _start_docker_desktop()
            return True

        # Check if OrbStack is installed
        if Path("/Applications/OrbStack.app").exists():
            print("  ✅ OrbStack already installed")
            print("  Starting OrbStack...")
            subprocess.run(["open", "-a", "OrbStack"], capture_output=True)
            return True

        # Check for Homebrew
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

                # Check if it failed because already installed
                combined = (result.stdout + result.stderr).lower()
                if "already installed" in combined:
                    print("  ✅ Docker Desktop already installed")
                    print("  Opening Docker Desktop...")
                    _start_docker_desktop()
                    return True

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


# ── Docker Setup Handler ───────────────────────────────────────────────────────


def _handle_docker_setup(non_interactive: bool = False) -> bool:
    """Handle Docker detection and setup.

    Returns:
        True if Docker is available and running
    """
    system = platform.system().lower()

    # Check Docker status
    status, message = _check_docker()

    print(_bold("Step 4: Docker"))
    print()

    if status == DockerStatus.RUNNING:
        print(f"  ✅ Docker is running")
        print()
        return True

    elif status == DockerStatus.DESKTOP_INSTALLED_NOT_RUNNING:
        # macOS: Docker Desktop installed but not running
        print(f"  Docker Desktop is installed but not running.")
        print()

        start = _prompt("  Start Docker Desktop now? [Y/n]", "Y", non_interactive)
        if start.lower() in ("y", "yes", ""):
            print("  Starting Docker Desktop...")
            if _start_docker_desktop():
                print("  Waiting for Docker daemon...")
                if _wait_for_docker():
                    print("  ✅ Docker is running")
                    print()
                    return True
                else:
                    print("  ⚠️ Docker Desktop started but daemon not ready yet.")
                    print("  Wait for Docker to fully start, then re-run 'colony init'.")
                    return False
            else:
                print("  ❌ Failed to start Docker Desktop")
                print("  Open Docker Desktop from Applications manually.")
                return False
        else:
            print("  Start Docker Desktop manually and re-run 'colony init'.")
            return False

    elif status == DockerStatus.INSTALLED_NOT_RUNNING:
        # Docker installed but daemon not running
        print(f"  Docker is installed but the daemon is not running.")
        print()

        if system == "linux":
            start = _prompt("  Start Docker daemon? [Y/n]", "Y", non_interactive)
            if start.lower() in ("y", "yes", ""):
                print("  Starting Docker daemon (may require sudo)...")
                if _start_docker_daemon_linux():
                    print("  Waiting for Docker daemon...")
                    if _wait_for_docker():
                        print("  ✅ Docker is running")
                        print()
                        return True
                    else:
                        print("  ⚠️ Daemon started but not ready yet.")
                        return False
                else:
                    print("  ❌ Failed to start Docker daemon")
                    print("  Try: sudo systemctl start docker")
                    return False
        else:
            print("  Start the Docker daemon and re-run 'colony init'.")
            return False

    elif status == DockerStatus.PERMISSION_DENIED:
        # Linux: user not in docker group
        print(_suggest_docker_group_fix())
        return False

    elif status == DockerStatus.COLIMA_INSTALLED:
        # macOS: Colima installed
        print("  Colima is installed but not running.")
        print()
        start = _prompt("  Start Colima now? [Y/n]", "Y", non_interactive)
        if start.lower() in ("y", "yes", ""):
            print("  Starting Colima...")
            try:
                subprocess.run(["colima", "start"], check=True, timeout=60)
                if _wait_for_docker():
                    print("  ✅ Colima is running")
                    print()
                    return True
            except subprocess.CalledProcessError as e:
                print(f"  ❌ Failed to start Colima: {e}")
                return False
            except Exception as e:
                print(f"  ❌ Failed to start Colima: {e}")
                return False
        else:
            print("  Run 'colima start' and re-run 'colony init'.")
            return False

    elif status == DockerStatus.ORBSTACK_INSTALLED:
        # macOS: OrbStack installed
        print("  OrbStack is installed but not running.")
        print()
        start = _prompt("  Start OrbStack now? [Y/n]", "Y", non_interactive)
        if start.lower() in ("y", "yes", ""):
            print("  Starting OrbStack...")
            try:
                subprocess.run(["open", "-a", "OrbStack"], check=True, timeout=5)
                if _wait_for_docker():
                    print("  ✅ OrbStack is running")
                    print()
                    return True
            except Exception as e:
                print(f"  ❌ Failed to start OrbStack: {e}")
                return False
        else:
            print("  Open OrbStack and re-run 'colony init'.")
            return False

    elif status == DockerStatus.PODMAN_INSTALLED:
        # Linux: Podman installed
        print("  Podman is installed (Docker-compatible).")
        print("  Colony can use Podman's Docker socket compatibility.")
        print()

        # Check if podman socket is running
        socket_path = Path.home() / ".local" / "share" / "containers" / "podman" / "machine" / "cni" / "podman.sock"
        if socket_path.exists():
            print("  ✅ Podman socket available")
            return True

        start = _prompt("  Start Podman socket? [Y/n]", "Y", non_interactive)
        if start.lower() in ("y", "yes", ""):
            print("  Starting Podman socket...")
            try:
                subprocess.run(["podman", "system", "service", "--time=0"], check=True, timeout=10)
                return True
            except Exception as e:
                print(f"  ❌ Failed to start Podman socket: {e}")
                return False
        return False

    elif status == DockerStatus.NOT_INSTALLED:
        # Docker not installed
        print("  Docker is required for Neo4j (graph memory).")
        print()

        # Check for alternatives first
        alt_runtime = _check_alternative_runtimes()
        if alt_runtime:
            name, cmd = alt_runtime
            print(f"  Alternative runtime detected: {name}")
            print(f"  Run '{cmd}' to start, then re-run 'colony init'.")
            return False

        install = _prompt("  Install Docker now? [Y/n]", "Y", non_interactive)
        if install.lower() in ("y", "yes", ""):
            if _install_docker():
                print("  Waiting for Docker daemon...")
                if _wait_for_docker():
                    print("  ✅ Docker is running")
                    print()
                    return True
                else:
                    print("  ⚠️ Docker installed but daemon not reachable yet.")
                    print("  Start Docker and re-run 'colony init'.")
            else:
                print()
                print("  Install Docker manually: https://docs.docker.com/get-docker/")
        else:
            print("  Skipping Docker — Neo4j will not be available.")
        return False

    else:
        # Error or unknown status
        print(f"  ⚠️ Docker check failed: {message}")
        print()
        print("  Ensure Docker is installed and running, then re-run 'colony init'.")
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

    # Run Neo4j container. The credential travels via the process env
    # (`-e NEO4J_AUTH` with no value makes docker read it from there) so
    # the password never appears in argv where `ps` exposes it.
    try:
        cmd = [
            "docker", "run", "-d",
            "--name", "neo4j-colony",
            "-p", "7474:7474",
            "-p", "7687:7687",
            "-e", "NEO4J_AUTH",
            "-v", f"{neo4j_data}:/data",
            "neo4j:5.15"
        ]
        env = {**os.environ, "NEO4J_AUTH": f"neo4j/{neo4j_password}"}
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        if result.returncode != 0:
            print(f"    ⚠️ docker run failed: {result.stderr.strip()}")
            return False
        return True
    except Exception as exc:
        print(f"    ⚠️ docker run failed: {exc}")
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


def _install_hermes_addons(hermes_home: Path, ops_src: Path) -> None:
    """Install the GENERIC host-side Colony<->Hermes ops add-ons — doctor, restart
    runner, pre-restart summary — and schedule the doctor. Not specific to any
    agent; these make any Hermes+Colony integration self-validating and resilient.
    Idempotent."""
    if not ops_src.exists():
        print("    ⚠️  Ops add-ons not found (skipping doctor / restart add-ons)")
        return
    scripts_dst = hermes_home / "scripts"
    scripts_dst.mkdir(parents=True, exist_ok=True)
    for f in ("colony-doctor.py", "colony-doctor-cron.sh",
              "hermes-gateway-restart-runner.sh", "pre-restart-summary.py",
              "colony-activity-monitor.py"):
        src = ops_src / f
        if src.exists():
            shutil.copy2(src, scripts_dst / f)
            try:
                os.chmod(scripts_dst / f, 0o755)
            except OSError:
                pass
    print(f"    ✅ Ops add-ons      →  {scripts_dst}")

    if sys.platform != "darwin":
        print("    ℹ️  On non-macOS, schedule the doctor via cron:")
        print("       0 */6 * * * ~/.hermes/scripts/colony-doctor-cron.sh")
        return
    la = Path.home() / "Library" / "LaunchAgents"
    if not la.exists():
        return
    import plistlib
    venv_py = str(hermes_home / "hermes-agent" / "venv" / "bin" / "python3")
    logs = hermes_home / "logs"
    # Generate launchd plists with THIS host's paths (portable; not copied hardcoded).
    plists = {
        "ai.aevonix.colony-doctor": {
            "ProgramArguments": ["/bin/bash", str(scripts_dst / "colony-doctor-cron.sh")],
            "RunAtLoad": True, "StartInterval": 21600,
            "StandardOutPath": str(logs / "colony-doctor.out"),
            "StandardErrorPath": str(logs / "colony-doctor.err"),
        },
        "ai.hermes.activity-monitor": {
            "ProgramArguments": [venv_py, str(scripts_dst / "colony-activity-monitor.py")],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
            "StandardOutPath": str(logs / "activity-monitor.out.log"),
            "StandardErrorPath": str(logs / "activity-monitor.err.log"),
        },
    }
    for label, body in plists.items():
        body["Label"] = label
        dst = la / f"{label}.plist"
        try:
            with open(dst, "wb") as fh:
                plistlib.dump(body, fh)
            print(f"    ✅ launchd          →  {dst.name}")
            print(f"       enable: launchctl bootstrap gui/$(id -u) {dst}")
        except Exception as exc:
            print(f"    ⚠️  launchd {label} skipped: {exc}")


def _run_colony_doctor(hermes_home: Path) -> None:
    """Run the integration doctor as a post-setup validation gate."""
    doctor = hermes_home / "scripts" / "colony-doctor.py"
    if not doctor.exists():
        return
    hermes_py = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
    py = str(hermes_py) if hermes_py.exists() else sys.executable
    print("  Validating integration (colony-doctor)...")
    try:
        r = subprocess.run([py, str(doctor)], capture_output=True, text=True, timeout=40)
        result = [l for l in r.stdout.splitlines() if l.startswith("RESULT")]
        fails = [l for l in r.stdout.splitlines() if l.strip().startswith("❌")]
        print("    " + (result[-1] if result else f"doctor exit {r.returncode}"))
        for l in fails[:6]:
            print("    " + l.strip())
    except Exception as exc:
        print(f"    ⚠️  doctor run skipped: {exc}")


def _setup_hermes_plugin(api_key: str, sidecar_url: str, non_interactive: bool = False, contact_id: str = "") -> bool:
    """Configure Colony as a Hermes MemoryProvider plugin.

    Installs plugin files from the Colony repo into ~/.hermes/plugins/
    and updates ~/.hermes/config.yaml. Works on Linux, macOS, and WSL.
    """
    hermes_home = Path.home() / ".hermes"
    hermes_config = hermes_home / "config.yaml"

    # Find plugin source files relative to this module (in the repo).
    # plugins/colony-memory is the SINGLE canonical memory provider (the
    # former hermes-memory and hermes-plugin/memory_provider copies were
    # consolidated into it).
    colony_repo = Path(__file__).resolve().parents[2]  # colony_sidecar/ -> sidecar/ -> repo-root/
    mem_src = colony_repo / "plugins" / "colony-memory"
    ctx_src = colony_repo / "plugins" / "hermes-context"
    gen_src = colony_repo / "plugins" / "hermes-plugin"

    # Check if source files exist
    if not mem_src.exists():
        print("  ⚠️ Colony plugin source files not found")
        print(f"     Expected: {mem_src}")
        print("     Install manually: git clone https://github.com/Aevonix/ColonyAI.git")
        return False

    print("  Installing Colony Hermes plugins...")

    # Install memory provider (same target install.sh uses; this is the
    # layout Hermes loads for memory.provider = "colony")
    mem_dst = hermes_home / "plugins" / "colony-memory"
    mem_dst.mkdir(parents=True, exist_ok=True)
    for f in ["__init__.py", "provider.py", "cli.py", "plugin.yaml", "SKILL.md"]:
        src = mem_src / f
        if src.exists():
            shutil.copy2(src, mem_dst / f)
    print(f"    ✅ Memory provider  →  {mem_dst}")

    # Install context engine
    if ctx_src.exists():
        ctx_dst = hermes_home / "plugins" / "context_engine" / "colony"
        ctx_dst.mkdir(parents=True, exist_ok=True)
        for f in ["__init__.py", "plugin.yaml"]:
            src = ctx_src / f
            if src.exists():
                shutil.copy2(src, ctx_dst / f)
        print(f"    ✅ Context engine   →  {ctx_dst}")

    # Install general plugin
    if gen_src.exists():
        gen_dst = hermes_home / "plugins" / "colony"
        gen_dst.mkdir(parents=True, exist_ok=True)
        for f in ["__init__.py", "client.py", "events.py", "slash.py", "plugin.yaml"]:
            src = gen_src / f
            if src.exists():
                shutil.copy2(src, gen_dst / f)
        print(f"    ✅ General plugin   →  {gen_dst}")

    # Generic host-side ops add-ons + scheduled doctor (any agent).
    _install_hermes_addons(hermes_home, gen_src / "ops")

    # Write/update Hermes config.yaml
    print("  Configuring Hermes config...")
    _write_hermes_config(hermes_config, api_key, sidecar_url, contact_id)
    print(f"    ✅ Config written   →  {hermes_config}")

    # Verify sidecar is reachable
    try:
        import httpx
        resp = httpx.get(f"{sidecar_url}/v1/host/health", timeout=3)
        if resp.status_code == 200:
            caps = resp.json().get("capabilities", [])
            print(f"    ✅ Sidecar healthy  —  {len(caps)} capabilities")
        else:
            print(f"    ⚠️  Sidecar returned HTTP {resp.status_code}")
    except Exception as exc:
        print(f"    ⚠️  Sidecar not reachable: {exc}")
        print("       Start it with: colony start")

    _run_colony_doctor(hermes_home)

    print()
    print("  Hermes integration complete!")
    print()
    print("  Restart Hermes to load the Colony plugin:")
    print("    hermes restart")
    print()
    print("  Then verify:")
    print("    hermes colony status")
    print()
    print("  Plugin docs:")
    print("    https://github.com/Aevonix/ColonyAI/blob/main/plugins/colony-memory/SKILL.md")
    return True


def _write_hermes_config(config_path: Path, api_key: str, sidecar_url: str, contact_id: str) -> None:
    """Write or update ~/.hermes/config.yaml with Colony settings.

    Uses safe string manipulation — no pyyaml required. Preserves
    existing user config and only updates Colony-related sections.
    """
    # Default config if file doesn't exist
    default_lines = [
        "# Hermes Configuration",
        "# Generated by Colony setup wizard",
        "",
    ]

    existing_lines = []
    if config_path.exists():
        existing_lines = config_path.read_text().splitlines()

    if not existing_lines:
        existing_lines = default_lines

    # Parse existing lines to find sections
    in_memory = False
    in_plugins = False
    in_context_engine = False
    memory_start = -1
    memory_end = -1
    plugins_start = -1
    plugins_end = -1
    context_engine_start = -1
    context_engine_end = -1

    for i, line in enumerate(existing_lines):
        stripped = line.strip()
        if stripped.startswith("memory:"):
            in_memory = True
            memory_start = i
            continue
        if stripped.startswith("plugins:"):
            in_plugins = True
            plugins_start = i
            in_memory = False
            if memory_start >= 0 and memory_end < 0:
                memory_end = i
            continue
        if stripped.startswith("context_engine:"):
            in_context_engine = True
            context_engine_start = i
            in_memory = False
            in_plugins = False
            if memory_start >= 0 and memory_end < 0:
                memory_end = i
            if plugins_start >= 0 and plugins_end < 0:
                plugins_end = i
            continue
        if in_memory and stripped and not stripped.startswith("#") and not line.startswith(" ") and not line.startswith("  "):
            memory_end = i
            in_memory = False
        if in_plugins and stripped and not stripped.startswith("#") and not line.startswith(" ") and not line.startswith("  "):
            plugins_end = i
            in_plugins = False
        if in_context_engine and stripped and not stripped.startswith("#") and not line.startswith(" ") and not line.startswith("  "):
            context_engine_end = i
            in_context_engine = False

    # Close any open sections at EOF
    if memory_start >= 0 and memory_end < 0:
        memory_end = len(existing_lines)
    if plugins_start >= 0 and plugins_end < 0:
        plugins_end = len(existing_lines)
    if context_engine_start >= 0 and context_engine_end < 0:
        context_engine_end = len(existing_lines)

    # Extract non-Colony plugins from existing plugins section
    other_plugins: list[str] = []
    if plugins_start >= 0:
        for line in existing_lines[plugins_start + 1:plugins_end]:
            stripped = line.strip()
            # Keep lines that aren't the colony plugin
            if stripped and not stripped.startswith("colony:") and not stripped.startswith("#"):
                # Only keep top-level plugin keys (2-space indent)
                if line.startswith("  ") and not line.startswith("    "):
                    other_plugins.append(line)

    # Build new lines
    new_lines = []

    contact_id_str = contact_id or "default"
    # Memory section
    memory_lines = [
        "memory:",
        "  provider: colony",
        "  config:",
        f'    url: "{sidecar_url}"',
        '    api_key: "${COLONY_API_KEY}"',
        f'    contact_id: "{contact_id_str}"',
    ]

    # Plugins section
    plugins_lines = [
        "plugins:",
        "  colony:",
        f'    url: "{sidecar_url}"',
        '    api_key: "${COLONY_API_KEY}"',
        f'    contact_id: "{contact_id_str}"',
    ]
    plugins_lines.extend(other_plugins)

    # Context engine section
    context_engine_lines = [
        "context_engine: colony",
    ]

    # Assemble output preserving non-conflicting sections
    added_memory = False
    added_plugins = False
    added_context_engine = False

    i = 0
    while i < len(existing_lines):
        line = existing_lines[i]
        stripped = line.strip()

        # Skip old memory section
        if memory_start >= 0 and memory_start <= i < memory_end:
            if not added_memory:
                new_lines.extend(memory_lines)
                added_memory = True
            i = memory_end
            continue

        # Skip old plugins section
        if plugins_start >= 0 and plugins_start <= i < plugins_end:
            if not added_plugins:
                new_lines.extend(plugins_lines)
                added_plugins = True
            i = plugins_end
            continue

        # Skip old context_engine line
        if context_engine_start >= 0 and context_engine_start <= i < context_engine_end:
            if not added_context_engine:
                new_lines.extend(context_engine_lines)
                added_context_engine = True
            i = context_engine_end
            continue

        new_lines.append(line)
        i += 1

    # Add any missing sections at the end
    if not added_memory:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.extend(memory_lines)
    if not added_plugins:
        new_lines.append("")
        new_lines.extend(plugins_lines)
    if not added_context_engine:
        new_lines.append("")
        new_lines.extend(context_engine_lines)

    config_path.write_text("\n".join(new_lines) + "\n")


def _write_env(env_path: Path, values: dict[str, str]) -> None:
    lines = [
        "# Colony Sidecar Configuration",
        "# Generated by 'colony init'",
        "#",
        "# Colony is a sidecar — it gets LLM credentials from its host",
        "# (Hermes, MCP harnesses, etc.) at runtime via POST /v1/host/configure.",
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


# ── LLM host config helpers (.colony-llm-config.json) ──────────────────────

# Providers that speak the OpenAI-compatible API. LiteLLM routes these via
# OPENAI_API_BASE, which must point at the ``/v1`` API root.
OPENAI_COMPAT_PROVIDERS = frozenset({
    "zai", "local", "custom", "lmstudio", "vllm", "openai",
    "openai-compatible", "openai_compatible",
})


def normalize_llm_base_url(url: str, provider: str) -> tuple[str, bool]:
    """Ensure ``baseUrl`` ends with ``/v1`` for OpenAI-compatible providers.

    A bare ``host:port`` base URL silently 404s on chat completions because
    LiteLLM appends ``/chat/completions`` directly. Returns ``(url, changed)``;
    non-OpenAI-compatible providers (e.g. ollama, anthropic) pass through.
    """
    if not url:
        return url, False
    if (provider or "").strip().lower() not in OPENAI_COMPAT_PROVIDERS:
        return url, False
    stripped = url.rstrip("/")
    if stripped.endswith("/v1"):
        return url, False
    return stripped + "/v1", True


def ensure_api_key(cfg: dict) -> tuple[dict, bool]:
    """Never persist an empty ``apiKey`` for OpenAI-compatible providers.

    LiteLLM requires a non-empty api_key even for keyless local servers
    (vLLM, LM Studio, llama.cpp, ...) — an empty string breaks the auth
    header. Defaults to ``"local-no-key"``. Returns ``(cfg, changed)``;
    the input dict is never mutated.
    """
    provider = (cfg.get("provider") or "").strip().lower()
    if provider in OPENAI_COMPAT_PROVIDERS and not (cfg.get("apiKey") or "").strip():
        fixed = dict(cfg)
        fixed["apiKey"] = "local-no-key"
        return fixed, True
    return cfg, False


def apply_llm_config_fixes(cfg: dict) -> tuple[dict, list[str]]:
    """Apply both LLM host-config footgun fixes. Returns ``(cfg, notes)``."""
    notes: list[str] = []
    fixed = dict(cfg)
    url, changed = normalize_llm_base_url(fixed.get("baseUrl", ""), fixed.get("provider", ""))
    if changed:
        fixed["baseUrl"] = url
        notes.append(f"baseUrl did not end with /v1 — normalized to {url}")
    fixed, changed = ensure_api_key(fixed)
    if changed:
        notes.append(
            'apiKey was empty — set to "local-no-key" '
            "(LiteLLM requires a non-empty value even for keyless servers)"
        )
    return fixed, notes


def write_llm_host_config(path: Path, cfg: dict) -> tuple[dict, list[str]]:
    """Persist an LLM host config, applying both footgun fixes at the source.

    Every wizard write of ``.colony-llm-config.json`` must go through here.
    Returns the (possibly fixed) config and the human-readable fix notes.
    """
    import json
    fixed, notes = apply_llm_config_fixes(cfg)
    path.write_text(json.dumps(fixed, indent=2))
    return fixed, notes


def repair_persisted_llm_config() -> list[str]:
    """Quiet variant of :func:`_normalize_persisted_llm_config` for
    ``colony doctor --fix``: applies the same footgun fixes in place and
    returns the fix notes (empty when nothing needed changing)."""
    import json
    from colony_sidecar import get_state_dir

    config_path = get_state_dir() / ".colony-llm-config.json"
    if not config_path.exists():
        return []
    cfg = json.loads(config_path.read_text())
    fixed, notes = apply_llm_config_fixes(cfg)
    if notes:
        write_llm_host_config(config_path, fixed)
    return notes


def _normalize_persisted_llm_config() -> None:
    """Fix footguns in an already-persisted ``.colony-llm-config.json``, if any."""
    import json
    try:
        from colony_sidecar import get_state_dir
        config_path = get_state_dir() / ".colony-llm-config.json"
        if not config_path.exists():
            print("  ⚪ No persisted LLM host config yet (written on first host connect)")
            return
        cfg = json.loads(config_path.read_text())
        fixed, notes = apply_llm_config_fixes(cfg)
        if notes:
            write_llm_host_config(config_path, fixed)
            for note in notes:
                print(f"  ⚠️ {note}")
            print(f"  ✅ LLM host config updated ({config_path})")
        else:
            print(f"  ✅ LLM host config OK ({config_path})")
    except Exception as exc:
        print(f"  ⚪ LLM config check skipped: {exc}")


# ── Autonomy & approvals step ───────────────────────────────────────────────

# Gateways the owner can register handles for in the wizard.
OWNER_HANDLE_GATEWAYS = (
    "whatsapp", "telegram", "imessage", "email", "sms", "signal", "discord", "slack",
)

# Platforms that can act as the home channel for proactive delivery.
HOME_CHANNEL_PLATFORMS = ("whatsapp", "telegram", "discord", "slack", "signal")


async def build_owner_contact(
    store,
    display_name: str,
    handles: list[tuple[str, str]] | None = None,
    *,
    trust_tier: str = "inner_circle",
    interaction_allowed: bool = True,
    import_source: str = "wizard",
) -> str:
    """Create the owner contact (plus handles) and return its contact_id.

    The first handle becomes primary. A handle already owned by another
    contact is skipped rather than failing the owner record — identity
    fail-closed needs the owner cid to exist either way.
    """
    contact = await store.create(
        display_name=display_name,
        trust_tier=trust_tier,
        interaction_allowed=interaction_allowed,
        import_source=import_source,
    )
    for i, (gateway, address) in enumerate(handles or []):
        try:
            await store.add_handle(
                contact.contact_id,
                gateway=gateway,
                address=address,
                is_primary=(i == 0),
                source="wizard",
                verified=True,
            )
        except ValueError:
            # Address already assigned to another contact — skip it.
            continue
    return contact.contact_id


def collect_owner_handles(ask=None, non_interactive: bool = False) -> list[tuple[str, str]]:
    """Interactive gateway/address loop for the owner's handles."""
    handles: list[tuple[str, str]] = []
    if non_interactive:
        return handles
    print("  Add ways to reach you (leave gateway blank to finish).")
    print(f"  Gateways: {', '.join(OWNER_HANDLE_GATEWAYS)}")
    while True:
        gateway = _prompt("  Gateway (blank to finish)", "", non_interactive, ask=ask).strip().lower()
        if not gateway:
            break
        if gateway not in OWNER_HANDLE_GATEWAYS:
            print(f"  Invalid gateway. Choose one of: {', '.join(OWNER_HANDLE_GATEWAYS)}")
            continue
        address = _prompt(f"  {gateway} address", "", non_interactive, ask=ask).strip()
        if not address:
            print("  No address given — skipped.")
            continue
        handles.append((gateway, address))
    return handles


def _run_owner_identity(
    values: dict[str, str],
    existing: dict[str, str],
    non_interactive: bool = False,
    ask=None,
) -> dict[str, str]:
    """Owner identity sub-step. Returns env updates ({} on failure)."""
    from colony_sidecar.contacts.config import ContactsConfig
    from colony_sidecar.contacts.store import SQLiteContactStore

    print("  Colony fails closed on identity: owner-exclusion filters, check-ins")
    print("  and outreach authorization all need to know who you are. Without an")
    print("  owner contact, autonomous outreach stays disabled.")
    print()

    # Make sure ContactsConfig.from_env() resolves to the wizard's DB path.
    if values.get("COLONY_CONTACTS_DB") and not os.environ.get("COLONY_CONTACTS_DB"):
        os.environ["COLONY_CONTACTS_DB"] = values["COLONY_CONTACTS_DB"]
    config = ContactsConfig.from_env()

    async def _get(cid: str):
        store = SQLiteContactStore(config=config)
        await store.connect()
        try:
            return await store.get(cid)
        finally:
            await store.close()

    # Idempotent re-run: keep an owner that still resolves.
    prior_cid = (
        existing.get("COLONY_OWNER_CONTACT_ID")
        or os.environ.get("COLONY_OWNER_CONTACT_ID", "")
    ).strip()
    if prior_cid:
        try:
            prior = asyncio.run(_get(prior_cid))
        except Exception:
            prior = None
        if prior is not None:
            print(f"  Owner contact already configured: {prior.display_name} ({prior_cid})")
            keep = _prompt("  Keep this owner? [Y/n]", "Y", non_interactive, ask=ask)
            if keep.strip().lower() in ("y", "yes", ""):
                print(f"  ✅ Owner contact kept: {prior_cid}")
                return {"COLONY_OWNER_CONTACT_ID": prior_cid}
        else:
            print(f"  ⚠️ COLONY_OWNER_CONTACT_ID={prior_cid} no longer resolves — recreating.")

    display_name = _prompt(
        "  Your name (what Colony calls its owner)",
        os.environ.get("USER", ""), non_interactive, ask=ask,
    ).strip() or os.environ.get("USER", "owner")
    handles = collect_owner_handles(ask=ask, non_interactive=non_interactive)

    async def _create() -> str:
        store = SQLiteContactStore(config=config)
        await store.connect()
        try:
            return await build_owner_contact(store, display_name, handles)
        finally:
            await store.close()

    try:
        cid = asyncio.run(_create())
    except Exception as exc:
        print(f"  ⚠️ Could not create owner contact: {exc}")
        print("     Set COLONY_OWNER_CONTACT_ID in .env manually once resolved.")
        return {}
    print(f"  ✅ Owner contact created: {display_name} ({cid})")
    if handles:
        print(f"     Handles: {', '.join(f'{g}:{a}' for g, a in handles)}")
    return {"COLONY_OWNER_CONTACT_ID": cid}


def collect_autonomy_env(
    existing: dict[str, str],
    ask=None,
    non_interactive: bool = False,
) -> dict[str, str]:
    """Collect approval-policy, autonomy-gate and home-channel env values.

    Pure prompt-assembly: takes the existing env (so re-runs default to the
    current values) and an injectable ``ask`` callable; returns the env
    updates the wizard persists.
    """
    updates: dict[str, str] = {}

    # ── Approval policy ──
    print("  How much should Colony check in before acting?")
    print("    [1] strict    — every mutating or outbound agent action waits")
    print("                    for your approval (default, safest)")
    print("    [2] graduated — only destructive actions and outreach to people")
    print("                    you haven't authorized wait for you; everything")
    print("                    else runs with an audit trail")
    current_policy = (existing.get("COLONY_APPROVAL_POLICY", "") or "").strip().lower()
    default_choice = "2" if current_policy == "graduated" else "1"
    choice = _prompt("  Approval policy [1/2]", default_choice, non_interactive, ask=ask).strip().lower()
    policy = "graduated" if choice in ("2", "graduated") else "strict"
    updates["COLONY_APPROVAL_POLICY"] = policy
    print(f"  ✅ Approval policy: {policy}")
    print()

    # ── Autonomy gates ──
    def _gate(env_key: str, question: str, blurb: str) -> None:
        enabled = (existing.get(env_key, "") or "").strip().lower() == "true"
        default = "Y" if enabled else "N"
        hint = "[Y/n]" if enabled else "[y/N]"
        print(f"  {blurb}")
        answer = _prompt(f"  {question} {hint}", default, non_interactive, ask=ask)
        on = answer.strip().lower() in ("y", "yes", "true")
        updates[env_key] = "true" if on else "false"
        print(f"  {'✅ Enabled' if on else '⚪ Disabled'}")
        print()

    _gate(
        "COLONY_ENABLE_SKILL_SYNTHESIS",
        "Enable skill synthesis?",
        "Successful novel agent work is captured as draft skills you approve.",
    )

    # ── Autonomy posture (one preset drives all fourteen mode flags) ──
    print("  How autonomous should this Colony be?")
    print("    [1] passive     — observe and remember only; nothing thinks or acts")
    print("    [2] calibration — everything runs in shadow and EARNS live autonomy")
    print("                      through its real track record (recommended)")
    print("    [3] autonomous  — thinking, projects, beliefs, workers, connectors")
    print("                      run live, still bounded by approvals, boundaries,")
    print("                      and the immutable floor")
    print("  (Individual COLONY_*_MODE env vars always override the preset.)")
    current_preset = (existing.get("COLONY_AUTONOMY_PRESET", "") or "").strip().lower()
    preset_default = {"passive": "1", "calibration": "2", "autonomous": "3"}.get(
        current_preset, "2")
    preset_choice = _prompt(
        "  Autonomy preset [1/2/3]", preset_default, non_interactive, ask=ask,
    ).strip().lower()
    preset = {"1": "passive", "2": "calibration", "3": "autonomous",
              "passive": "passive", "calibration": "calibration",
              "autonomous": "autonomous"}.get(preset_choice, "calibration")
    updates["COLONY_AUTONOMY_PRESET"] = preset
    print(f"  ✅ Autonomy preset: {preset}")
    if preset == "calibration":
        print("     Shadow runs build a track record; the trust engine graduates")
        print("     each capability class to ask-first and then act-first, and")
        print("     notifies you on every graduation.")
    print()

    # ── Home channel ──
    print("  Which platform reaches you for proactive updates?")
    print(f"  Options: {', '.join(HOME_CHANNEL_PLATFORMS)}, none")
    print("  ('none' means initiatives queue for your review but are never pushed)")
    existing_platform = ""
    existing_chat = ""
    for p in HOME_CHANNEL_PLATFORMS:
        if existing.get(f"{p.upper()}_HOME_CHANNEL"):
            existing_platform, existing_chat = p, existing[f"{p.upper()}_HOME_CHANNEL"]
            break
    while True:
        platform_choice = _prompt(
            "  Home channel platform", existing_platform or "none", non_interactive, ask=ask
        ).strip().lower()
        if platform_choice in HOME_CHANNEL_PLATFORMS or platform_choice in ("", "none"):
            break
        print(f"  Invalid platform. Choose one of: {', '.join(HOME_CHANNEL_PLATFORMS)}, none")
    if platform_choice in ("", "none"):
        print("  ⚪ No home channel — initiatives will queue but never be pushed")
    else:
        default_chat = existing_chat if platform_choice == existing_platform else ""
        chat_id = _prompt(
            f"  {platform_choice} channel/contact id", default_chat, non_interactive, ask=ask
        ).strip()
        if chat_id:
            updates[f"{platform_choice.upper()}_HOME_CHANNEL"] = chat_id
            print(f"  ✅ Home channel: {platform_choice} → {chat_id}")
        else:
            print("  ⚪ No channel id — initiatives will queue but never be pushed")

    return updates


def run_autonomy_step(
    values: dict[str, str],
    existing: dict[str, str],
    non_interactive: bool = False,
    ask=None,
) -> dict[str, str]:
    """Step 8: Autonomy & approvals.

    Owner identity, approval policy, thinking/synthesis gates, home channel,
    plus the LLM host-config footgun check. Returns the env updates to merge
    into the values the wizard persists.
    """
    print(_bold("Step 8: Autonomy & approvals"))
    print()

    updates: dict[str, str] = {}
    updates.update(_run_owner_identity(values, existing, non_interactive, ask=ask))
    print()
    updates.update(collect_autonomy_env({**existing, **values}, ask=ask, non_interactive=non_interactive))
    print()

    # LLM host config footgun check (baseUrl /v1 + non-empty apiKey).
    _normalize_persisted_llm_config()
    return updates


# ── Scheduled agent workers (v0.20.0) ───────────────────────────────────────
#
# The agent-side workers (queue worker + skills sync) only do their job
# when something actually schedules them. The wizard offers to install
# crontab entries; the pure helpers below (command/line construction and
# crontab merging) are extracted for tests.

#: The schedulable agent-side workers: console-script name, module path
#: (for the `python -m` fallback) and cron schedule.
WORKER_SPECS = (
    {
        "name": "colony-queue-worker",
        "module": "colony_sidecar.workers.queue_worker",
        "schedule": "*/5 * * * *",
        "blurb": "claims approved agent_action jobs every 5 minutes and hands "
                 "them to your agent (without it, auto-approved jobs sit QUEUED forever)",
    },
    {
        "name": "colony-skills-sync",
        "module": "colony_sidecar.workers.skills_sync",
        "schedule": "0 9 * * *",
        "blurb": "reports your agent's installed skill index to Colony once a "
                 "day so it proposes work the agent can actually do",
    },
)


def build_worker_command(name: str, module: str, which=shutil.which, python: str = "") -> str:
    """Command for one worker: the console script when installed on PATH,
    else ``<python> -m <module>`` (works from any environment where the
    package is importable)."""
    script = which(name)
    if script:
        return script
    return f"{python or sys.executable} -m {module}"


def build_cron_lines(
    env_file: str,
    log_dir: str,
    workdir: str = "",
    which=shutil.which,
    python: str = "",
) -> list[str]:
    """Build the crontab lines for both workers.

    Each line sources the wizard's .env (``set -a`` so every var is
    exported to the worker), runs from a stable directory, and appends
    stdout+stderr to ``<log_dir>/cron-<name>.log``.
    """
    workdir = workdir or str(Path.home())
    lines: list[str] = []
    for spec in WORKER_SPECS:
        cmd = build_worker_command(spec["name"], spec["module"], which=which, python=python)
        prefix = f"cd {workdir} && set -a; . {env_file}; set +a;"
        log = f"{log_dir}/cron-{spec['name']}.log"
        lines.append(f"{spec['schedule']} {prefix} {cmd} >> {log} 2>&1")
    return lines


def merge_crontab(existing: str, new_lines: list[str]) -> tuple[str, list[str]]:
    """Merge worker cron lines into an existing crontab (idempotent).

    A new line is skipped when the existing crontab already references
    that worker in either form (console-script name or ``-m`` module
    path), so re-running the wizard never duplicates entries and never
    clobbers hand-tuned schedules. Existing content is preserved
    verbatim. Returns ``(merged_text, lines_actually_added)``.
    """
    existing = existing or ""
    added: list[str] = []
    for line in new_lines:
        markers = [line.strip()]
        for spec in WORKER_SPECS:
            if spec["name"] in line or spec["module"] in line:
                markers = [spec["name"], spec["module"]]
                break
        if any(m in existing for m in markers):
            continue
        added.append(line)
    if not added:
        return existing, []
    merged = existing.rstrip("\n")
    merged = (merged + "\n" if merged else "") + "\n".join(added) + "\n"
    return merged, added


def install_cron_jobs(lines: list[str], run=subprocess.run) -> list[str]:
    """Merge ``lines`` into the user crontab via ``crontab -l`` / ``crontab -``.

    Returns the lines actually added ([] when everything was already
    installed). Raises ``RuntimeError`` when the write fails. ``run`` is
    injectable for tests.
    """
    read = run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    # `crontab -l` exits non-zero with "no crontab for <user>" — treat as empty.
    existing = read.stdout if read.returncode == 0 else ""
    merged, added = merge_crontab(existing, lines)
    if not added:
        return []
    write = run(["crontab", "-"], input=merged, capture_output=True, text=True, timeout=10)
    if write.returncode != 0:
        raise RuntimeError(
            f"crontab write failed: {(write.stderr or write.stdout or '').strip()}"
        )
    return added


def run_workers_step(
    env_path: Path,
    non_interactive: bool = False,
    ask=None,
    run=subprocess.run,
    which=shutil.which,
) -> None:
    """Step 10e: Scheduled agent workers.

    Explains the two cron-driven workers, asks whether the agent lives on
    this machine, and (on macOS/Linux with crontab available) installs
    the schedule entries idempotently. Otherwise prints the exact lines
    for manual installation. Never raises — scheduling is a convenience,
    not a setup blocker.
    """
    print(_bold("Step 10e: Scheduled agent workers"))
    print()
    print("  Colony needs two small workers scheduled on the agent's machine:")
    for spec in WORKER_SPECS:
        print(f"    • {spec['name']} — {spec['blurb']}")
    print()

    # Cron lines are built the same way for install and manual fallback.
    state_dir = os.environ.get("COLONY_STATE_DIR", "")
    workdir = str(Path(state_dir).expanduser().parent) if state_dir else str(Path.home())
    colony_home = Path(os.environ.get("COLONY_HOME", Path.home() / ".colony"))
    log_dir = colony_home / "logs"
    lines = build_cron_lines(
        env_file=str(env_path.expanduser().resolve()),
        log_dir=str(log_dir),
        workdir=workdir,
        which=which,
    )

    def _print_manual():
        print("  Add these crontab lines yourself when ready (crontab -e):")
        for line in lines:
            print(f"    {line}")
        print()

    answer = _prompt("  Is your agent on this machine? [Y/n]", "Y", non_interactive, ask=ask)
    if answer.strip().lower() not in ("y", "yes", ""):
        print("  ⚪ Skipping local schedule install (agent is elsewhere).")
        _print_manual()
        return

    system = platform.system()
    if system not in ("Darwin", "Linux") or not which("crontab"):
        reason = "crontab not found" if system in ("Darwin", "Linux") else f"unsupported platform ({system})"
        print(f"  ⚪ Cannot install automatically ({reason}).")
        _print_manual()
        return

    install = _prompt("  Install the crontab entries now? [Y/n]", "Y", non_interactive, ask=ask)
    if install.strip().lower() not in ("y", "yes", ""):
        print("  ⚪ Skipped.")
        _print_manual()
        return

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        added = install_cron_jobs(lines, run=run)
        if added:
            print(f"  ✅ Installed {len(added)} crontab entr{'y' if len(added) == 1 else 'ies'}:")
            for line in added:
                print(f"    {line}")
        else:
            print("  ✅ Crontab entries already installed (nothing to do).")
        print(f"  Logs: {log_dir}/cron-<worker>.log")
    except Exception as exc:
        print(f"  ⚠️ Crontab install failed: {exc}")
        _print_manual()
    print()


# ── Final health check (colony doctor) ──────────────────────────────────────

def _print_doctor_results(results) -> None:
    """Print doctor CheckResults in the wizard's own style."""
    icons = {
        "ok": "✅", "pass": "✅", "passed": "✅",
        "warn": "⚠️", "warning": "⚠️",
        "skip": "⚪", "skipped": "⚪",
        "fail": "❌", "failed": "❌", "error": "❌",
    }
    for check in results or []:
        name = getattr(check, "name", str(check))
        status = getattr(check, "status", "")
        status = str(getattr(status, "value", status)).lower()
        detail = getattr(check, "detail", "") or ""
        remedy = getattr(check, "remedy", "") or ""
        icon = icons.get(status, "⚪")
        line = f"  {icon} {name}"
        if detail:
            line += f": {detail}"
        print(line)
        if remedy and icon in ("⚠️", "❌"):
            print(f"     ↳ {remedy}")


def _offer_doctor_run(
    non_interactive: bool = False,
    ask=None,
    colony_url: str = "",
    api_key: str = "",
) -> None:
    """Final wizard step: offer to run colony doctor's checks.

    The doctor module may be mid-build, so import lazily and degrade to a
    pointer at the CLI when unavailable. Server checks self-skip when the
    sidecar isn't running yet.
    """
    print(_bold("Step 12: Health check (colony doctor)"))
    print()
    try:
        from colony_sidecar.doctor import run_doctor
    except Exception:
        print("  ⚪ Doctor not available yet — run `colony doctor` after starting the sidecar.")
        print()
        return

    answer = _prompt("  Run the doctor now? [Y/n]", "Y", non_interactive, ask=ask)
    if answer.strip().lower() not in ("y", "yes", ""):
        print("  ⚪ Skipped — run `colony doctor` any time.")
        print()
        return

    print("  Running local checks (server checks skip if the sidecar isn't up)...")
    try:
        import inspect
        base_kwargs: dict = {}
        if colony_url:
            base_kwargs["colony_url"] = colony_url
        if api_key:
            base_kwargs["api_key"] = api_key
        results = None
        # Be tolerant of signature drift while doctor.py is mid-build.
        for kwargs in (base_kwargs, {}):
            try:
                results = run_doctor(**kwargs)
                break
            except TypeError:
                continue
        if inspect.iscoroutine(results):
            results = asyncio.run(results)
        results = list(results or [])
        if results:
            _print_doctor_results(results)
        else:
            print("  ⚪ Doctor returned no results")
    except Exception as exc:
        print(f"  ⚪ Doctor run failed: {exc}")
        print("  Run `colony doctor` after starting the sidecar.")
    print()


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

    docker_status, docker_msg = _check_docker()
    docker_ok = docker_status == DockerStatus.RUNNING
    print(f"  Docker: {'✅ available' if docker_ok else '⚪ ' + docker_msg}")

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
            if hf == "hermes":
                agent_harness = hf
            elif hf == "openclaw":
                print("  ⚠️ OpenClaw support was removed in v0.21.14 — "
                      "running standalone. Use --agent-harness hermes or "
                      "'colony mcp setup'.")
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
        print("    [1] Hermes — persistent agent framework (plugins installed")
        print("        to ~/.hermes/plugins/)")
        print("    [2] Skip — run standalone (coding tools can still connect")
        print("        via 'colony mcp setup')")
        print()

        # Loop until valid choice
        while True:
            choice = _prompt("  Choice [2]", "2", non_interactive)

            if choice == "1":
                # Hermes doesn't require Node.js - it works with any Python setup
                hermes_detected = "hermes" in agent_detected
                if not hermes_detected:
                    print()
                    print("  ⚠️ Hermes not detected on this system.")
                    print("     Plugin files will be installed to ~/.hermes/plugins/")
                    print("     Install Hermes later: "
                          "https://github.com/NousResearch/hermes-agent")
                    print()
                    cont = _prompt("  Continue with Hermes setup? [Y/n]", "Y", non_interactive)
                    if cont.lower() not in ("y", "yes", ""):
                        continue
                agent_harness = "hermes"
                break
            elif choice == "2":
                break  # standalone
            else:
                print("  Invalid choice. Enter 1 or 2.")
                continue

        # Get contact name if any harness is connected
        if mcp_harnesses or agent_harness:
            print()
            contact_id = _prompt("  What should Colony call you?", os.environ.get("USER", ""), non_interactive)

    print()

    # ── Step 4: Docker setup ────────────────────────────────────────────

    docker_ok = _handle_docker_setup(non_interactive)

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
    tier = None  # Will be set if auto-detection runs

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
                if hw.gpu_type == "cuda":
                    embed_provider = "cuda"
                elif hw.gpu_type == "mlx":
                    # Prefer native MLX when the package is available
                    try:
                        import mlx_embeddings  # noqa: F401
                        embed_provider = "native_mlx"
                    except ImportError:
                        embed_provider = "mlx"
                else:
                    embed_provider = "cpu"
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
                # Note for Apple Silicon users about native MLX
                if hw.gpu_type == "mlx":
                    print()
                    if embed_provider == "native_mlx":
                        print("  🎯 Apple Silicon detected: using native MLX framework (fastest path)")
                    else:
                        print("  ⚠️ Apple Silicon detected: using PyTorch MPS fallback.")
                        print("     Install mlx-embeddings for native MLX: pip install mlx-embeddings mlx-lm")
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
    if agent_harness == "hermes":
        print()
        _setup_hermes_plugin(values["COLONY_API_KEY"], sidecar_url, non_interactive, contact_id or "default")
    
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

    # —— Step 7: Download embedding + reranker models
    if embed_provider == "skip":
        print(_bold("Step 7: Embeddings skipped"))
        print()
        print("  Colony will run without vector search. You can enable embeddings later")
        print("  by editing COLONY_EMBED_PROVIDER in .env and restarting.")
    elif embed_provider in ("cuda", "cpu", "mlx", "native_mlx") and embed_model:
        print(_bold("Step 7: Download embedding model"))
        print()
        
        # Check for HF_TOKEN
        hf_token = os.environ.get("HF_TOKEN")
        if not hf_token:
            print("  ⚠️ No HF_TOKEN set — downloads may be slower due to rate limits")
            print("     Get a token at: https://huggingface.co/settings/tokens")
            print("     Set it with: echo 'HF_TOKEN=hf_xxx' >> ~/.colony/.env")
            print()
        
        print(f"  Downloading {embed_model}...")
        print(f"  (This may take a while on first run — models are cached by HuggingFace)")
        try:
            if embed_provider == "native_mlx":
                from mlx_embeddings import load
                load(embed_model, lazy=False)
            else:
                from sentence_transformers import SentenceTransformer
                SentenceTransformer(embed_model)
            print(f"  ✅ Embedding model downloaded and cached")
        except Exception as exc:
            print(f"  ⚠️ Model download failed: {exc}")
            print(f"  The model will download on first start instead.")

        if reranker_model:
            print(f"  Downloading reranker {reranker_model}...")
            try:
                if embed_provider == "native_mlx":
                    from mlx_lm import load as mlx_lm_load
                    mlx_lm_load(reranker_model, lazy=False)
                else:
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
                matches_model = t.text_embedder and t.text_embedder.model_id == embed_model
                matches_label = tier and t.label == tier.label
                if matches_model or matches_label:
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

    # ── Step 8: Autonomy & approvals ────────────────────────────────────

    # ContactsConfig.from_env() must resolve to the DB the wizard just set up.
    os.environ["COLONY_CONTACTS_DB"] = str(contacts_db)
    autonomy_updates = run_autonomy_step(values, existing, non_interactive)
    if autonomy_updates:
        values.update(autonomy_updates)
        _write_env(env_path, values)
        print(f"  ✅ Updated {env_path}")
        # Make the new settings visible to everything the wizard starts next.
        os.environ.update(autonomy_updates)

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

                    # ── Step 10a: Seed self-knowledge via API ────────────────
                    # Now that sidecar is running, seed Neo4j memories
                    try:
                        api_key = values.get("COLONY_API_KEY", "colony")
                        seed_r = httpx.post(
                            f"{sidecar_url}/v1/host/seed",
                            headers={"Authorization": f"Bearer {api_key}"},
                            timeout=30,
                        )
                        if seed_r.status_code == 200:
                            seed_data = seed_r.json()
                            print(f"  ✅ Self-knowledge seeded (memories: {seed_data.get('memories', 0)}, entities: {seed_data.get('entities', 0)})")
                        else:
                            print(f"  ⚪ Seeding deferred (status {seed_r.status_code})")
                    except Exception as seed_err:
                        print(f"  ⚪ Seeding deferred: {seed_err}")

                    break
            except Exception:
                pass

        if not sidecar_started:
            print("  ⚠️ Sidecar didn't respond within 15s")
            print("  It may still be starting. Run 'colony status' to check.")
    else:
        print("  ⚪ Skipping sidecar start")

    # ── Step 10b: Verify LLM credentials ────────────────────────────────

    # ── Step 10c: Restart gateway ───────────────────────────────────────

    # (Step 10c removed with OpenClaw support: Hermes plugin activation is a
    # 'hermes restart' the user runs when convenient; the summary prints it.)

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

    # ── Step 10e: Scheduled agent workers ───────────────────────────────

    print()
    run_workers_step(env_path, non_interactive)

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
        print("    colony mcp setup --harness <claude-code|codex|crush|opencode>")
        print("    colony init --agent-harness hermes")
        print()
    elif agent_harness == "hermes":
        print("  Colony is connected to Hermes.")
        print()
        print("  Hermes plugins installed:")
        print(f"    ~/.hermes/plugins/colony-memory/")
        print(f"    ~/.hermes/plugins/context_engine/colony/")
        print(f"    ~/.hermes/plugins/colony/")
        print()
        print("  Restart Hermes to activate:")
        print("    hermes restart")
        print()
        print("  Verify the connection:")
        print("    hermes colony status")
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
    if sidecar_started and (mcp_harnesses or agent_harness == "hermes"):
        print()
        if agent_harness == "hermes":
            print("  The sidecar is running and Hermes is configured.")
        elif mcp_harnesses:
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

    # ── Step 12: Health check (colony doctor) ───────────────────────────

    _offer_doctor_run(
        non_interactive,
        colony_url=sidecar_url,
        api_key=values.get("COLONY_API_KEY", ""),
    )

    return 0
