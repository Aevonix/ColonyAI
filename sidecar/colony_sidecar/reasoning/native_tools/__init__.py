"""Native tool handlers for Colony's reasoning loop.

These tools run inside Colony without requiring a host harness roundtrip.
"""

from colony_sidecar.reasoning.native_tools.calculate import CalculateTool
from colony_sidecar.reasoning.native_tools.web_search import WebSearchTool
from colony_sidecar.reasoning.native_tools.file_ops import ReadFileTool, WriteFileTool, ListDirectoryTool

__all__ = [
    "CalculateTool",
    "WebSearchTool",
    "ReadFileTool",
    "WriteFileTool",
    "ListDirectoryTool",
]
