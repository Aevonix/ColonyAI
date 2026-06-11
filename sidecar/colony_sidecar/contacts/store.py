"""Colony Contacts — SQLite-backed ContactStore."""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from .config import ContactsConfig
from .models import (
    Contact,
    ContactHandle,
    TRUST_TIERS,
    TIER_DEFAULT_INTERACTION,
    more_permissive_tier,
)

logger = logging.getLogger("colony.contacts.store")

_SCHEMA_FILE = Path(__file__).parent / "migrations" / "001_contacts_schema.sql"

_PHONE_DIGITS = re.compile(r'\D')


def _gen_id(prefix: str) -> str:
    import secrets
    ts = int(time.time() * 1000)
    rand = secrets.token_hex(6)  # 12 hex chars, 48 bits of CSPRNG entropy
    return f"{prefix}-{ts}-{rand}"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_phone(phone: str) -> str:
    """Strip non-digit characters; preserve leading +."""
    phone = phone.strip()
    if phone.startswith("+"):
        return "+" + _PHONE_DIGITS.sub("", phone[1:])
    return _PHONE_DIGITS.sub("", phone)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _name_similarity(a: Optional[str], b: Optional[str]) -> float:
    """Simple character bigram similarity in [0, 1]."""
    if not a or not b:
        return 0.0
    a, b = a.lower(), b.lower()
    if a == b:
        return 1.0

    def bigrams(s):
        return {s[i:i+2] for i in range(len(s) - 1)}

    bg_a, bg_b = bigrams(a), bigrams(b)
    if not bg_a or not bg_b:
        return 0.0
    return 2.0 * len(bg_a & bg_b) / (len(bg_a) + len(bg_b))


# ── Abstract interface ────────────────────────────────────────────────────────

class ContactStore(ABC):
    """Primary read/write interface for the contact store."""

    @abstractmethod
    async def get(self, contact_id: str) -> Optional[Contact]:
        """Fetch a contact by canonical ID. Returns None if not found or deleted."""

    @abstractmethod
    async def resolve_handle(self, gateway: str, address: str) -> Optional[Contact]:
        """Resolve a gateway handle to a Contact."""

    @abstractmethod
    async def create(
        self,
        display_name: Optional[str] = None,
        given_name: Optional[str] = None,
        family_name: Optional[str] = None,
        organization: Optional[str] = None,
        trust_tier: str = "unknown",
        interaction_allowed: Optional[bool] = None,
        tags: Optional[List[str]] = None,
        privacy_level: str = "private",
        import_source: str = "manual",
        notes: Optional[str] = None,
    ) -> Contact:
        """Create a new contact record."""

    @abstractmethod
    async def add_handle(
        self,
        contact_id: str,
        gateway: str,
        address: str,
        is_primary: bool = False,
        confidence: float = 1.0,
        source: str = "manual",
        verified: bool = False,
    ) -> ContactHandle:
        """Add a gateway handle to an existing contact."""

    @abstractmethod
    async def get_handles(self, contact_id: str) -> List[ContactHandle]:
        """Return all handles for a contact."""

    @abstractmethod
    async def update_tier(
        self,
        contact_id: str,
        new_tier: str,
        reason: Optional[str] = None,
        performed_by: str = "operator",
    ) -> None:
        """Update a contact's trust tier and record the change in audit."""

    @abstractmethod
    async def update_relationship_score(self, contact_id: str, score: float) -> None:
        """Update the relationship_score for a contact (0.0–1.0)."""

    @abstractmethod
    async def update_interaction_allowed(
        self, contact_id: str, allowed: bool, performed_by: str = "operator"
    ) -> None:
        """Toggle the interaction_allowed flag."""

    @abstractmethod
    async def soft_delete(
        self, contact_id: str, reason: Optional[str] = None, performed_by: str = "operator"
    ) -> None:
        """Soft-delete a contact."""

    @abstractmethod
    async def hard_delete(self, contact_id: str, performed_by: str = "system") -> None:
        """Permanently delete a contact record."""

    @abstractmethod
    async def list(
        self,
        trust_tier: Optional[str] = None,
        interaction_allowed: Optional[bool] = None,
        tag: Optional[str] = None,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Contact]:
        """List contacts with optional filtering."""

    @abstractmethod
    async def find_by_name(self, name: str, threshold: float = 0.5) -> List[Contact]:
        """Find contacts whose display_name is similar to name."""

    @abstractmethod
    async def update(self, contact_id: str, **fields) -> Optional[Contact]:
        """Update arbitrary fields on a contact."""

    @abstractmethod
    async def record_audit(
        self,
        contact_id: str,
        action: str,
        detail: Optional[Dict[str, Any]] = None,
        performed_by: str = "system",
    ) -> None:
        """Write an audit record."""

    @abstractmethod
    async def connect(self) -> None:
        """Open the database connection."""

    @abstractmethod
    async def close(self) -> None:
        """Close the database connection."""

    async def __aenter__(self) -> "ContactStore":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()


