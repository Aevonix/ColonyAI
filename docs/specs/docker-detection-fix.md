# Docker Detection Fix Spec

> **Issue:** Colony wizard fails to detect existing Docker installation when daemon is not running, then incorrectly offers to install Docker Desktop (which fails because it's already installed)
> **Impact:** Poor UX, confusing error messages, blocked setup
> **Platforms:** macOS, Linux

---

## Problem Analysis

### Current Behavior

```python
def _check_docker() -> bool:
    if not shutil.which("docker"):
        return False
    result = subprocess.run(["docker", "info"], ...)
    return result.returncode == 0
```

**Returns `False` for:**
- Docker not installed ✅ correct
- Docker installed but daemon not running ❌ wrong response
- Permission denied (Linux, not in docker group) ❌ wrong response
- Docker Desktop installed but not started ❌ wrong response

### User Impact

| Scenario | Current | User Sees |
|----------|---------|-----------|
| Docker installed, daemon off | "Install Docker?" | Confusing — Docker IS installed |
| Permission denied | "Install Docker?" | Wrong fix — need group membership |
| Alt runtime (Colima) | May not detect | Inconsistent behavior |

---

## Proposed Solution

### 1. DockerStatus Enum

```python
class DockerStatus(str, Enum):
    """Docker installation/runtime status."""
    
    # Good states
    RUNNING = "running"                    # Docker daemon is running
    
    # Install states
    NOT_INSTALLED = "not_installed"        # No docker binary found
    DESKTOP_INSTALLED_NOT_RUNNING = "desktop_not_running"  # macOS: Docker Desktop app exists, daemon off
    
    # Runtime states  
    INSTALLED_NOT_RUNNING = "not_running"  # docker binary exists, daemon off
    PERMISSION_DENIED = "permission_denied"  # Linux: user not in docker group
    
    # Alternative runtimes
    COLIMA_INSTALLED = "colima"            # macOS: Colima installed
    ORBSTACK_INSTALLED = "orbstack"        # macOS: OrbStack installed
    PODMAN_INSTALLED = "podman"            # Linux: Podman installed (docker-compatible)
    
    # Errors
    ERROR = "error"                        # Unexpected error
```

### 2. Enhanced Detection Function

```python
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


def _check_alternative_runtimes() -> Optional[tuple[str, str]]:
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
```

### 3. Docker Desktop Detection (macOS)

```python
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
```

### 4. Linux-Specific Handling

```python
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
```

### 5. Updated Step 4 Flow

```python
def _handle_docker_setup(non_interactive: bool = False) -> tuple[bool, str]:
    """Handle Docker detection and setup.
    
    Returns:
        (success, neo4j_password_candidate)
    """
    system = platform.system().lower()
    
    # Check Docker status
    status, message = _check_docker()
    
    print(_bold("Step 4: Docker"))
    print()
    
    if status == DockerStatus.RUNNING:
        print(f"  ✅ Docker is running")
        print()
        return True, ""
    
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
                    return True, ""
                else:
                    print("  ⚠️ Docker Desktop started but daemon not ready yet.")
                    print("  Wait for Docker to fully start, then re-run 'colony init'.")
                    return False, ""
            else:
                print("  ❌ Failed to start Docker Desktop")
                print("  Open Docker Desktop from Applications manually.")
                return False, ""
        else:
            print("  Start Docker Desktop manually and re-run 'colony init'.")
            return False, ""
    
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
                        return True, ""
                    else:
                        print("  ⚠️ Daemon started but not ready yet.")
                        return False, ""
                else:
                    print("  ❌ Failed to start Docker daemon")
                    print("  Try: sudo systemctl start docker")
                    return False, ""
        else:
            print("  Start the Docker daemon and re-run 'colony init'.")
            return False, ""
    
    elif status == DockerStatus.PERMISSION_DENIED:
        # Linux: user not in docker group
        print(_suggest_docker_group_fix())
        return False, ""
    
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
                    return True, ""
            except Exception as e:
                print(f"  ❌ Failed to start Colima: {e}")
                return False, ""
        else:
            print("  Run 'colima start' and re-run 'colony init'.")
            return False, ""
    
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
                    return True, ""
            except Exception as e:
                print(f"  ❌ Failed to start OrbStack: {e}")
                return False, ""
        else:
            print("  Open OrbStack and re-run 'colony init'.")
            return False, ""
    
    elif status == DockerStatus.PODMAN_INSTALLED:
        # Linux: Podman installed
        print("  Podman is installed (Docker-compatible).")
        print("  Colony can use Podman's Docker socket compatibility.")
        print()
        
        # Check if podman socket is running
        socket_path = Path.home() / ".local" / "share" / "containers" / "podman" / "machine" / "cni" / "podman.sock"
        if socket_path.exists():
            print("  ✅ Podman socket available")
            return True, ""
        
        start = _prompt("  Start Podman socket? [Y/n]", "Y", non_interactive)
        if start.lower() in ("y", "yes", ""):
            print("  Starting Podman socket...")
            try:
                subprocess.run(["podman", "system", "service", "--time=0"], check=True, timeout=10)
                return True, ""
            except Exception as e:
                print(f"  ❌ Failed to start Podman socket: {e}")
                return False, ""
        return False, ""
    
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
            return False, ""
        
        install = _prompt("  Install Docker now? [Y/n]", "Y", non_interactive)
        if install.lower() in ("y", "yes", ""):
            if _install_docker():
                print("  Waiting for Docker daemon...")
                if _wait_for_docker():
                    print("  ✅ Docker is running")
                    print()
                    return True, ""
                else:
                    print("  ⚠️ Docker installed but daemon not reachable yet.")
                    print("  Start Docker and re-run 'colony init'.")
            else:
                print()
                print("  Install Docker manually: https://docs.docker.com/get-docker/")
        else:
            print("  Skipping Docker — Neo4j will not be available.")
        return False, ""
    
    else:
        # Error or unknown status
        print(f"  ⚠️ Docker check failed: {message}")
        print()
        print("  Ensure Docker is installed and running, then re-run 'colony init'.")
        return False, ""


def _install_docker() -> bool:
    """Attempt to install Docker based on platform. Returns True if installed."""
    system = platform.system().lower()

    if system == "linux":
        # Check if Docker is actually already installed but just not detected
        docker_path = shutil.which("docker")
        if docker_path:
            print("  ⚠️ Docker binary found at {docker_path} but not working.")
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
                if "already installed" in result.stderr.lower() or "already the newest" in result.stderr.lower():
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
                if "already installed" in result.stdout.lower() or "already installed" in result.stderr.lower():
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
```

---

## Test Matrix

### macOS Test Cases

| Setup | Expected Detection | Expected Action |
|-------|-------------------|-----------------|
| No Docker, no Homebrew | NOT_INSTALLED | Manual install URL |
| No Docker, Homebrew | NOT_INSTALLED | Offer `brew install --cask docker` |
| Docker Desktop installed, not running | DESKTOP_INSTALLED_NOT_RUNNING | Offer to start |
| Docker Desktop installed, running | RUNNING | Skip to Neo4j |
| Docker Desktop installed, not in PATH | DESKTOP_INSTALLED_NOT_RUNNING | Offer to start |
| Colima installed, not running | COLIMA_INSTALLED | Offer `colima start` |
| Colima running | RUNNING | Skip to Neo4j |
| OrbStack installed, not running | ORBSTACK_INSTALLED | Offer to start |
| OrbStack running | RUNNING | Skip to Neo4j |
| brew install fails (already installed) | DESKTOP_INSTALLED_NOT_RUNNING | Start existing |

### Linux Test Cases

| Setup | Expected Detection | Expected Action |
|-------|-------------------|-----------------|
| No Docker | NOT_INSTALLED | Offer `curl get.docker.com \| sudo sh` |
| Docker installed, not running | INSTALLED_NOT_RUNNING | Offer `sudo systemctl start docker` |
| Docker installed, running | RUNNING | Skip to Neo4j |
| Docker installed, user not in group | PERMISSION_DENIED | Show `usermod` instructions |
| Podman installed, socket running | RUNNING | Use podman socket |
| Podman installed, socket not running | PODMAN_INSTALLED | Offer `podman system service` |
| get.docker.com fails (already installed) | INSTALLED_NOT_RUNNING | Start daemon |

---

## Implementation Checklist

- [ ] Add `DockerStatus` enum
- [ ] Update `_check_docker()` to return `(DockerStatus, str)`
- [ ] Add `_detect_docker_desktop()` for macOS
- [ ] Add `_start_docker_desktop()` for macOS
- [ ] Add `_check_docker_group()` for Linux
- [ ] Add `_start_docker_daemon_linux()` for Linux
- [ ] Add `_check_alternative_runtimes()` 
- [ ] Replace Step 4 with `_handle_docker_setup()`
- [ ] Update `_install_docker()` to handle "already installed" errors
- [ ] Add integration tests for all scenarios

---

## Effort Estimate

| Task | Time |
|------|------|
| Detection refactor | 2h |
| macOS handlers | 1h |
| Linux handlers | 1h |
| Testing | 2h |
| **Total** | **6h** |

---

## Breaking Changes

None — this is internal to `setup.py` and only affects the wizard flow.

---

## Future Improvements

1. **Windows support** — Add Windows detection and handling
2. **Remote Docker** — Support `DOCKER_HOST` environment variable
3. **Rootless Docker** — Better support for rootless Docker on Linux
4. **Auto-detect running container** — If Neo4j is already running in Docker, skip setup
