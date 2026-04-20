"""Format detection for document content."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Common extension to MIME type mapping
EXTENSION_MAP = {
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".xml": "application/xml",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
}

# Magic byte signatures
MAGIC_SIGNATURES = {
    b"%PDF": "application/pdf",
    b"<!DOCTYPE": "text/html",
    b"<html": "text/html",
    b"<?xml": "application/xml",
}


class FormatDetector:
    """Detect document format from content, filename, or explicit MIME type."""

    def detect(
        self,
        content: bytes,
        filename: str = "",
        mime_type: str = "",
    ) -> str:
        """Detect content format.

        Priority: explicit mime_type > filename extension > magic bytes > fallback.
        """
        # 1. Explicit MIME type wins
        if mime_type:
            return mime_type

        # 2. Filename extension
        if filename:
            ext = Path(filename).suffix.lower()
            if ext in EXTENSION_MAP:
                return EXTENSION_MAP[ext]

        # 3. Magic bytes
        if content:
            for sig, fmt in MAGIC_SIGNATURES.items():
                if content[:len(sig)].startswith(sig):
                    return fmt

            # JSON detection
            stripped = content[:512].strip()
            if stripped and stripped[0:1] in (b"{", b"["):
                return "application/json"

            # CSV detection (simple heuristic)
            first_line = content[:512].split(b"\n")[0]
            if b"," in first_line and first_line.count(b",") >= 2:
                return "text/csv"

        # 4. Fallback
        return "text/plain"
