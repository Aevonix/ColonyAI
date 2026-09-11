"""Web search native tool."""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class WebSearchTool:
    """Web search via Colony's SearchOrchestrator."""

    def __init__(self, search_orchestrator=None):
        self._orchestrator = search_orchestrator

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Max results (default 5)", "default": 5},
            },
            "required": ["query"],
        }

    async def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if not self._orchestrator or not self._orchestrator.has_providers:
            return {"error": True, "message": "Web search not configured"}

        query = args.get("query", "")
        max_results = args.get("max_results", 5)

        try:
            results = await self._orchestrator.search(query, max_results)
            return {
                "results": [
                    {"title": r.title, "url": r.url, "snippet": r.snippet, "source": r.source}
                    for r in results
                ],
                "count": len(results),
            }
        except Exception as e:
            return {"error": True, "message": str(e)}
