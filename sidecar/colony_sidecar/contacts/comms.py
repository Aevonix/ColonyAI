"""Cross-channel communication ledger + outreach governance.

Gives the agent the WHOLE picture of its relationship traffic with a contact —
every inbound/outbound exchange across every channel (WhatsApp now, email/SMS
later) — and a principled decision on whether / how / when to (re)initiate, so it
never spams and always references the last discussion + open follow-ups before
reaching out. Proactive outreach to anyone but the owner is gated on owner
approval by policy.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


class CommsLog:
    """SQLite ledger of communications with each contact, across all channels."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS communications (
                id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT 'unknown',
                direction TEXT NOT NULL,        -- 'in' (they->us) | 'out' (us->them)
                summary TEXT,
                session_id TEXT,
                ts TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_comms_contact ON communications(contact_id, ts);
            """
        )
        self._conn.commit()

    def log(self, contact_id: str, *, channel: str = "unknown", direction: str = "in",
            summary: str = "", session_id: str = "") -> None:
        if not contact_id or direction not in ("in", "out"):
            return
        self._conn.execute(
            "INSERT INTO communications (id, contact_id, channel, direction, summary, session_id, ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, contact_id, channel or "unknown", direction,
             (summary or "")[:500], session_id or "", _now().isoformat()),
        )
        self._conn.commit()

    def history(self, contact_id: str, limit: int = 15) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT channel, direction, summary, ts FROM communications WHERE contact_id=?"
            " ORDER BY ts DESC LIMIT ?", (contact_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def last_per_channel(self, contact_id: str) -> Dict[str, Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT channel, direction, summary, MAX(ts) AS ts FROM communications"
            " WHERE contact_id=? GROUP BY channel", (contact_id,)).fetchall()
        return {r["channel"]: {"direction": r["direction"], "summary": r["summary"], "ts": r["ts"]}
                for r in rows}

    def last_outbound(self, contact_id: str) -> Optional[Dict[str, Any]]:
        r = self._conn.execute(
            "SELECT channel, summary, ts FROM communications WHERE contact_id=? AND direction='out'"
            " ORDER BY ts DESC LIMIT 1", (contact_id,)).fetchone()
        return dict(r) if r else None

    def inbound_since(self, contact_id: str, since_iso: str) -> List[str]:
        """Timestamps of inbound rows from a contact since an ISO instant
        (selfhood benchmark: did the owner respond after a delivery)."""
        rows = self._conn.execute(
            "SELECT ts FROM communications WHERE contact_id=? AND"
            " direction='in' AND ts >= ? ORDER BY ts ASC LIMIT 5000",
            (contact_id, since_iso)).fetchall()
        return [r["ts"] for r in rows]

    def counts(self, contact_id: str) -> Dict[str, Any]:
        r = self._conn.execute(
            "SELECT SUM(direction='in') AS inbound, SUM(direction='out') AS outbound,"
            " COUNT(DISTINCT channel) AS channels FROM communications WHERE contact_id=?",
            (contact_id,)).fetchone()
        return {"inbound": r["inbound"] or 0, "outbound": r["outbound"] or 0,
                "channels": r["channels"] or 0}

    def stats(self, contact_id: str, *, since_days: int = 90) -> Dict[str, Any]:
        """Channel-usage counts and interaction-hour histogram (UTC hours;
        the consumer shifts into the contact's timezone). Feeds the
        relationship profiler's approach guidance (preferred channel,
        best time to reach)."""
        rows = self._conn.execute(
            "SELECT channel, ts FROM communications"
            " WHERE contact_id=? AND ts >= datetime('now', ?)",
            (contact_id, f"-{int(since_days)} day")).fetchall()
        channels: Dict[str, int] = {}
        hours = [0] * 24
        for r in rows:
            channels[r["channel"]] = channels.get(r["channel"], 0) + 1
            try:
                hours[int(str(r["ts"])[11:13])] += 1
            except (ValueError, IndexError):
                pass
        return {"total": len(rows), "channels": channels, "hours_utc": hours}


# ---------------------------------------------------------------------------
# Outreach governance
# ---------------------------------------------------------------------------
def evaluate_outreach(
    contact: Any,
    *,
    is_owner: bool = False,
    last_outbound_ts: Optional[str] = None,
    cadence_days: Optional[float] = None,
    overdue: bool = False,
    open_followups: Optional[List[str]] = None,
    suggested_channel: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Decide whether the agent should (re)initiate contact, and how.

    Returns: should_contact, reason, requires_owner_approval, suggested_channel,
    cooldown_active, talking_points. Policy: never contact a blocked contact;
    respect a cooldown so we don't double-message; only reach out when there's a
    real reason (overdue cadence or an open follow-up); and ANY proactive outreach
    to someone other than the owner requires the owner's approval first.
    """
    now = now or _now()
    followups = [f for f in (open_followups or []) if f]
    allowed = getattr(contact, "interaction_allowed", True)

    result = {
        "should_contact": False,
        "reason": "",
        "requires_owner_approval": (not is_owner),
        "suggested_channel": suggested_channel or "",
        "cooldown_active": False,
        "talking_points": followups[:5],
    }

    if not allowed:
        result["reason"] = "contact is not authorized for outreach (interaction_allowed=false)"
        result["requires_owner_approval"] = True
        return result

    # Cooldown: don't reach out again too soon after our last outbound.
    last_out = _parse(last_outbound_ts)
    if last_out is not None:
        hrs = (now - last_out).total_seconds() / 3600.0
        cooldown_hrs = max(24.0, (float(cadence_days) * 24.0 / 2.0) if cadence_days else 24.0)
        if hrs < cooldown_hrs:
            result["cooldown_active"] = True
            result["reason"] = (f"reached out {round(hrs)}h ago; in cooldown "
                                f"(~{round(cooldown_hrs)}h) — hold off to avoid spamming")
            return result

    reasons = []
    if overdue:
        reasons.append("overdue vs their usual cadence"
                       + (f" (~{round(cadence_days)}d)" if cadence_days else ""))
    if followups:
        reasons.append(f"{len(followups)} open follow-up(s)")

    if not reasons:
        result["reason"] = "no current reason to reach out (not overdue, no open follow-ups)"
        return result

    result["should_contact"] = True
    result["reason"] = "; ".join(reasons)
    return result
