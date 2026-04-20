"""Extraction pipeline — orchestrates format detection and entity extraction."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from colony_sidecar.world_model.extraction.base import ExtractedEntity, FormatExtractor
from colony_sidecar.world_model.extraction.detector import FormatDetector

logger = logging.getLogger(__name__)


class ExtractionPipeline:
    """Orchestrate format detection and entity extraction.

    Flow:
    1. Detect format
    2. Try structured extraction (fast, no LLM)
    3. Fall back to text extraction + LLM
    """

    def __init__(
        self,
        extractors: List[FormatExtractor],
        llm_extract_fn: Optional[Callable] = None,
    ):
        self._extractors: Dict[str, FormatExtractor] = {}
        for ext in extractors:
            for fmt in ext.supported_formats():
                self._extractors[fmt] = ext
        self._detector = FormatDetector()
        self._llm_extract_fn = llm_extract_fn

    async def extract(
        self,
        content: bytes,
        filename: str = "",
        mime_type: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[ExtractedEntity]:
        """Extract entities from content.

        1. Detect format
        2. Try structured extraction (fast, no LLM)
        3. Fall back to text extraction + LLM
        """
        metadata = metadata or {}

        # Detect format
        fmt = self._detector.detect(content, filename=filename, mime_type=mime_type)
        logger.debug("Detected format: %s", fmt)

        # Find extractor
        extractor = self._extractors.get(fmt)
        if extractor is None:
            logger.warning("No extractor for format '%s' — skipping", fmt)
            return []

        # 1. Try structured extraction first
        entities = await extractor.extract_entities(content, metadata)

        # 2. Fall back to text + LLM
        if not entities:
            text = await extractor.extract_text(content, metadata)
            if text and self._llm_extract_fn:
                try:
                    entities = await self._llm_extract_fn(text, metadata)
                except Exception as e:
                    logger.warning("LLM extraction failed: %s", e)

        logger.info("Extracted %d entities from %s format", len(entities), fmt)
        return entities
