"""Colony intelligence sidecar — harness-agnostic cognition server.

This is the extracted intelligence layer from colony-ai (the Hermes fork).
It runs as a standalone FastAPI server that hosts (OpenClaw, future shims)
mount as a plugin via the ``/v1/host`` API surface.
"""

from pathlib import Path
import os


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
