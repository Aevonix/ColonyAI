"""JSON entity extractor."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from colony_sidecar.world_model.extraction.base import FormatExtractor, ExtractedEntity

logger = logging.getLogger(__name__)


class JSONExtractor(FormatExtractor):
    """Extract entities from structured JSON data.

    Handles two patterns:
    1. Array of objects: each object becomes an entity
    2. Nested objects: top-level keys become entity types
    """

    def supported_formats(self) -> List[str]:
        return ["application/json"]

    async def extract_text(self, content: bytes, metadata: Dict[str, Any]) -> str:
        try:
            data = json.loads(content)
            return json.dumps(data, indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ""

    async def extract_entities(self, content: bytes, metadata: Dict[str, Any]) -> List[ExtractedEntity]:
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("Failed to parse JSON: %s", e)
            return []

        entities = []

        # Array of objects — each becomes an entity
        if isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, dict):
                    name = item.get("name") or item.get("title") or item.get("id") or f"item-{i}"
                    etype = item.get("type", "entity")
                    attrs = {k: v for k, v in item.items() if k not in ("name", "title", "id", "type")}
                    entities.append(ExtractedEntity(
                        name=str(name),
                        entity_type=str(etype),
                        attributes=attrs,
                        confidence=0.9,
                    ))

        # Single object — top-level keys become attributes
        elif isinstance(data, dict):
            # If it has a 'name' or 'title', treat as one entity
            name = data.get("name") or data.get("title") or metadata.get("filename", "json-object")
            etype = data.get("type", "entity")
            attrs = {k: v for k, v in data.items() if k not in ("name", "title", "type") and isinstance(v, (str, int, float, bool))}
            entities.append(ExtractedEntity(
                name=str(name),
                entity_type=str(etype),
                attributes=attrs,
                confidence=0.9,
            ))

        return entities
