"""GuardAuditStore — durable record of response-guard evaluations.

Two kinds of rows:

* ``guard_events`` — one row per evaluation that produced ANY finding or a
  non-allow decision (not just cross_context). ``would_block`` marks whether
  the findings would have suppressed the reply under enforce, regardless of
  the mode actually running — this is what a false-positive budget is
  measured against while the guard is still in shadow.
* ``guard_eval_days`` — a per-UTC-day counter of TOTAL evaluations (clean or
  not), the denominator that turns finding counts into rates.
* ``guard_communication_policy_evaluations`` — one digest-bound row for every
  evaluation carrying a versioned communication policy, including clean
  allows.  It is separate so those rows do not alter false-positive metrics.

``summary()`` reports the historical authorized/unauthorized split plus, for
24h/7d/14d windows: evaluations, flagged events, per-check counts and the
``would_block_rate`` (would-block events / evaluations). Evaluation counts
are day-granular, so the "24h" window is really "today + yesterday" (UTC) —
close enough for budget tracking, cheap enough to keep forever.

Generic: ``conversation_key`` is an opaque host-supplied id; the store
attaches no meaning.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Dict, List, Optional, Sequence

from colony_sidecar.gate.communication_policy import CommunicationPolicyContextV1


def evidence_min() -> int:
    """Parse the legacy verdict-row threshold configuration.

    Retained for configuration compatibility and diagnostics.  No value makes
    candidate-verdict rows applied-enforcement evidence; ``enforce_evidence``
    remains False until a receipt-backed egress mediator exists.
    """
    try:
        return max(1, int(os.environ.get("COLONY_GUARD_EVIDENCE_MIN", "3")))
    except (TypeError, ValueError):
        return 3


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


class GuardAuditStore:
    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guard_events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               TEXT NOT NULL,
                conversation_key TEXT,
                mode             TEXT,
                decision         TEXT,
                authorized       INTEGER NOT NULL DEFAULT 0,
                checks           TEXT,
                entities         TEXT,
                response_excerpt TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_guard_events_ts ON guard_events(ts);
            CREATE INDEX IF NOT EXISTS idx_guard_events_auth ON guard_events(authorized);
            CREATE TABLE IF NOT EXISTS guard_eval_days (
                day         TEXT PRIMARY KEY,
                evaluations INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS guard_communication_policy_evaluations (
                id                           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                           TEXT NOT NULL,
                conversation_key             TEXT,
                mode                         TEXT NOT NULL,
                decision                     TEXT NOT NULL,
                authorized                   INTEGER NOT NULL DEFAULT 0,
                gateway                      TEXT,
                surface                      TEXT NOT NULL,
                surface_policy_id            TEXT NOT NULL,
                surface_policy_digest        TEXT NOT NULL,
                candidate_digest             TEXT NOT NULL,
                guard_status                 TEXT NOT NULL,
                target_contact_id             TEXT NOT NULL,
                communication_schema          TEXT NOT NULL,
                communication_version         INTEGER NOT NULL,
                communication_context_digest  TEXT NOT NULL,
                route_id                      TEXT NOT NULL,
                grant_id                      TEXT NOT NULL,
                grant_digest                  TEXT NOT NULL,
                communication_policy_id       TEXT NOT NULL,
                communication_policy_digest   TEXT NOT NULL,
                disclosure_class              TEXT NOT NULL,
                grants_execution_authority    INTEGER NOT NULL
                    CHECK(grants_execution_authority = 0),
                grants_control_authority      INTEGER NOT NULL
                    CHECK(grants_control_authority = 0),
                selects_owner_private_context INTEGER NOT NULL
                    CHECK(selects_owner_private_context = 0)
            );
            CREATE INDEX IF NOT EXISTS idx_guard_comm_policy_ts
                ON guard_communication_policy_evaluations(ts);
            CREATE INDEX IF NOT EXISTS idx_guard_comm_policy_context
                ON guard_communication_policy_evaluations(
                    communication_context_digest
                );
            """
        )
        # Migrations (additive): pre-existing DBs lack these columns.
        cols = {r["name"] for r in self._conn.execute(
            "PRAGMA table_info(guard_events)").fetchall()}
        if "would_block" not in cols:
            self._conn.execute(
                "ALTER TABLE guard_events ADD COLUMN would_block INTEGER NOT NULL DEFAULT 0")
        if "gateway" not in cols:
            # Nullable: rows recorded before the gateway was threaded stay
            # NULL and can never satisfy a per-gateway evidence query.
            self._conn.execute(
                "ALTER TABLE guard_events ADD COLUMN gateway TEXT")
        if "surface" not in cols:
            # Legacy rows remain NULL: an unclassified row cannot prove that
            # the current exact-surface policy was enforced.
            self._conn.execute(
                "ALTER TABLE guard_events ADD COLUMN surface TEXT")
        if "policy_id" not in cols:
            self._conn.execute(
                "ALTER TABLE guard_events ADD COLUMN policy_id TEXT")
        if "policy_digest" not in cols:
            # A policy name alone is not an immutable policy identity.  Legacy
            # rows remain NULL and therefore cannot be mistaken for a verdict
            # produced by the current exact policy document.
            self._conn.execute(
                "ALTER TABLE guard_events ADD COLUMN policy_digest TEXT")
        if "candidate_digest" not in cols:
            # Exact identity of the candidate bytes that were evaluated.  This
            # still does not prove a transport applied the verdict.
            self._conn.execute(
                "ALTER TABLE guard_events ADD COLUMN candidate_digest TEXT")
        if "guard_status" not in cols:
            self._conn.execute(
                "ALTER TABLE guard_events ADD COLUMN guard_status TEXT")
        self._conn.commit()

    def count_evaluation(self) -> None:
        """One evaluation happened (finding or not) — bump today's counter."""
        self._conn.execute(
            "INSERT INTO guard_eval_days (day, evaluations) VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET evaluations = evaluations + 1",
            (_today(),),
        )
        self._conn.commit()

    def record(self, *, conversation_key: Optional[str], mode: str, decision: str,
               authorized: bool, checks: Sequence[str], entities: Sequence[str],
               response_text: str = "", would_block: bool = False,
               gateway: Optional[str] = None, surface: Optional[str] = None,
               policy_id: Optional[str] = None,
               policy_digest: Optional[str] = None,
               guard_status: str = "evaluated") -> None:
        candidate_digest = sha256(
            str(response_text or "").encode("utf-8")
        ).hexdigest()
        self._conn.execute(
            "INSERT INTO guard_events (ts, conversation_key, mode, decision, authorized, "
            "checks, entities, response_excerpt, would_block, gateway, surface, "
            "policy_id, policy_digest, candidate_digest, guard_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), conversation_key, mode, decision, 1 if authorized else 0,
             ",".join(checks), ",".join(entities), (response_text or "")[:240],
             1 if would_block else 0, (gateway or "").strip().lower() or None,
             surface, policy_id, policy_digest, candidate_digest, guard_status),
        )
        self._conn.commit()

    def recent(self, *, limit: int = 50, authorized: Optional[bool] = None,
               check: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM guard_events"
        where: list = []
        params: list = []
        if authorized is not None:
            where.append("authorized = ?")
            params.append(1 if authorized else 0)
        if check:
            # checks is a comma-joined list; match the whole token.
            where.append("(',' || checks || ',') LIKE ?")
            params.append(f"%,{check},%")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def record_communication_policy(
        self,
        *,
        conversation_key: Optional[str],
        mode: str,
        decision: str,
        authorized: bool,
        gateway: Optional[str],
        surface: str,
        surface_policy_id: str,
        surface_policy_digest: str,
        response_text: str,
        guard_status: str,
        target_contact_id: str,
        communication_policy: CommunicationPolicyContextV1,
    ) -> None:
        """Record one exact communication-policy/result binding.

        Purpose and disclosure-statement content are deliberately not copied
        into the durable audit DB.  Their complete canonical identity is
        retained by ``communication_context_digest`` while the minimum useful
        route/policy identifiers remain queryable.
        """

        if not isinstance(communication_policy, CommunicationPolicyContextV1):
            raise TypeError(
                "communication_policy must be CommunicationPolicyContextV1"
            )
        communication_policy = CommunicationPolicyContextV1.model_validate(
            communication_policy.canonical_dict()
        )
        if target_contact_id != communication_policy.target_contact_id:
            raise ValueError("communication policy audit target mismatch")
        candidate_digest = sha256(
            str(response_text or "").encode("utf-8")
        ).hexdigest()
        self._conn.execute(
            "INSERT INTO guard_communication_policy_evaluations ("
            "ts,conversation_key,mode,decision,authorized,gateway,surface,"
            "surface_policy_id,surface_policy_digest,candidate_digest,"
            "guard_status,target_contact_id,communication_schema,"
            "communication_version,communication_context_digest,route_id,"
            "grant_id,grant_digest,"
            "communication_policy_id,communication_policy_digest,"
            "disclosure_class,grants_execution_authority,"
            "grants_control_authority,selects_owner_private_context) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _now(),
                conversation_key,
                mode,
                decision,
                1 if authorized else 0,
                (gateway or "").strip().lower() or None,
                surface,
                surface_policy_id,
                surface_policy_digest,
                candidate_digest,
                guard_status,
                target_contact_id,
                communication_policy.schema_name,
                communication_policy.version,
                communication_policy.context_digest,
                communication_policy.route_id,
                communication_policy.grant_id,
                communication_policy.grant_digest,
                communication_policy.policy_id,
                communication_policy.policy_digest,
                communication_policy.disclosure_class,
                0,
                0,
                0,
            ),
        )
        self._conn.commit()

    def recent_communication_policy(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Return newest digest-bound communication-policy evaluations."""

        rows = self._conn.execute(
            "SELECT * FROM guard_communication_policy_evaluations "
            "ORDER BY id DESC LIMIT ?",
            (max(0, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def _window(self, days: int) -> Dict[str, Any]:
        now = datetime.now(tz=timezone.utc)
        ts_cutoff = (now - timedelta(days=days)).isoformat()
        day_cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        evals = self._conn.execute(
            "SELECT COALESCE(SUM(evaluations), 0) n FROM guard_eval_days WHERE day >= ?",
            (day_cutoff,)).fetchone()["n"]
        rows = self._conn.execute(
            "SELECT checks, would_block FROM guard_events WHERE ts >= ?",
            (ts_cutoff,)).fetchall()
        by_check: Dict[str, int] = {}
        would_block = 0
        for r in rows:
            if r["would_block"]:
                would_block += 1
            for c in (r["checks"] or "").split(","):
                c = c.strip()
                if c:
                    by_check[c] = by_check.get(c, 0) + 1
        return {
            "evaluations": int(evals),
            "flagged_events": len(rows),
            "would_block": would_block,
            "would_block_rate": round(would_block / evals, 4) if evals else None,
            "by_check": by_check,
        }

    def enforce_evidence(self, gateway: str, hours: float = 24.0,
                         surface: str = "text_chat") -> bool:
        """Return False until an applied-output receipt store is wired.

        ``guard_events`` proves only that Colony evaluated candidate bytes.  It
        cannot prove that a transport actually withheld those bytes or emitted
        a checked revision.  Counting verdict rows as applied enforcement would
        let a caller unlock higher-trust cognition while ignoring every verdict,
        so this compatibility method deliberately remains fail-closed.
        """

        del gateway, hours, surface
        return False

    def summary(self) -> Dict[str, Any]:
        """All-time authorized/unauthorized split + windowed rates for the
        false-positive budget (see module docstring for granularity)."""
        rows = self._conn.execute(
            "SELECT authorized, COUNT(*) n FROM guard_events GROUP BY authorized"
        ).fetchall()
        by_auth = {("authorized" if r["authorized"] else "unauthorized"): r["n"] for r in rows}
        total = sum(by_auth.values())
        return {"total": total,
                "authorized_transfers": by_auth.get("authorized", 0),
                "unauthorized_flags": by_auth.get("unauthorized", 0),
                "windows": {"24h": self._window(1),
                            "7d": self._window(7),
                            "14d": self._window(14)}}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
