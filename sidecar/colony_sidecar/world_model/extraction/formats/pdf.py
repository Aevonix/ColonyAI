"""PDF entity extractor (requires PyMuPDF)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from colony_sidecar.world_model.extraction.base import FormatExtractor, ExtractedEntity

logger = logging.getLogger(__name__)

try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False


class PDFExtractor(FormatExtractor):
    """Extract text and entities from PDF files via PyMuPDF."""

    def supported_formats(self) -> List[str]:
        return ["application/pdf"]

    async def extract_text(self, content: bytes, metadata: Dict[str, Any]) -> str:
        if not HAS_PYMUPDF:
            logger.warning("PyMuPDF not installed — cannot extract PDF text")
            return ""

        try:
            doc = fitz.open(stream=content, filetype="pdf")
            text_parts = []
            for page in doc:
                text_parts.append(page.get_text())
            doc.close()
            return "\n\n".join(text_parts)
        except Exception as e:
            logger.warning("Failed to extract PDF text: %s", e)
            return ""

    async def extract_entities(self, content: bytes, metadata: Dict[str, Any]) -> List[ExtractedEntity]:
        # PDFs don't have structured entities — rely on text extraction + LLM
        return []
