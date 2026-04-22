"""Structured data importer: calendar events and contact lists.

Provides high-confidence entity data from structured sources.
"""

import random
import string
import time
from typing import Any, Dict, Optional

from ..confidence import CONFIDENCE_BY_SOURCE
from ..entities import EventEntity, PersonEntity
from ..sqlite.backend import _generate_id
from ..constants import ENTITY_ID_PREFIX


class StructuredImporter:
    """Imports structured data from calendar and contact sources."""

    async def import_calendar_event(
        self,
        calendar_event: Dict[str, Any],
        source: str = "calendar",
    ) -> EventEntity:
        """Convert a calendar event dict to a World Model EventEntity.

        Extracts: title → name, start/end times, attendees (→ PersonEntity
        stubs), location (→ LocationEntity), organizer.

        Args:
            calendar_event: Raw calendar event data (iCal-compatible dict).
            source: Source identifier for confidence scoring.

        Returns:
            EventEntity ready for resolution and storage.
        """
        conf = CONFIDENCE_BY_SOURCE.get(source, CONFIDENCE_BY_SOURCE["structured_import"])
        event_id = calendar_event.get("id") or _generate_id(ENTITY_ID_PREFIX)

        return EventEntity(
            id=event_id,
            name=calendar_event.get("title") or calendar_event.get("summary", "Untitled Event"),
            entity_type="event",
            confidence=conf,
            event_type=calendar_event.get("event_type", "meeting"),
            start_time=calendar_event.get("start") or calendar_event.get("start_time"),
            end_time=calendar_event.get("end") or calendar_event.get("end_time"),
            description=calendar_event.get("description"),
            url=calendar_event.get("url"),
            external_ids=(
                {"calendar_uid": calendar_event["uid"]}
                if calendar_event.get("uid")
                else {}
            ),
        )

    async def import_contact(
        self,
        contact: Dict[str, Any],
        source: str = "contacts",
    ) -> PersonEntity:
        """Convert a contacts entry to a World Model PersonEntity.

        Args:
            contact: Raw contact data dict.
            source: Source identifier for confidence scoring.

        Returns:
            PersonEntity ready for resolution and storage.
        """
        conf = CONFIDENCE_BY_SOURCE.get(source, CONFIDENCE_BY_SOURCE["structured_import"])
        contact_id = contact.get("id") or _generate_id(ENTITY_ID_PREFIX)

        name = (
            contact.get("name")
            or f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip()
            or contact.get("email", "Unknown")
        )

        external_ids: Dict[str, str] = {}
        if contact.get("email"):
            external_ids["email"] = contact["email"]
        if contact.get("phone"):
            external_ids["phone"] = contact["phone"]
        if contact.get("linkedin"):
            external_ids["linkedin"] = contact["linkedin"]

        return PersonEntity(
            id=contact_id,
            name=name,
            entity_type="person",
            confidence=conf,
            email=contact.get("email"),
            phone=contact.get("phone"),
            title=contact.get("title") or contact.get("job_title"),
            linkedin_url=contact.get("linkedin"),
            twitter_handle=contact.get("twitter"),
            external_ids=external_ids,
        )
