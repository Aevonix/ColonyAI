"""Colony Contacts — import pipeline."""

from __future__ import annotations

import csv
import io
import logging
import re
from abc import ABC, abstractmethod
from typing import List, Optional

from .config import ContactsConfig
from .importers.batch import BatchImportResult, ImportOutcome, ImportRecord
from .importers.macos_contacts import RawContactRecord
from .store import SQLiteContactStore, _normalize_phone, _normalize_email, _gen_id

logger = logging.getLogger("colony.contacts.importer")

# vCard property pattern
_VCARD_PROP = re.compile(r'^([A-Z\-]+)(?:;[^:]*)?:(.*)$')


def _parse_vcard(vcard_data: str) -> List[RawContactRecord]:
    """Parse one or more vCard 4.0 entries into RawContactRecord objects."""
    records: List[RawContactRecord] = []
    current: Optional[dict] = None
    for line in vcard_data.splitlines():
        line = line.strip()
        if line.upper() == "BEGIN:VCARD":
            current = {"phones": [], "emails": [], "given": None, "family": None, "org": None, "fn": None}
        elif line.upper() == "END:VCARD" and current is not None:
            given = current["given"]
            family = current["family"]
            display = current["fn"] or (" ".join(p for p in [given, family] if p) or None)
            if not given and display:
                parts = display.split(None, 1)
                given = parts[0] if parts else None
                family = parts[1] if len(parts) > 1 else None
            records.append(RawContactRecord(
                given_name=given,
                family_name=family,
                organization=current["org"],
                phone_numbers=current["phones"],
                email_addresses=current["emails"],
                source="vcard",
            ))
            current = None
        elif current is not None:
            m = _VCARD_PROP.match(line)
            if not m:
                continue
            prop, val = m.group(1).upper(), m.group(2).strip()
            if prop == "FN":
                current["fn"] = val
            elif prop == "N":
                parts = val.split(";")
                current["family"] = parts[0] if parts else None
                current["given"] = parts[1] if len(parts) > 1 else None
            elif prop in ("TEL", "X-TEL") or prop.startswith("TEL"):
                if val:
                    current["phones"].append(val)
            elif prop == "EMAIL":
                if val:
                    current["emails"].append(val.lower())
            elif prop == "ORG":
                current["org"] = val
    return records


def _parse_csv(csv_data: str, source: str) -> List[RawContactRecord]:
    """Parse a CSV string into RawContactRecord objects."""
    records: List[RawContactRecord] = []
    reader = csv.DictReader(io.StringIO(csv_data))
    for row in reader:
        # Support common column names
        given = row.get("given_name") or row.get("First Name") or row.get("first_name") or None
        family = row.get("family_name") or row.get("Last Name") or row.get("last_name") or None
        org = row.get("organization") or row.get("Organization") or row.get("Company") or None
        phone_raw = row.get("phone") or row.get("Phone") or row.get("primary_phone") or ""
        email_raw = row.get("email") or row.get("Email") or row.get("primary_email") or ""
        phones = [p.strip() for p in phone_raw.split("|") if p.strip()] if phone_raw else []
        emails = [e.strip().lower() for e in email_raw.split("|") if e.strip()] if email_raw else []
        if not given and not family and not phones and not emails:
            continue
        records.append(RawContactRecord(
            given_name=given or None,
            family_name=family or None,
            organization=org or None,
            phone_numbers=phones,
            email_addresses=emails,
            source=source,
        ))
    return records


# ── Abstract interface ────────────────────────────────────────────────────────

class ContactImporter(ABC):
    """Orchestrates contact import from any source through deduplication."""

    @abstractmethod
    async def import_raw(self, records: List[RawContactRecord], source: str) -> BatchImportResult:
        """Import a list of RawContactRecord objects through dedup pipeline."""

    @abstractmethod
    async def import_from_macos_contacts(self) -> BatchImportResult:
        """Import all contacts from the macOS Contacts framework."""

    @abstractmethod
    async def import_from_vcard(self, vcard_data: str) -> BatchImportResult:
        """Import contacts from a vCard 4.0 string."""

    @abstractmethod
    async def import_from_csv(self, csv_data: str, source: str = "csv") -> BatchImportResult:
        """Import contacts from a UTF-8 CSV string."""


# ── SQLite-backed implementation ──────────────────────────────────────────────

