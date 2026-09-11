"""Colony skill runtime handle.

Provides synthesized skills with a ``colony`` object that exposes
tool invocation via the sidecar HTTP API.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class _ToolProxy:
    """Namespace that lets skills call ``colony.tools.invoke(name, args)``."""

    def __init__(self, base_url: str, headers: dict[str, str]) -> None:
        self._base_url = base_url
        self._headers = headers

    async def invoke(self, name: str, args: dict[str, Any] | None = None) -> Any:
        """Invoke a Colony MCP tool by name through the sidecar API.

        Routes to the internal tool-dispatch endpoint so the skill
        runs with the same auth context as the sidecar itself.
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self._base_url}/v1/host/tools/invoke",
                    headers=self._headers,
                    json={"tool": name, "args": args or {}},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.error("colony.tools.invoke(%s) failed: %s", name, exc)
            raise


class ColonyRuntime:
    """Thin runtime handle injected into synthesized skills that declare
    a ``colony`` parameter in their ``run()`` signature.

    Usage inside a skill::

        async def run(colony, query: str) -> str:
            result = await colony.tools.invoke("colony_lookup_facts", {"query": query})
            return str(result)
    """

    def __init__(self, base_url: str = "http://127.0.0.1:7777") -> None:
        self._base_url = base_url.rstrip("/")
        self._headers: dict[str, str] = {}
        self.tools = _ToolProxy(self._base_url, self._headers)
