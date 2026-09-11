"""vCard 4.0 exporter (RFC 6350)."""

from __future__ import annotations

from typing import List

from ..models import Contact, ContactHandle


class VCardExporter:
    """Exports contact records to vCard 4.0 format (RFC 6350).

    Args:
        include_tags:  If True, export Colony tags as X-COLONY-TAGS property.
        include_tier:  If True, export trust tier as X-COLONY-TIER property.
    """

    def __init__(
        self,
        include_tags: bool = True,
        include_tier: bool = False,
    ) -> None:
        self._include_tags = include_tags
        self._include_tier = include_tier

    def export_one(self, contact: Contact, handles: List[ContactHandle]) -> str:
        """Render a single contact as a vCard 4.0 string.

        Returns:
            A vCard 4.0 string with CRLF line endings per RFC 6350.
        """
        lines = [
            "BEGIN:VCARD",
            "VERSION:4.0",
        ]
        fn = contact.display_name or "Unknown"
        lines.append(f"FN:{fn}")

        if contact.given_name or contact.family_name:
            family = contact.family_name or ""
            given = contact.given_name or ""
            lines.append(f"N:{family};{given};;;")

        if contact.organization:
            lines.append(f"ORG:{contact.organization}")

        for handle in handles:
            if handle.gateway in ("imessage", "sms"):
                lines.append(f"TEL;TYPE=CELL:{handle.address}")
            elif handle.gateway == "email":
                lines.append(f"EMAIL:{handle.address}")
            elif handle.gateway == "telegram":
                lines.append(f"X-TELEGRAM:{handle.address}")
            elif handle.gateway == "signal":
                lines.append(f"X-SIGNAL:{handle.address}")

        if self._include_tags and contact.tags:
            lines.append(f"X-COLONY-TAGS:{','.join(contact.tags)}")

        if self._include_tier:
            lines.append(f"X-COLONY-TIER:{contact.trust_tier}")

        lines.append(f"UID:{contact.contact_id}")
        lines.append("END:VCARD")
        return "\r\n".join(lines) + "\r\n"

    def export_many(self, contacts_with_handles) -> str:
        """Export multiple contacts as concatenated vCard entries."""
        return "".join(
            self.export_one(c, h) for c, h in contacts_with_handles
        )
