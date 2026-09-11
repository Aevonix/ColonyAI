"""Sandboxed file operation tools."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ReadFileTool:
    """Read files from a sandboxed directory."""

    def __init__(self, sandbox_dir: str = ""):
        self._sandbox = Path(sandbox_dir).resolve() if sandbox_dir else None

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to sandbox"},
            },
            "required": ["path"],
        }

    async def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if not self._sandbox:
            return {"error": True, "message": "File sandbox not configured"}

        path = args.get("path", "")
        try:
            resolved = self._resolve_safe(path)
            if not resolved.exists():
                return {"error": True, "message": f"File not found: {path}"}
            if resolved.is_dir():
                return {"error": True, "message": f"Path is a directory: {path}"}
            content = resolved.read_text(errors="replace")
            return {"content": content, "path": str(resolved.relative_to(self._sandbox)), "size": len(content)}
        except ValueError as e:
            return {"error": True, "message": str(e)}

    def _resolve_safe(self, path: str) -> Path:
        """Resolve path within sandbox — reject path traversal."""
        if not self._sandbox:
            raise ValueError("Sandbox not configured")
        resolved = (self._sandbox / path).resolve()
        if not str(resolved).startswith(str(self._sandbox)):
            raise ValueError("Path traversal detected")
        return resolved


class WriteFileTool:
    """Write files to a sandboxed directory."""

    def __init__(self, sandbox_dir: str = ""):
        self._sandbox = Path(sandbox_dir).resolve() if sandbox_dir else None

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to sandbox"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if not self._sandbox:
            return {"error": True, "message": "File sandbox not configured"}

        path = args.get("path", "")
        content = args.get("content", "")
        try:
            resolved = self._resolve_safe(path)
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content)
            return {"path": str(resolved.relative_to(self._sandbox)), "size": len(content), "written": True}
        except ValueError as e:
            return {"error": True, "message": str(e)}

    def _resolve_safe(self, path: str) -> Path:
        if not self._sandbox:
            raise ValueError("Sandbox not configured")
        resolved = (self._sandbox / path).resolve()
        if not str(resolved).startswith(str(self._sandbox)):
            raise ValueError("Path traversal detected")
        return resolved


class ListDirectoryTool:
    """List files in a sandboxed directory."""

    def __init__(self, sandbox_dir: str = ""):
        self._sandbox = Path(sandbox_dir).resolve() if sandbox_dir else None

    @property
    def name(self) -> str:
        return "list_directory"

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path relative to sandbox (default: root)", "default": ""},
            },
        }

    async def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if not self._sandbox:
            return {"error": True, "message": "File sandbox not configured"}

        path = args.get("path", "")
        try:
            resolved = self._resolve_safe(path)
            if not resolved.exists():
                return {"error": True, "message": f"Directory not found: {path}"}
            if not resolved.is_dir():
                return {"error": True, "message": f"Not a directory: {path}"}

            entries = []
            for p in sorted(resolved.iterdir()):
                entries.append({
                    "name": p.name,
                    "type": "directory" if p.is_dir() else "file",
                    "size": p.stat().st_size if p.is_file() else None,
                })
            return {"path": path or "/", "entries": entries, "count": len(entries)}
        except ValueError as e:
            return {"error": True, "message": str(e)}

    def _resolve_safe(self, path: str) -> Path:
        if not self._sandbox:
            raise ValueError("Sandbox not configured")
        resolved = (self._sandbox / path).resolve() if path else self._sandbox
        if not str(resolved).startswith(str(self._sandbox)):
            raise ValueError("Path traversal detected")
        return resolved
