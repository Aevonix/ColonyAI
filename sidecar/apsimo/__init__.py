"""Apsimo cognition server for agent runtimes.

A standalone FastAPI server that agent hosts (Hermes plugin, MCP harnesses,
REST integrations) mount via the ``/v1/host`` API surface.
"""

from pathlib import Path
import os
from .environment import apply_environment_aliases

apply_environment_aliases()

try:  # single source of truth: the installed package metadata (pyproject)
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("apsimo")
except Exception:  # editable/unbuilt checkouts without installed metadata
    __version__ = "0.0.0+unknown"


def get_state_dir() -> Path:
    """Return the selected state directory without moving existing state.
    
    Priority:
    1. APSIMO_STATE_DIR or legacy COLONY_STATE_DIR (explicit override)
    2. ~/.colony/data (retained unconfigured-library compatibility default)

    Guided setup selects its explicit private instance directory. This fallback
    stays aligned with older independently configured storage readers.
    
    Creates the directory if it doesn't exist.
    """
    explicit = os.environ.get("APSIMO_STATE_DIR") or os.environ.get("COLONY_STATE_DIR")
    if explicit:
        path = Path(explicit)
    else:
        path = Path.home() / ".colony" / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path
