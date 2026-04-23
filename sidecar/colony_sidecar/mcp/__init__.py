"""Colony MCP Server — exposes cognitive substrate as MCP tools."""

import importlib.util

_MCP_SDK_AVAILABLE = importlib.util.find_spec("mcp") is not None

if _MCP_SDK_AVAILABLE:
    from colony_sidecar.mcp.server import create_server, run_stdio, run_http
else:
    # mcp package not installed (optional dependency)
    create_server = run_stdio = run_http = None  # type: ignore[assignment,misc]

__all__ = ["create_server", "run_stdio", "run_http"]
