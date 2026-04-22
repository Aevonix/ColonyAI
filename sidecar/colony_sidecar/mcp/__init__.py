"""Colony MCP Server — exposes cognitive substrate as MCP tools."""

from colony_sidecar.mcp.server import create_server, run_stdio, run_http

__all__ = ["create_server", "run_stdio", "run_http"]
