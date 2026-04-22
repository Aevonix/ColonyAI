"""Base class for format extractors."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ExtractedEntity:
    """An entity extracted from a document."""

    def __init__(
        self,
        name: str,
        entity_type: str,
        attributes: Optional[Dict[str, Any]] = None,
        source_offset: Optional[int] = None,
        confidence: float = 1.0,
    ):
        self.name = name
        self.entity_type = entity_type
        self.attributes = attributes or {}
        self.source_offset = source_offset
        self.confidence = confidence

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.entity_type,
            "attributes": self.attributes,
            "confidence": self.confidence,
        }


class FormatExtractor(ABC):
    """Base class for document format extractors.

    Each extractor handles one or more MIME types and can:
    1. Extract raw text from the document
    2. Extract structured entities directly (without LLM)
    """

    @abstractmethod
    def supported_formats(self) -> List[str]:
        """Return list of MIME types this extractor handles."""
        ...

    @abstractmethod
    async def extract_text(self, content: bytes, metadata: Dict[str, Any]) -> str:
        """Extract raw text from content. Returns empty string if extraction fails."""
        ...

    async def extract_entities(self, content: bytes, metadata: Dict[str, Any]) -> List[ExtractedEntity]:
        """Extract structured entities directly from content (no LLM).

        Default implementation returns empty list. Override for formats
        that have structured data (JSON, CSV).
        """
        return []
