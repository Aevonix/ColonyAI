"""Background document entity extractor.

Invoked via task queue. MAY use heavier NER models.
Processes structured documents and returns typed entity candidates.
"""

import base64
from enum import Enum
from typing import Optional

from .conversation_extractor import ExtractionResult, ConversationExtractor


class DocumentType(str, Enum):
    PDF = "pdf"
    HTML = "html"
    EMAIL_BODY = "email_body"
    PLAIN_TEXT = "plain_text"
    CALENDAR_ICS = "calendar_ics"


class DocumentExtractor:
    """Background document entity extractor.

    Invoked via task queue. MAY use heavier NER models.
    Processes structured documents and returns typed entity candidates.
    """

    def __init__(self) -> None:
        self._conv_extractor = ConversationExtractor(min_message_length=0)

    async def extract_from_document(
        self,
        content: bytes,
        document_type: DocumentType,
        source_reference: str,
    ) -> ExtractionResult:
        """Extract entities from a document.

        Args:
            content: Raw document bytes.
            document_type: The type of document being processed.
            source_reference: Email ID, URL, or file path for provenance.

        Returns:
            ExtractionResult with full entity and relationship candidates.
        """
        text = self._decode_content(content, document_type)
        return await self._conv_extractor.extract(
            message_text=text,
            source_id=source_reference,
        )

    def _decode_content(self, content: bytes, document_type: DocumentType) -> str:
        """Decode raw bytes to text based on document type."""
        if document_type == DocumentType.HTML:
            return self._strip_html(content.decode("utf-8", errors="replace"))
        elif document_type == DocumentType.CALENDAR_ICS:
            return self._parse_ics(content.decode("utf-8", errors="replace"))
        else:
            return content.decode("utf-8", errors="replace")

    def _strip_html(self, html: str) -> str:
        """Minimal HTML tag stripper."""
        import re
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&[a-z]+;", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _parse_ics(self, ics: str) -> str:
        """Extract displayable text from iCal content."""
        lines = []
        for line in ics.splitlines():
            for prefix in ("SUMMARY:", "DESCRIPTION:", "LOCATION:", "ORGANIZER:"):
                if line.startswith(prefix):
                    lines.append(line[len(prefix):].strip())
                    break
        return " ".join(lines)
