"""Colony MCP Server — exposes cognitive substrate as MCP tools."""

import importlib.util
import logging

logger = logging.getLogger(__name__)


def _fastmcp_available() -> bool:
    """True only if the symbol this package actually imports is present.

    Checking for the top-level ``mcp`` package is not sufficient: mcp 2.x
    ships the distribution but no longer provides ``mcp.server.fastmcp``.
    Probing the parent alone made an incompatible SDK look available and
    turned an optional dependency into an import-time crash.
    """
    if importlib.util.find_spec("mcp") is None:
        return False
    try:
        if importlib.util.find_spec("mcp.server.fastmcp") is None:
            raise ModuleNotFoundError("mcp.server.fastmcp")
    except (ImportError, AttributeError, ValueError):
        logger.warning(
            "mcp is installed but does not provide mcp.server.fastmcp "
            "(mcp>=2 removed it). The Colony MCP server is UNAVAILABLE. "
            "Install mcp[cli]>=1.0,<2 to enable it."
        )
        return False
    return True


_MCP_SDK_AVAILABLE = _fastmcp_available()

if _MCP_SDK_AVAILABLE:
    from colony_sidecar.mcp.server import create_server, run_stdio, run_http
else:
    # Optional dependency absent or incompatible. Callers must check for None
    # rather than assume a server can be constructed.
    create_server = run_stdio = run_http = None  # type: ignore[assignment,misc]

__all__ = ["create_server", "run_stdio", "run_http", "_MCP_SDK_AVAILABLE"]
