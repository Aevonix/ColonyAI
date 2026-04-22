"""CSV entity extractor."""

from __future__ import annotations

import csv
import io
import logging
from typing import Any, Dict, List

from colony_sidecar.world_model.extraction.base import FormatExtractor, ExtractedEntity

logger = logging.getLogger(__name__)


class CSVExtractor(FormatExtractor):
    """Extract entities from CSV data.

    Each row becomes an entity. Column headers are used as attribute keys.
    A 'name' or 'title' column (if present) is used as the entity name.
    """

    def supported_formats(self) -> List[str]:
        return ["text/csv"]

    async def extract_text(self, content: bytes, metadata: Dict[str, Any]) -> str:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = content.decode("latin-1")
            except Exception:
                return ""
        return text

    async def extract_entities(self, content: bytes, metadata: Dict[str, Any]) -> List[ExtractedEntity]:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = content.decode("latin-1")
            except Exception as e:
                logger.warning("Failed to decode CSV: %s", e)
                return []

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return []

        entities = []
        for i, row in enumerate(reader):
            # Find the name column
            name = row.get("name") or row.get("title") or row.get("Name") or row.get("Title") or f"row-{i}"
            etype = row.get("type") or row.get("Type") or "entity"

            # All other columns become attributes
            attrs = {}
            for k, v in row.items():
                if k and k.lower() not in ("name", "title", "type") and v:
                    attrs[k] = v

            entities.append(ExtractedEntity(
                name=str(name).strip(),
                entity_type=str(etype).strip(),
                attributes=attrs,
                confidence=0.85,
            ))

        return entities
