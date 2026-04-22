"""Colony Contacts — block manager."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .store import SQLiteContactStore, _gen_id, _now_iso

logger = logging.getLogger("colony.contacts.block_manager")


class BlockManager(ABC):
    """Manages the contact blocklist.

    Blocking operates at the contact_id level, not the handle level.
    Blocking a contact blocks all their associated handles.
    """

    @abstractmethod
    async def block(
        self,
        contact_id: str,
        reason: Optional[str] = None,
        blocked_by: str = "operator",
    ) -> None:
        """Block a contact, preventing all gateway interactions."""

    @abstractmethod
    async def unblock(self, contact_id: str, reason: Optional[str] = None) -> None:
        """Remove a block from a contact."""

    @abstractmethod
    async def is_blocked(self, contact_id: str) -> bool:
        """Return True if the contact is currently blocked."""

    @abstractmethod
    async def is_blocked_by_handle(self, gateway: str, address: str) -> bool:
        """Return True if the given gateway handle belongs to a blocked contact."""

    @abstractmethod
    async def list_blocked(self) -> List[Dict[str, Any]]:
        """Return all currently blocked contacts."""


class SQLiteBlockManager(BlockManager):
    """SQLite-backed BlockManager."""

    def __init__(self, store: SQLiteContactStore) -> None:
        self._store = store

    def _db(self):
        return self._store._require_db()

    async def block(
        self,
        contact_id: str,
        reason: Optional[str] = None,
        blocked_by: str = "operator",
    ) -> None:
        db = self._db()
        now = _now_iso()
        # Upsert: if already blocked, update; otherwise insert
        async with db.execute(
            "SELECT contact_id FROM contact_blocklist WHERE contact_id = ?", (contact_id,)
        ) as cur:
            existing = await cur.fetchone()

        if existing:
            # Re-block (clear unblocked_at)
            await db.execute(
                "UPDATE contact_blocklist SET reason = ?, blocked_by = ?, blocked_at = ?, unblocked_at = NULL WHERE contact_id = ?",
                (reason, blocked_by, now, contact_id),
            )
        else:
            await db.execute(
                "INSERT INTO contact_blocklist (contact_id, reason, blocked_by, blocked_at) VALUES (?,?,?,?)",
                (contact_id, reason, blocked_by, now),
            )

        # Also set interaction_allowed = 0
        await db.execute(
            "UPDATE contacts SET interaction_allowed = 0, updated_at = ? WHERE contact_id = ?",
            (now, contact_id),
        )
        await db.commit()
        await self._store.record_audit(
            contact_id, "blocked",
            {"reason": reason, "blocked_by": blocked_by},
            performed_by=blocked_by,
        )

    async def unblock(self, contact_id: str, reason: Optional[str] = None) -> None:
        db = self._db()
        now = _now_iso()
        await db.execute(
            "UPDATE contact_blocklist SET unblocked_at = ? WHERE contact_id = ? AND unblocked_at IS NULL",
            (now, contact_id),
        )
        await db.commit()
        await self._store.record_audit(
            contact_id, "unblocked", {"reason": reason}
        )

    async def is_blocked(self, contact_id: str) -> bool:
        db = self._db()
        async with db.execute(
            "SELECT contact_id FROM contact_blocklist WHERE contact_id = ? AND unblocked_at IS NULL",
            (contact_id,),
        ) as cur:
            row = await cur.fetchone()
        return row is not None

    async def is_blocked_by_handle(self, gateway: str, address: str) -> bool:
        db = self._db()
        async with db.execute(
            """
            SELECT bl.contact_id
            FROM contact_blocklist bl
            JOIN contact_handles h ON h.contact_id = bl.contact_id
            WHERE h.gateway = ? AND h.address = ? AND bl.unblocked_at IS NULL
            """,
            (gateway, address),
        ) as cur:
            row = await cur.fetchone()
        return row is not None

    async def list_blocked(self) -> List[Dict[str, Any]]:
        db = self._db()
        async with db.execute(
            "SELECT * FROM contact_blocklist WHERE unblocked_at IS NULL ORDER BY blocked_at DESC",
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
