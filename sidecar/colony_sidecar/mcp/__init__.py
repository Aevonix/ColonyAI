"""Colony MCP Server — exposes cognitive substrate as MCP tools."""

try:
    from colony_sidecar.mcp.server import create_server, run_stdio, run_http
except ImportError:
    # mcp package not installed (optional dependency)
    create_server = run_stdio = run_http = None

__all__ = ["create_server", "run_stdio", "run_http"]