# ── SQLite implementation ─────────────────────────────────────────────────────

class SQLiteContactStore(ContactStore):
    """SQLite-backed implementation of ContactStore."""

    def __init__(self, config: Optional[ContactsConfig] = None, graph=None) -> None:
        self._config = config or ContactsConfig()
        self._db: Optional[aiosqlite.Connection] = None
        self._graph = graph  # Optional ColonyGraph for score sync

    async def connect(self) -> None:
        path = self._config.sqlite_path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        schema = _SCHEMA_FILE.read_text()
        await self._db.executescript(schema)
        await self._db.commit()
        await self._apply_migrations()

    async def _apply_migrations(self) -> None:
        """Idempotent additive migrations for DBs created before a column existed.

        SQLite has no ADD COLUMN IF NOT EXISTS, so we introspect first.
        """
        db = self._db
        assert db is not None
        async with db.execute("PRAGMA table_info(contacts)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "timezone" not in cols:  # v0.21.0 — per-contact timezone
            await db.execute("ALTER TABLE contacts ADD COLUMN timezone TEXT")
            await db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    def _require_db(self) -> aiosqlite.Connection:
        if not self._db:
            raise RuntimeError("ContactStore not connected. Use async with or call connect().")
        return self._db

    # ── Read ops ──────────────────────────────────────────────────────────────

    async def get(self, contact_id: str) -> Optional[Contact]:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM contacts WHERE contact_id = ? AND deleted_at IS NULL",
            (contact_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Contact.from_row(dict(row))

    async def resolve_handle(self, gateway: str, address: str) -> Optional[Contact]:
        db = self._require_db()
        norm = _normalize_email(address) if gateway == "email" else _normalize_phone(address) if gateway in ("imessage", "sms", "signal") else address
        async with db.execute(
            """
            SELECT c.* FROM contacts c
            JOIN contact_handles h ON h.contact_id = c.contact_id
            WHERE h.gateway = ? AND h.address = ? AND c.deleted_at IS NULL
            """,
            (gateway, norm),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Contact.from_row(dict(row))

    async def get_handles(self, contact_id: str) -> List[ContactHandle]:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM contact_handles WHERE contact_id = ? ORDER BY is_primary DESC, created_at",
            (contact_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [ContactHandle.from_row(dict(r)) for r in rows]

    async def list(
        self,
        trust_tier: Optional[str] = None,
        interaction_allowed: Optional[bool] = None,
        tag: Optional[str] = None,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Contact]:
        db = self._require_db()
        clauses = []
        params: List[Any] = []
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        if trust_tier:
            clauses.append("trust_tier = ?")
            params.append(trust_tier)
        if interaction_allowed is not None:
            clauses.append("interaction_allowed = ?")
            params.append(1 if interaction_allowed else 0)
        if tag:
            # SQL-02: escape LIKE wildcards to prevent contact enumeration
            safe_tag = tag.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
            clauses.append("tags_json LIKE ? ESCAPE '\\'")
            params.append(f'%"{safe_tag}"%')
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params += [limit, offset]
        async with db.execute(
            f"SELECT * FROM contacts {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [Contact.from_row(dict(r)) for r in rows]

    async def find_by_name(self, name: str, threshold: float = 0.5) -> List[Contact]:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM contacts WHERE deleted_at IS NULL",
        ) as cur:
            rows = await cur.fetchall()
        results = []
        for row in rows:
            c = Contact.from_row(dict(row))
            sim = _name_similarity(name, c.display_name)
            if sim >= threshold:
                results.append((sim, c))
        results.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in results]

    async def find_by_person_node_id(self, person_node_id: str) -> Optional[Contact]:
        """Fetch a contact by its linked Neo4j Person node ID."""
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM contacts WHERE person_node_id = ? AND deleted_at IS NULL",
            (person_node_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Contact.from_row(dict(row))

    async def find_discovered_by_handle(
        self, gateway: str, address: str
    ) -> Optional[Contact]:
        """Find a discovered (world_model) contact that owns a given handle."""
        db = self._require_db()
        norm = _normalize_email(address) if gateway == "email" else _normalize_phone(address) if gateway in ("imessage", "sms", "signal") else address
        async with db.execute(
            """
            SELECT c.* FROM contacts c
            JOIN contact_handles h ON h.contact_id = c.contact_id
            WHERE c.import_source = 'world_model'
              AND c.deleted_at IS NULL
              AND h.gateway = ? AND h.address = ?
            LIMIT 1
            """,
            (gateway, norm),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Contact.from_row(dict(row))

    # ── Write ops ─────────────────────────────────────────────────────────────

    async def create(
        self,
        display_name: Optional[str] = None,
        given_name: Optional[str] = None,
        family_name: Optional[str] = None,
        organization: Optional[str] = None,
        trust_tier: str = "unknown",
        interaction_allowed: Optional[bool] = None,
        tags: Optional[List[str]] = None,
        privacy_level: str = "private",
        import_source: str = "manual",
        notes: Optional[str] = None,
    ) -> Contact:
        db = self._require_db()
        if trust_tier not in TRUST_TIERS:
            raise ValueError(f"Invalid trust_tier: {trust_tier}")
        if interaction_allowed is None:
            interaction_allowed = TIER_DEFAULT_INTERACTION.get(trust_tier, True)
        contact_id = _gen_id("cid")
        now = _now_iso()
        dn = display_name
        if not dn and (given_name or family_name):
            dn = " ".join(p for p in [given_name, family_name] if p)
        await db.execute(
            """
            INSERT INTO contacts
              (contact_id, display_name, given_name, family_name, organization,
               trust_tier, interaction_allowed, tags_json, privacy_level,
               import_source, notes, first_seen_at, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                contact_id, dn, given_name, family_name, organization,
                trust_tier, 1 if interaction_allowed else 0,
                json.dumps(tags or []), privacy_level,
                import_source, notes, now, now, now,
            ),
        )
        await db.commit()
        await self.record_audit(contact_id, "created", {"import_source": import_source})
        contact = await self.get(contact_id)
        assert contact is not None
        return contact

    async def add_handle(
        self,
        contact_id: str,
        gateway: str,
        address: str,
        is_primary: bool = False,
        confidence: float = 1.0,
        source: str = "manual",
        verified: bool = False,
    ) -> ContactHandle:
        db = self._require_db()
        # Normalize address
        if gateway == "email":
            address = _normalize_email(address)
        elif gateway in ("imessage", "sms", "signal"):
            address = _normalize_phone(address)

        # Check if address already belongs to another contact
        async with db.execute(
            "SELECT contact_id FROM contact_handles WHERE gateway = ? AND address = ?",
            (gateway, address),
        ) as cur:
            existing = await cur.fetchone()
        if existing and existing["contact_id"] != contact_id:
            raise ValueError(
                f"Handle ({gateway}, {address}) is already assigned to contact {existing['contact_id']}"
            )
        if existing and existing["contact_id"] == contact_id:
            # Already exists for this contact — return it
            async with db.execute(
                "SELECT * FROM contact_handles WHERE gateway = ? AND address = ?",
                (gateway, address),
            ) as cur:
                row = await cur.fetchone()
            return ContactHandle.from_row(dict(row))

        handle_id = _gen_id("hdl")
        now = _now_iso()
        await db.execute(
            """
            INSERT INTO contact_handles
              (handle_id, contact_id, gateway, address, is_primary, verified, confidence, source, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (handle_id, contact_id, gateway, address, 1 if is_primary else 0,
             1 if verified else 0, confidence, source, now),
        )
        await db.commit()
        await self.record_audit(
            contact_id, "handle_added",
            {"gateway": gateway, "address": address, "source": source},
        )
        async with db.execute(
            "SELECT * FROM contact_handles WHERE handle_id = ?", (handle_id,)
        ) as cur:
            row = await cur.fetchone()
        return ContactHandle.from_row(dict(row))

    async def update_tier(
        self,
        contact_id: str,
        new_tier: str,
        reason: Optional[str] = None,
        performed_by: str = "operator",
    ) -> None:
        db = self._require_db()
        if new_tier not in TRUST_TIERS:
            raise ValueError(f"Invalid trust_tier: {new_tier}")
        async with db.execute(
            "SELECT trust_tier FROM contacts WHERE contact_id = ?", (contact_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise ValueError(f"Contact not found: {contact_id}")
        old_tier = row["trust_tier"]
        await db.execute(
            "UPDATE contacts SET trust_tier = ?, updated_at = ? WHERE contact_id = ?",
            (new_tier, _now_iso(), contact_id),
        )
        await db.commit()
        await self.record_audit(
            contact_id, "tier_changed",
            {"old_tier": old_tier, "new_tier": new_tier, "reason": reason},
            performed_by=performed_by,
        )

    async def update_relationship_score(self, contact_id: str, score: float) -> None:
        db = self._require_db()
        score = max(0.0, min(1.0, score))
        await db.execute(
            "UPDATE contacts SET relationship_score = ?, updated_at = ? WHERE contact_id = ?",
            (score, _now_iso(), contact_id),
        )
        await db.commit()
        # Sync to graph if linked
        if self._graph is not None:
            try:
                contact = await self.get(contact_id)
                if contact and contact.person_node_id:
                    await self._graph.update_person(
                        contact.person_node_id, score=score,
                    )
            except Exception as exc:
                logger.debug("Score sync to graph failed for %s: %s", contact_id, exc)

    async def update_interaction_allowed(
        self, contact_id: str, allowed: bool, performed_by: str = "operator"
    ) -> None:
        db = self._require_db()
        await db.execute(
            "UPDATE contacts SET interaction_allowed = ?, updated_at = ? WHERE contact_id = ?",
            (1 if allowed else 0, _now_iso(), contact_id),
        )
        await db.commit()
        await self.record_audit(
            contact_id, "interaction_toggled",
            {"interaction_allowed": allowed},
            performed_by=performed_by,
        )

    async def compute_cadence_overdue(
        self,
        *,
        now_iso: Optional[str] = None,
        default_cadence_days: float = 7.0,
        factor: float = 1.5,
        min_silence_days: float = 2.0,
        overdue_only: bool = True,
        limit: int = 20,
        exclude_ids: Optional[set] = None,
    ) -> List[Dict[str, Any]]:
        """Per-contact rhythm + silence (v0.21.0), SQLite-based.

        Estimates each contact's typical cadence from their own interaction
        history (active span / interactions) and flags those overdue relative
        to *their* rhythm — so a daily contact is overdue after a few days while
        a monthly one isn't for weeks. Independent of the Neo4j graph.
        """
        from colony_sidecar.util import temporal as _t
        db = self._require_db()
        now = _t.parse_iso(now_iso) or _t.now_utc()
        exclude = set(exclude_ids or [])
        async with db.execute(
            "SELECT contact_id, display_name, given_name, first_seen_at, "
            "last_interaction_at, interaction_count, timezone "
            "FROM contacts WHERE deleted_at IS NULL AND interaction_allowed = 1 "
            "AND last_interaction_at IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()

        out: List[Dict[str, Any]] = []
        for r in rows:
            cid = r["contact_id"]
            if cid in exclude:
                continue
            last = _t.parse_iso(r["last_interaction_at"])
            if last is None:
                continue
            first = _t.parse_iso(r["first_seen_at"])
            count = int(r["interaction_count"] or 0)
            days_since = (now - last).total_seconds() / 86400.0
            # cadence estimate
            if count >= 2 and first is not None and last > first:
                span = (last - first).total_seconds() / 86400.0
                cadence = span / max(count - 1, 1)
            else:
                cadence = default_cadence_days
            cadence = max(0.5, min(cadence, 90.0))
            threshold = max(min_silence_days, cadence * factor)
            is_overdue = days_since > threshold
            if overdue_only and not is_overdue:
                continue
            out.append({
                "contact_id": cid,
                "name": r["display_name"] or r["given_name"] or cid,
                "timezone": r["timezone"],
                "last_interaction_at": r["last_interaction_at"],
                "days_since": round(days_since, 1),
                "cadence_days": round(cadence, 1),
                "overdue": is_overdue,
                "overdue_ratio": round(days_since / max(cadence, 0.5), 2),
            })

        out.sort(key=lambda x: x["overdue_ratio"], reverse=True)
        return out[:limit]

    async def record_interaction(self, contact_id: str, at_iso: Optional[str] = None) -> bool:
        """Bump last_interaction_at (+count) for a contact. v0.21.0.

        Returns True if a row was updated (i.e. the contact exists).
        """
        db = self._require_db()
        ts = at_iso or _now_iso()
        cur = await db.execute(
            "UPDATE contacts SET last_interaction_at = ?, "
            "interaction_count = interaction_count + 1, updated_at = ? "
            "WHERE contact_id = ? AND deleted_at IS NULL",
            (ts, ts, contact_id),
        )
        await db.commit()
        return cur.rowcount > 0

    async def set_timezone(
        self, contact_id: str, timezone: Optional[str], performed_by: str = "operator"
    ) -> None:
        """Set (or clear, with None) a contact's IANA timezone. v0.21.0."""
        from colony_sidecar.util.temporal import is_valid_timezone
        if timezone is not None and not is_valid_timezone(timezone):
            raise ValueError(f"Invalid IANA timezone: {timezone!r}")
        db = self._require_db()
        async with db.execute(
            "SELECT timezone FROM contacts WHERE contact_id = ?", (contact_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise ValueError(f"Contact not found: {contact_id}")
        old_tz = row["timezone"]
        await db.execute(
            "UPDATE contacts SET timezone = ?, updated_at = ? WHERE contact_id = ?",
            (timezone, _now_iso(), contact_id),
        )
        await db.commit()
        await self.record_audit(
            contact_id, "timezone_changed",
            {"old_timezone": old_tz, "new_timezone": timezone},
            performed_by=performed_by,
        )

    async def soft_delete(
        self, contact_id: str, reason: Optional[str] = None, performed_by: str = "operator"
    ) -> None:
        db = self._require_db()
        now = _now_iso()
        await db.execute(
            "UPDATE contacts SET deleted_at = ?, updated_at = ? WHERE contact_id = ?",
            (now, now, contact_id),
        )
        await db.commit()
        await self.record_audit(
            contact_id, "soft_deleted", {"reason": reason}, performed_by=performed_by
        )

    async def hard_delete(self, contact_id: str, performed_by: str = "system") -> None:
        db = self._require_db()
        await self.record_audit(
            contact_id, "hard_deleted", {}, performed_by=performed_by
        )
        await db.execute("DELETE FROM contacts WHERE contact_id = ?", (contact_id,))
        await db.commit()

    async def update(self, contact_id: str, **fields) -> Optional[Contact]:
        db = self._require_db()
        allowed_fields = {
            "display_name", "given_name", "family_name", "organization",
            "notes", "person_node_id", "privacy_level",
            "last_interaction_at", "interaction_count",
            "enrichment_source", "enrichment_last_at",
        }
        set_parts = []
        params = []
        for k, v in fields.items():
            if k not in allowed_fields:
                continue
            if k == "enrichment_source" and isinstance(v, list):
                v = json.dumps(v)
            # SQL-01: column name is validated against allowed_fields; double-quote the
            # identifier so SQLite treats it safely even if allowed_fields is later extended.
            set_parts.append(f'"{k}" = ?')
            params.append(v)
        if not set_parts:
            return await self.get(contact_id)
        set_parts.append("updated_at = ?")
        params.append(_now_iso())
        params.append(contact_id)
        await db.execute(
            f"UPDATE contacts SET {', '.join(set_parts)} WHERE contact_id = ?",
            params,
        )
        await db.commit()
        return await self.get(contact_id)

    async def record_audit(
        self,
        contact_id: str,
        action: str,
        detail: Optional[Dict[str, Any]] = None,
        performed_by: str = "system",
    ) -> None:
        db = self._require_db()
        audit_id = _gen_id("cau")
        now = _now_iso()
        await db.execute(
            "INSERT INTO contact_audit (id, contact_id, action, detail, performed_by, created_at) VALUES (?,?,?,?,?,?)",
            (audit_id, contact_id, action, json.dumps(detail or {}), performed_by, now),
        )
        await db.commit()

    async def get_audit_log(self, contact_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM contact_audit WHERE contact_id = ? ORDER BY created_at DESC LIMIT ?",
            (contact_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Deduplication helpers ─────────────────────────────────────────────────

    async def find_by_handle(self, gateway: str, address: str) -> Optional[Contact]:
        """Find contact by exact handle (including soft-deleted contacts)."""
        db = self._require_db()
        async with db.execute(
            """
            SELECT c.* FROM contacts c
            JOIN contact_handles h ON h.contact_id = c.contact_id
            WHERE h.gateway = ? AND h.address = ?
            """,
            (gateway, address),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Contact.from_row(dict(row))

    async def find_dedup_candidates(
        self, given_name: Optional[str], family_name: Optional[str],
        phones: List[str], emails: List[str],
    ) -> List[tuple]:
        """Return list of (confidence, contact_id, reason) tuples."""
        candidates = []
        seen_ids: set = set()

        # Normalize
        norm_phones = [_normalize_phone(p) for p in phones if p]
        norm_emails = [_normalize_email(e) for e in emails if e]

        # 1. Exact phone match
        for phone in norm_phones:
            contact = await self.resolve_handle("imessage", phone)
            if contact is None:
                contact = await self.resolve_handle("sms", phone)
            if contact and contact.contact_id not in seen_ids:
                seen_ids.add(contact.contact_id)
                candidates.append((0.99, contact.contact_id, f"exact_phone:{phone}"))

        # 2. Exact email match
        for email in norm_emails:
            contact = await self.resolve_handle("email", email)
            if contact and contact.contact_id not in seen_ids:
                seen_ids.add(contact.contact_id)
                candidates.append((0.99, contact.contact_id, f"exact_email:{email}"))

        # 3. Fuzzy name similarity
        display = " ".join(p for p in [given_name, family_name] if p)
        if display:
            name_matches = await self.find_by_name(display, threshold=0.4)
            for c in name_matches:
                if c.contact_id not in seen_ids:
                    sim = _name_similarity(display, c.display_name)
                    if sim >= 0.4:
                        seen_ids.add(c.contact_id)
                        candidates.append((sim * 0.7, c.contact_id, f"name_similarity:{sim:.2f}"))

        return sorted(candidates, key=lambda x: x[0], reverse=True)
