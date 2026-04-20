"""Plain text extractor."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from colony_sidecar.world_model.extraction.base import FormatExtractor, ExtractedEntity

logger = logging.getLogger(__name__)


class TextExtractor(FormatExtractor):
    """Extract entities from plain text (via LLM)."""

    def supported_formats(self) -> List[str]:
        return ["text/plain", "text/markdown"]

    async def extract_text(self, content: bytes, metadata: Dict[str, Any]) -> str:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return content.decode("latin-1")
            except Exception:
                logger.warning("Failed to decode text content")
                return ""
