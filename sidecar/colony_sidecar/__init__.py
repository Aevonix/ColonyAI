"""Colony intelligence sidecar — harness-agnostic cognition server.

A standalone FastAPI server that agent hosts (Hermes plugin, MCP harnesses,
REST integrations) mount via the ``/v1/host`` API surface.
"""

from pathlib import Path
import os

try:  # single source of truth: the installed package metadata (pyproject)
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("colonyai")
except Exception:  # editable/unbuilt checkouts without installed metadata
    __version__ = "0.0.0+unknown"


def get_state_dir() -> Path:
    """Return the Colony state directory.
    
    Priority:
    1. COLONY_STATE_DIR env var (explicit override)
    2. ~/.colony/data/ (default centralized location)
    
    Creates the directory if it doesn't exist.
    """
    explicit = os.environ.get("COLONY_STATE_DIR")
    if explicit:
        path = Path(explicit)
    else:
        path = Path.home() / ".colony" / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path
