"""Colony Contacts — merge engine."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .config import ContactsConfig
from .models import Contact, MergeAuditRecord, MergeProposal, more_permissive_tier, more_restrictive_privacy
from .store import SQLiteContactStore, _gen_id, _now_iso

logger = logging.getLogger("colony.contacts.merger")


class ContactMerger(ABC):
    """Manages contact deduplication and merge operations."""

    @abstractmethod
    async def find_candidates(self, contact_id: str) -> List[MergeProposal]:
        """Find pending merge proposals for a contact."""

    @abstractmethod
    async def list_proposals(self, status: str = "pending") -> List[MergeProposal]:
        """List merge proposals with given status."""

    @abstractmethod
    async def propose_merge(
        self,
        contact_id_a: str,
        contact_id_b: str,
        confidence: float,
        reason: str,
    ) -> MergeProposal:
        """Create a merge proposal for operator review."""

    @abstractmethod
    async def execute_merge(
        self,
        proposal_id: str,
        approved_by: str = "operator",
    ) -> MergeAuditRecord:
        """Execute a merge proposal atomically."""

    @abstractmethod
    async def reject_merge(
        self, proposal_id: str, reason: Optional[str] = None
    ) -> None:
        """Reject a merge proposal and prevent re-proposal of this pair."""


class SQLiteContactMerger(ContactMerger):
    """SQLite-backed ContactMerger."""

    def __init__(self, store: SQLiteContactStore, config: Optional[ContactsConfig] = None) -> None:
        self._store = store
        self._config = config or ContactsConfig()

    def _db(self):
        return self._store._require_db()

    async def find_candidates(self, contact_id: str) -> List[MergeProposal]:
        db = self._db()
        async with db.execute(
            """
            SELECT * FROM contact_merge_proposals
            WHERE (contact_id_a = ? OR contact_id_b = ?) AND status = 'pending'
            ORDER BY confidence DESC
            """,
            (contact_id, contact_id),
        ) as cur:
            rows = await cur.fetchall()
        return [MergeProposal.from_row(dict(r)) for r in rows]

    async def list_proposals(self, status: str = "pending") -> List[MergeProposal]:
        db = self._db()
        async with db.execute(
            "SELECT * FROM contact_merge_proposals WHERE status = ? ORDER BY proposed_at DESC",
            (status,),
        ) as cur:
            rows = await cur.fetchall()
        return [MergeProposal.from_row(dict(r)) for r in rows]

    async def propose_merge(
        self,
        contact_id_a: str,
        contact_id_b: str,
        confidence: float,
        reason: str,
    ) -> MergeProposal:
        db = self._db()
        # Check confirmed distinct
        async with db.execute(
            """
            SELECT id FROM contact_confirmed_distinct
            WHERE (contact_id_a = ? AND contact_id_b = ?)
               OR (contact_id_a = ? AND contact_id_b = ?)
            """,
            (contact_id_a, contact_id_b, contact_id_b, contact_id_a),
        ) as cur:
            if await cur.fetchone():
                raise ValueError(
                    f"Contacts {contact_id_a} and {contact_id_b} are confirmed distinct — cannot propose merge."
                )

        # Check if proposal already exists
        async with db.execute(
            """
            SELECT id FROM contact_merge_proposals
            WHERE (contact_id_a = ? AND contact_id_b = ?)
               OR (contact_id_a = ? AND contact_id_b = ?)
            """,
            (contact_id_a, contact_id_b, contact_id_b, contact_id_a),
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            async with db.execute(
                "SELECT * FROM contact_merge_proposals WHERE id = ?", (existing["id"],)
            ) as cur:
                row = await cur.fetchone()
            return MergeProposal.from_row(dict(row))

        proposal_id = _gen_id("cmp")
        now = _now_iso()
        await db.execute(
            """
            INSERT INTO contact_merge_proposals
              (id, contact_id_a, contact_id_b, confidence, reason, status, proposed_at)
            VALUES (?,?,?,?,?,'pending',?)
            """,
            (proposal_id, contact_id_a, contact_id_b, confidence, reason, now),
        )
        await db.commit()
        async with db.execute(
            "SELECT * FROM contact_merge_proposals WHERE id = ?", (proposal_id,)
        ) as cur:
            row = await cur.fetchone()
        return MergeProposal.from_row(dict(row))

    async def execute_merge(
        self,
        proposal_id: str,
        approved_by: str = "operator",
    ) -> MergeAuditRecord:
        """Execute a merge atomically.

        The older contact (by created_at) becomes canonical.
        All handles from the absorbed contact move to the canonical contact.
        """
        db = self._db()
        # Load proposal
        async with db.execute(
            "SELECT * FROM contact_merge_proposals WHERE id = ?", (proposal_id,)
        ) as cur:
            prop_row = await cur.fetchone()
        if not prop_row:
            raise ValueError(f"Merge proposal not found: {proposal_id}")
        proposal = MergeProposal.from_row(dict(prop_row))
        if proposal.status not in ("pending",):
            raise ValueError(f"Proposal {proposal_id} is not pending (status={proposal.status})")

        # Load both contacts (include deleted for completeness, but normally both active)
        contact_a = await self._store.get(proposal.contact_id_a)
        contact_b = await self._store.get(proposal.contact_id_b)
        if not contact_a or not contact_b:
            raise ValueError("One or both contacts not found or already deleted")

        # Determine canonical (older wins)
        if contact_a.created_at <= contact_b.created_at:
            canonical, absorbed = contact_a, contact_b
        else:
            canonical, absorbed = contact_b, contact_a

        # Snapshots before merge
        snapshot_a = canonical.to_dict()
        snapshot_b = absorbed.to_dict()

        handles_a = await self._store.get_handles(canonical.contact_id)
        handles_b = await self._store.get_handles(absorbed.contact_id)
        snapshot_a["handles"] = [h.to_dict() for h in handles_a]
        snapshot_b["handles"] = [h.to_dict() for h in handles_b]

        # Compute merged field values
        merged = _merge_fields(canonical, absorbed)

        # Execute atomically
        now = _now_iso()
        await db.execute("BEGIN EXCLUSIVE")
        try:
            # Move handles from absorbed → canonical (skip conflicts)
            for handle in handles_b:
                try:
                    await db.execute(
                        """
                        UPDATE contact_handles SET contact_id = ?
                        WHERE handle_id = ?
                          AND NOT EXISTS (
                            SELECT 1 FROM contact_handles
                            WHERE contact_id = ? AND gateway = ? AND address = ?
                          )
                        """,
                        (canonical.contact_id, handle.handle_id,
                         canonical.contact_id, handle.gateway, handle.address),
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to reassign handle %s to contact %s during merge: %s",
                        handle.handle_id, canonical.contact_id, exc,
                    )

            # Update canonical contact with merged fields
            tags_json = json.dumps(merged["tags"])
            enrichment_source_json = json.dumps(merged["enrichment_source"])
            await db.execute(
                """
                UPDATE contacts SET
                  display_name = ?, given_name = ?, family_name = ?,
                  organization = ?, trust_tier = ?, interaction_allowed = ?,
                  relationship_score = ?, tags_json = ?, privacy_level = ?,
                  person_node_id = ?, notes = ?,
                  first_seen_at = ?, last_interaction_at = ?, interaction_count = ?,
                  enrichment_source = ?, updated_at = ?
                WHERE contact_id = ?
                """,
                (
                    merged["display_name"], merged["given_name"], merged["family_name"],
                    merged["organization"],
                    merged["trust_tier"], 1 if merged["interaction_allowed"] else 0,
                    merged["relationship_score"], tags_json, merged["privacy_level"],
                    merged["person_node_id"], merged["notes"],
                    merged["first_seen_at"], merged["last_interaction_at"],
                    merged["interaction_count"],
                    enrichment_source_json, now,
                    canonical.contact_id,
                ),
            )

            # Soft-delete absorbed contact
            await db.execute(
                "UPDATE contacts SET deleted_at = ?, updated_at = ? WHERE contact_id = ?",
                (now, now, absorbed.contact_id),
            )

            # Mark proposal resolved
            await db.execute(
                "UPDATE contact_merge_proposals SET status = 'approved', resolved_at = ? WHERE id = ?",
                (now, proposal_id),
            )

            # Write merge audit record
            audit_id = _gen_id("cma")
            triggered_by = "manual" if approved_by == "operator" else "auto"
            await db.execute(
                """
                INSERT INTO contact_merge_audit
                  (id, canonical_id, absorbed_id, confidence, merge_reason,
                   triggered_by, contact_a_snapshot, contact_b_snapshot, merged_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    audit_id, canonical.contact_id, absorbed.contact_id,
                    proposal.confidence, proposal.reason,
                    triggered_by,
                    json.dumps(snapshot_a), json.dumps(snapshot_b), now,
                ),
            )

            await db.commit()
        except Exception:
            await db.rollback()
            raise

        return MergeAuditRecord(
            audit_id=audit_id,
            canonical_id=canonical.contact_id,
            absorbed_id=absorbed.contact_id,
            confidence=proposal.confidence,
            merge_reason=proposal.reason,
            triggered_by=triggered_by,
            contact_a_snapshot=snapshot_a,
            contact_b_snapshot=snapshot_b,
        )

    async def reject_merge(
        self, proposal_id: str, reason: Optional[str] = None
    ) -> None:
        db = self._db()
        async with db.execute(
            "SELECT * FROM contact_merge_proposals WHERE id = ?", (proposal_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise ValueError(f"Proposal not found: {proposal_id}")
        proposal = MergeProposal.from_row(dict(row))

        now = _now_iso()
        await db.execute(
            "UPDATE contact_merge_proposals SET status = 'rejected', resolved_at = ? WHERE id = ?",
            (now, proposal_id),
        )

        # Record confirmed distinct to prevent re-proposal
        cd_id = _gen_id("ccd")
        try:
            await db.execute(
                """
                INSERT OR IGNORE INTO contact_confirmed_distinct
                  (id, contact_id_a, contact_id_b, confirmed_at)
                VALUES (?,?,?,?)
                """,
                (cd_id, proposal.contact_id_a, proposal.contact_id_b, now),
            )
        except Exception as exc:
            logger.warning(
                "Failed to record confirmed_distinct for contacts %s / %s: %s",
                proposal.contact_id_a, proposal.contact_id_b, exc,
            )
        await db.commit()


def _merge_fields(canonical: Contact, absorbed: Contact) -> Dict[str, Any]:
    """Apply merge rules from spec §5.4."""
    # display_name: keep canonical's (it's the older/higher-trust one)
    display_name = canonical.display_name or absorbed.display_name
    given_name = canonical.given_name or absorbed.given_name
    family_name = canonical.family_name or absorbed.family_name
    organization = canonical.organization or absorbed.organization

    trust_tier = more_permissive_tier(canonical.trust_tier, absorbed.trust_tier)
    interaction_allowed = canonical.interaction_allowed or absorbed.interaction_allowed
    relationship_score = max(canonical.relationship_score, absorbed.relationship_score)
    tags = list(set(canonical.tags + absorbed.tags))
    privacy_level = more_restrictive_privacy(canonical.privacy_level, absorbed.privacy_level)

    # person_node_id: canonical's (older)
    person_node_id = canonical.person_node_id or absorbed.person_node_id

    # Timestamps
    first_seen_at = min(canonical.first_seen_at, absorbed.first_seen_at) if canonical.first_seen_at and absorbed.first_seen_at else (canonical.first_seen_at or absorbed.first_seen_at)
    last_interaction_at = _max_ts(canonical.last_interaction_at, absorbed.last_interaction_at)
    interaction_count = canonical.interaction_count + absorbed.interaction_count

    enrichment_source = list(set(canonical.enrichment_source + absorbed.enrichment_source))

    notes_parts = [n for n in [canonical.notes, absorbed.notes] if n]
    notes = "\n---\n".join(notes_parts) if notes_parts else None

    return {
        "display_name": display_name,
        "given_name": given_name,
        "family_name": family_name,
        "organization": organization,
        "trust_tier": trust_tier,
        "interaction_allowed": interaction_allowed,
        "relationship_score": relationship_score,
        "tags": tags,
        "privacy_level": privacy_level,
        "person_node_id": person_node_id,
        "notes": notes,
        "first_seen_at": first_seen_at,
        "last_interaction_at": last_interaction_at,
        "interaction_count": interaction_count,
        "enrichment_source": enrichment_source,
    }


def _max_ts(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a:
        return b
    if not b:
        return a
    return a if a >= b else b
