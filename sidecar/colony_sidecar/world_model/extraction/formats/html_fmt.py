"""HTML entity extractor (requires BeautifulSoup)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from colony_sidecar.world_model.extraction.base import FormatExtractor, ExtractedEntity

logger = logging.getLogger(__name__)

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False


class HTMLExtractor(FormatExtractor):
    """Extract text and entities from HTML via BeautifulSoup."""

    def supported_formats(self) -> List[str]:
        return ["text/html"]

    async def extract_text(self, content: bytes, metadata: Dict[str, Any]) -> str:
        if not HAS_BS4:
            logger.warning("BeautifulSoup not installed — cannot extract HTML text")
            return ""

        try:
            soup = BeautifulSoup(content, "html.parser")
            # Remove script and style elements
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)
        except Exception as e:
            logger.warning("Failed to extract HTML text: %s", e)
            return ""

    async def extract_entities(self, content: bytes, metadata: Dict[str, Any]) -> List[ExtractedEntity]:
        if not HAS_BS4:
            return []

        try:
            soup = BeautifulSoup(content, "html.parser")
            entities = []

            # Extract metadata entities
            title = soup.find("title")
            if title and title.string:
                entities.append(ExtractedEntity(
                    name=title.string.strip(),
                    entity_type="webpage",
                    attributes={"type": "title"},
                    confidence=0.95,
                ))

            # Extract links as relationship hints
            for a in soup.find_all("a", href=True)[:20]:
                link_text = a.get_text(strip=True)
                if link_text:
                    entities.append(ExtractedEntity(
                        name=link_text,
                        entity_type="link",
                        attributes={"href": a["href"]},
                        confidence=0.7,
                    ))

            return entities
        except Exception as e:
            logger.warning("Failed to extract HTML entities: %s", e)
            return []
