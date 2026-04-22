"""Colony Contacts — contact enrichment."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from .config import ContactsConfig
from .store import SQLiteContactStore, _now_iso

logger = logging.getLogger("colony.contacts.enricher")


class ContactEnricher:
    """Enriches contact records from conversation history and world model.

    Enrichment is non-destructive: extracted values MUST NOT overwrite
    existing values with confidence >= immutable_confidence_threshold.

    The enricher stores per-field confidence in the contact's properties
    (persisted as part of the notes/enrichment_source metadata for now;
    full per-field confidence store is a future enhancement).
    """

    def __init__(
        self,
        store: SQLiteContactStore,
        config: Optional[ContactsConfig] = None,
    ) -> None:
        self._store = store
        self._config = config or ContactsConfig()

    async def enrich_from_fields(
        self,
        contact_id: str,
        candidates: Dict[str, Any],
        source: str = "conversation_history",
        confidence: float = 0.70,
    ) -> bool:
        """Apply enrichment candidates to a contact if they meet the threshold.

        Args:
            contact_id:  Target contact.
            candidates:  Dict of field_name → value to apply.
            source:      Source label (e.g., 'conversation_history').
            confidence:  Confidence score for all candidates in this batch.

        Returns:
            True if any field was updated.
        """
        cfg = self._config.enrichment
        if not cfg.enabled:
            return False

        contact = await self._store.get(contact_id)
        if not contact:
            return False

        # Respect privacy_level = "restricted"
        if cfg.respect_privacy_restricted and contact.privacy_level == "restricted":
            logger.debug("Skipping enrichment for restricted contact %s", contact_id)
            return False

        if confidence < cfg.min_update_confidence:
            logger.debug(
                "Enrichment confidence %.2f below threshold %.2f for %s",
                confidence, cfg.min_update_confidence, contact_id,
            )
            return False

        updatable_fields = {"display_name", "given_name", "family_name", "organization"}
        updates: Dict[str, Any] = {}

        for field_name, new_value in candidates.items():
            if field_name not in updatable_fields or not new_value:
                continue
            existing_value = getattr(contact, field_name, None)
            if existing_value and confidence < cfg.immutable_confidence_threshold:
                # Don't overwrite existing values unless new confidence is very high
                # (existing could have been set at high confidence — we'd need per-field
                # tracking to be precise; conservatively skip if existing value exists)
                logger.debug(
                    "Skipping enrichment of %s.%s: existing value present, confidence %.2f < %.2f",
                    contact_id, field_name, confidence, cfg.immutable_confidence_threshold,
                )
                continue
            updates[field_name] = new_value

        if not updates:
            return False

        # Update enrichment_source
        enrichment_sources = list(set(contact.enrichment_source + [source]))
        updates["enrichment_source"] = enrichment_sources
        updates["enrichment_last_at"] = _now_iso()

        await self._store.update(contact_id, **updates)
        await self._store.record_audit(
            contact_id, "enriched",
            {"fields": list(updates.keys()), "source": source, "confidence": confidence},
        )
        logger.info("Enriched contact %s: fields=%s source=%s", contact_id, list(updates.keys()), source)
        return True

    async def enrich_from_conversation(
        self,
        contact_id: str,
        transcript: str,
        source: str = "conversation_history",
    ) -> bool:
        """Extract candidate fields from a conversation transcript and apply them.

        This is a simple heuristic extractor. In production, this would be
        backed by an LLM extraction pipeline.

        Patterns recognized:
        - "Call me <name>" → given_name candidate
        - "I'm <name>" / "I am <name>" → given_name candidate
        - "I work at <org>" / "I'm at <org>" → organization candidate
        - "my name is <name>" → display_name candidate
        """
        import re

        candidates: Dict[str, Any] = {}
        confidence = 0.75

        # Name patterns
        name_patterns = [
            (re.compile(r"call me ([A-Z][a-z]+)", re.IGNORECASE), "given_name", 0.90),
            (re.compile(r"my name is ([A-Z][a-z]+(?: [A-Z][a-z]+)?)", re.IGNORECASE), "display_name", 0.85),
            (re.compile(r"i(?:'m| am) ([A-Z][a-z]+)(?: [A-Z][a-z]+)?", re.IGNORECASE), "given_name", 0.70),
            (re.compile(r"i go by ([A-Z][a-z]+)", re.IGNORECASE), "given_name", 0.90),
        ]
        org_patterns = [
            (re.compile(r"i(?:'m| am)(?: the \w+)? at ([A-Z][A-Za-z\s]+)", re.IGNORECASE), "organization", 0.75),
            (re.compile(r"i work(?:ing)? at ([A-Z][A-Za-z\s]+)", re.IGNORECASE), "organization", 0.75),
            (re.compile(r"we(?:'re| are)(?: building)? at ([A-Z][A-Za-z\s]+)", re.IGNORECASE), "organization", 0.70),
        ]

        for pattern, field_name, conf in name_patterns + org_patterns:
            m = pattern.search(transcript)
            if m:
                val = m.group(1).strip()
                if val and (field_name not in candidates or conf > confidence):
                    candidates[field_name] = val
                    confidence = conf

        if not candidates:
            return False

        return await self.enrich_from_fields(
            contact_id, candidates, source=source, confidence=confidence
        )

    async def enrich_from_world_model(
        self,
        contact_id: str,
        world_model_store,  # WorldModelStore — optional dependency
    ) -> bool:
        """Cross-reference world model Person node to enrich contact fields.

        This is a read-only operation from the contacts perspective.
        """
        contact = await self._store.get(contact_id)
        if not contact or not contact.person_node_id:
            return False

        try:
            entity = await world_model_store.get_entity(contact.person_node_id)
        except Exception as exc:
            logger.warning("World model lookup failed for %s: %s", contact_id, exc)
            return False

        if not entity:
            return False

        candidates: Dict[str, Any] = {}
        if hasattr(entity, "email") and entity.email and not contact.given_name:
            pass  # email is a handle, not a name field
        if hasattr(entity, "title") and entity.title:
            candidates["organization"] = entity.properties.get("company") or contact.organization
        if entity.name and not contact.display_name:
            candidates["display_name"] = entity.name

        return await self.enrich_from_fields(
            contact_id, candidates, source="world_model", confidence=0.65
        )
