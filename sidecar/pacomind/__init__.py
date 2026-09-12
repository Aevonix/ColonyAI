"""PacoMind cognition server for agent runtimes.

A standalone FastAPI server that agent hosts (Hermes plugin, MCP harnesses,
REST integrations) mount via the ``/v1/host`` API surface.
"""

from pathlib import Path
import os


try:  # single source of truth: the installed package metadata (pyproject)
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("pacomind")
except Exception:  # editable/unbuilt checkouts without installed metadata
    __version__ = "0.0.0+unknown"


def get_state_dir() -> Path:
    """Return the selected state directory without moving existing state.

    Guided setup selects an explicit private instance directory. Unconfigured
    library use defaults to ~/.pacomind/data.

    Creates the directory if it does not exist.
    """
    explicit = os.environ.get("PACOMIND_STATE_DIR")
    if explicit:
        path = Path(explicit)
    else:
        path = Path.home() / ".pacomind" / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path
