"""Colony Contacts — export pipeline."""

from __future__ import annotations

import csv
import io
import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import ContactsConfig
from .exporters.vcard import VCardExporter
from .models import Contact, ContactHandle
from .store import SQLiteContactStore


class ContactExporter(ABC):
    """Exports contacts to portable formats."""

    @abstractmethod
    async def to_vcard(
        self,
        contact_ids: Optional[List[str]] = None,
        include_tier: bool = False,
    ) -> str:
        """Export contacts as vCard 4.0."""

    @abstractmethod
    async def to_csv(self, contact_ids: Optional[List[str]] = None) -> str:
        """Export contacts as UTF-8 CSV."""

    @abstractmethod
    async def to_json(self, contact_ids: Optional[List[str]] = None) -> str:
        """Export contacts as JSON in colony-contacts-v1 format."""


class SQLiteContactExporter(ContactExporter):
    """ContactExporter backed by SQLiteContactStore."""

    def __init__(self, store: SQLiteContactStore, config: Optional[ContactsConfig] = None) -> None:
        self._store = store
        self._config = config or ContactsConfig()

    async def _load_contacts(self, contact_ids: Optional[List[str]]) -> List[Contact]:
        if contact_ids is not None:
            contacts = []
            for cid in contact_ids:
                c = await self._store.get(cid)
                if c:
                    contacts.append(c)
            return contacts
        return await self._store.list(limit=10000)

    async def to_vcard(
        self,
        contact_ids: Optional[List[str]] = None,
        include_tier: bool = False,
    ) -> str:
        cfg = self._config.export_cfg
        exporter = VCardExporter(
            include_tags=cfg.vcard_include_tags,
            include_tier=include_tier or cfg.vcard_include_tier,
        )
        contacts = await self._load_contacts(contact_ids)
        pairs = []
        for c in contacts:
            handles = await self._store.get_handles(c.contact_id)
            pairs.append((c, handles))
        return exporter.export_many(pairs)

    async def to_csv(self, contact_ids: Optional[List[str]] = None) -> str:
        contacts = await self._load_contacts(contact_ids)
        fieldnames = [
            "contact_id", "display_name", "given_name", "family_name",
            "organization", "trust_tier", "interaction_allowed", "tags",
            "primary_phone", "primary_email", "primary_telegram",
            "first_seen_at", "last_interaction_at", "relationship_score",
        ]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for contact in contacts:
            handles = await self._store.get_handles(contact.contact_id)
            phones = [h.address for h in handles if h.gateway in ("imessage", "sms") and h.is_primary]
            if not phones:
                phones = [h.address for h in handles if h.gateway in ("imessage", "sms")]
            emails = [h.address for h in handles if h.gateway == "email" and h.is_primary]
            if not emails:
                emails = [h.address for h in handles if h.gateway == "email"]
            telegrams = [h.address for h in handles if h.gateway == "telegram" and h.is_primary]
            if not telegrams:
                telegrams = [h.address for h in handles if h.gateway == "telegram"]
            writer.writerow({
                "contact_id": contact.contact_id,
                "display_name": contact.display_name or "",
                "given_name": contact.given_name or "",
                "family_name": contact.family_name or "",
                "organization": contact.organization or "",
                "trust_tier": contact.trust_tier,
                "interaction_allowed": "true" if contact.interaction_allowed else "false",
                "tags": "|".join(contact.tags),
                "primary_phone": phones[0] if phones else "",
                "primary_email": emails[0] if emails else "",
                "primary_telegram": telegrams[0] if telegrams else "",
                "first_seen_at": contact.first_seen_at,
                "last_interaction_at": contact.last_interaction_at or "",
                "relationship_score": contact.relationship_score,
            })
        return output.getvalue()

    async def to_json(self, contact_ids: Optional[List[str]] = None) -> str:
        contacts = await self._load_contacts(contact_ids)
        exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        contact_list = []
        for contact in contacts:
            handles = await self._store.get_handles(contact.contact_id)
            contact_list.append({
                "contact_id": contact.contact_id,
                "display_name": contact.display_name,
                "given_name": contact.given_name,
                "family_name": contact.family_name,
                "organization": contact.organization,
                "trust_tier": contact.trust_tier,
                "interaction_allowed": contact.interaction_allowed,
                "relationship_score": contact.relationship_score,
                "tags": contact.tags,
                "privacy_level": contact.privacy_level,
                "person_node_id": contact.person_node_id,
                "handles": [
                    {
                        "gateway": h.gateway,
                        "address": h.address,
                        "is_primary": h.is_primary,
                        "verified": h.verified,
                        "confidence": h.confidence,
                        "source": h.source,
                    }
                    for h in handles
                ],
                "metadata": {
                    "first_seen_at": contact.first_seen_at,
                    "last_interaction_at": contact.last_interaction_at,
                    "interaction_count": contact.interaction_count,
                    "enrichment_source": contact.enrichment_source,
                    "import_source": contact.import_source,
                },
            })
        return json.dumps(
            {
                "export_format": "colony-contacts-v1",
                "exported_at": exported_at,
                "contacts": contact_list,
            },
            indent=2,
        )
