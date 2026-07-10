"""Owner pair-approvals for level-2 tom2 (L2.4) — M3, per pair, TTL'd.

The approval unit is a (reader, subject) PAIR, not a message: the owner
decides once that "epistemic lines about SUBJECT may render to READER",
asynchronously, and the decision expires on its own
(COLONY_TOM2_APPROVAL_TTL_DAYS, default 30). Per-message prompts were
rejected as approval fatigue (T10) — the budgets in the exposure ledger
bind frequency anyway.

Approvals ride the existing ProposalStore so they surface to the owner
through the same guarded proposal path as everything else Colony wants
reviewed: ``request_pair`` files a proposal; the owner approves/revokes via
the host endpoints. ``is_approved`` is the hook the L2.1 eligibility
pipeline consumes — it answers False on ANY doubt: no store, no matching
row, expired TTL, malformed ids, storage errors.

Refs-not-content applies here too: a pair row carries two contact ids and
nothing else — prose is refused at the boundary.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Machine-readable pair marker carried in Proposal.source.
_SOURCE_PREFIX = "tom2-pair:"

#: Contact ids are opaque tokens; prose is refused (refs-not-content).
_CID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

APPROVED = "approved"
REVOKED = "revoked"
PENDING = "shadow"           # ProposalStore's initial status


def approval_ttl_days() -> float:
    """COLONY_TOM2_APPROVAL_TTL_DAYS (default 30): how long an approval
    stays live. Malformed values read as the default; negative values read
    as 0 (nothing is approved)."""
    try:
        return max(0.0, float(
            os.environ.get("COLONY_TOM2_APPROVAL_TTL_DAYS", "30")))
    except (TypeError, ValueError):
        return 30.0


def _validate_cid(cid: Any, what: str) -> str:
    cid = str(cid or "").strip()
    if not _CID_RE.match(cid):
        raise ValueError(f"tom2 approval {what} {cid[:40]!r} is not an "
                         "opaque contact id")
    return cid


def _pair_source(reader: str, subject: str) -> str:
    return f"{_SOURCE_PREFIX}{reader}|{subject}"


class Tom2ApprovalRegistry:
    """Pair-approval registry over an injected ProposalStore."""

    def __init__(self, proposal_store: Any) -> None:
        self._store = proposal_store

    # -- internals ----------------------------------------------------------
    def _find(self, reader: str, subject: str) -> Optional[Any]:
        """Latest proposal for this exact pair, or None."""
        source = _pair_source(reader, subject)
        rows = [p for p in self._store.list(limit=100000)
                if getattr(p, "source", "") == source]
        rows.sort(key=lambda p: float(getattr(p, "created_at", 0) or 0))
        return rows[-1] if rows else None

    # -- writes -------------------------------------------------------------
    def request_pair(self, reader: str, subject: str) -> Any:
        """File (or return the existing) approval request for a pair. The
        proposal rides the normal guarded proposal path to the owner."""
        from colony_sidecar.proposals import Proposal

        reader = _validate_cid(reader, "reader")
        subject = _validate_cid(subject, "subject")
        existing = self._find(reader, subject)
        if existing is not None and existing.status in (PENDING, APPROVED):
            return existing
        p = Proposal(
            title=f"tom2 pair approval: {reader} -> {subject}"[:100],
            finding=(f"Level-2 theory-of-mind wants permission to render "
                     f"epistemic lines about {subject} (only facts the "
                     f"reader already holds) to {reader}."),
            why_it_helps=("You approve the PAIR once (30d TTL); budgets "
                          "still bind per rendering."),
            suggested_action=(f"POST /v1/host/tom2/approvals with "
                              f"reader={reader} subject={subject} "
                              "action=approve (or revoke)"),
            source=_pair_source(reader, subject),
            initiative_type="tom2_pair",
            confidence=0.5,
        )
        return self._store.add(p)

    def approve_pair(self, reader: str, subject: str) -> Any:
        """Owner approval: (re)stamps the pair approved with a FRESH TTL."""
        reader = _validate_cid(reader, "reader")
        subject = _validate_cid(subject, "subject")
        p = self._find(reader, subject)
        if p is None:
            p = self.request_pair(reader, subject)
        p.status = APPROVED
        p.created_at = time.time()          # TTL runs from the approval
        return self._store.add(p)           # same id => upsert

    def revoke_pair(self, reader: str, subject: str) -> bool:
        reader = _validate_cid(reader, "reader")
        subject = _validate_cid(subject, "subject")
        p = self._find(reader, subject)
        if p is None:
            return False
        p.status = REVOKED
        self._store.add(p)
        return True

    # -- the eligibility hook -------------------------------------------------
    def is_approved(self, reader: str, subject: str) -> bool:
        """The L2.1 approval hook. False on ANY doubt: missing store, no
        row, wrong status, expired TTL, malformed ids, storage errors."""
        try:
            if self._store is None:
                return False
            reader = _validate_cid(reader, "reader")
            subject = _validate_cid(subject, "subject")
            p = self._find(reader, subject)
            if p is None or p.status != APPROVED:
                return False
            age_days = (time.time()
                        - float(getattr(p, "created_at", 0) or 0)) / 86400.0
            return 0 <= age_days <= approval_ttl_days()
        except Exception:
            logger.debug("tom2 approval check failed (=> not approved)",
                         exc_info=True)
            return False

    # -- owner reads ------------------------------------------------------------
    def list_pairs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """All pair rows (any status) with live validity, newest first."""
        out: List[Dict[str, Any]] = []
        try:
            rows = [p for p in self._store.list(limit=100000)
                    if str(getattr(p, "source", "")
                           ).startswith(_SOURCE_PREFIX)]
        except Exception:
            return out
        rows.sort(key=lambda p: float(getattr(p, "created_at", 0) or 0),
                  reverse=True)
        for p in rows[:max(1, int(limit))]:
            pair = p.source[len(_SOURCE_PREFIX):]
            reader, _, subject = pair.partition("|")
            out.append({
                "reader_contact_id": reader,
                "subject_contact_id": subject,
                "status": p.status,
                "approved": self.is_approved(reader, subject),
                "created_at": p.created_at,
                "proposal_id": p.id,
            })
        return out