class SQLiteContactImporter(ContactImporter):
    """ContactImporter backed by SQLiteContactStore."""

    def __init__(self, store: SQLiteContactStore, config: Optional[ContactsConfig] = None) -> None:
        self._store = store
        self._config = config or ContactsConfig()

    async def import_raw(self, records: List[RawContactRecord], source: str) -> BatchImportResult:
        result = BatchImportResult(source=source, total=len(records))
        for raw in records:
            display_name = raw.display_name
            try:
                # Normalize addresses
                norm_phones = [_normalize_phone(p) for p in raw.phone_numbers if p]
                norm_emails = [_normalize_email(e) for e in raw.email_addresses if e]

                # Run dedup check
                candidates = await self._store.find_dedup_candidates(
                    raw.given_name, raw.family_name, norm_phones, norm_emails
                )

                auto_threshold = self._config.auto_merge_confidence_threshold
                proposal_threshold = self._config.merge_proposal_threshold

                if candidates and candidates[0][0] >= auto_threshold:
                    # High-confidence match — merge handles into existing contact
                    conf, existing_id, reason = candidates[0]
                    # Add any new handles to the existing contact
                    for phone in norm_phones:
                        try:
                            await self._store.add_handle(
                                existing_id, "imessage", phone,
                                confidence=conf, source=raw.source,
                            )
                        except ValueError:
                            pass  # Handle conflict handled by store
                    for email in norm_emails:
                        try:
                            await self._store.add_handle(
                                existing_id, "email", email,
                                confidence=conf, source=raw.source,
                            )
                        except ValueError:
                            pass
                    result.merged += 1
                    result.records.append(ImportRecord(
                        raw_display_name=display_name,
                        outcome=ImportOutcome.MERGED,
                        contact_id=existing_id,
                        merged_into_id=existing_id,
                    ))
                elif candidates and candidates[0][0] >= proposal_threshold:
                    # Below auto-merge but above proposal threshold — create and propose
                    contact = await self._store.create(
                        display_name=display_name,
                        given_name=raw.given_name,
                        family_name=raw.family_name,
                        organization=raw.organization,
                        import_source=raw.source,
                    )
                    for phone in norm_phones:
                        try:
                            await self._store.add_handle(contact.contact_id, "imessage", phone, source=raw.source)
                        except ValueError:
                            pass
                    for email in norm_emails:
                        try:
                            await self._store.add_handle(contact.contact_id, "email", email, source=raw.source)
                        except ValueError:
                            pass
                    # Queue merge proposal (handled by merger)
                    result.created += 1
                    result.records.append(ImportRecord(
                        raw_display_name=display_name,
                        outcome=ImportOutcome.CREATED,
                        contact_id=contact.contact_id,
                    ))
                else:
                    # No match — create new contact
                    contact = await self._store.create(
                        display_name=display_name,
                        given_name=raw.given_name,
                        family_name=raw.family_name,
                        organization=raw.organization,
                        import_source=raw.source,
                    )
                    for phone in norm_phones:
                        try:
                            await self._store.add_handle(contact.contact_id, "imessage", phone, source=raw.source)
                        except ValueError:
                            pass
                    for email in norm_emails:
                        try:
                            await self._store.add_handle(contact.contact_id, "email", email, source=raw.source)
                        except ValueError:
                            pass
                    result.created += 1
                    result.records.append(ImportRecord(
                        raw_display_name=display_name,
                        outcome=ImportOutcome.CREATED,
                        contact_id=contact.contact_id,
                    ))
            except Exception as exc:
                logger.warning("Failed to import record %r: %s", display_name, exc)
                result.failed += 1
                result.records.append(ImportRecord(
                    raw_display_name=display_name,
                    outcome=ImportOutcome.FAILED,
                    error=str(exc),
                ))
        return result

    async def import_from_macos_contacts(self) -> BatchImportResult:
        from .importers.macos_contacts import MacOSContactsImporter
        importer = MacOSContactsImporter(normalize_phones=self._config.import_cfg.normalize_phones)
        raw_records = importer.fetch_all()
        return await self.import_raw(raw_records, "ios_contacts")

    async def import_from_vcard(self, vcard_data: str) -> BatchImportResult:
        records = _parse_vcard(vcard_data)
        return await self.import_raw(records, "vcard")

    async def import_from_csv(self, csv_data: str, source: str = "csv") -> BatchImportResult:
        records = _parse_csv(csv_data, source)
        return await self.import_raw(records, source)
