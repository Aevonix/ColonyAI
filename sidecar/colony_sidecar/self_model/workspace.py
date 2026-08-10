"""Cognitive workspace: continuity of thought between interactions (Mind M2).

A bounded store of active concerns, each carrying a salience score that
events raise and time decays. When idle capacity exists the scheduler pops
the most salient concern and runs one bounded thinking job; the outcome
updates memory, resolves the concern, proposes an initiative or experiment,
or concludes "nothing to do" (which decays salience faster so rumination
cannot persist). A nightly sleep window lets the heavy standing agenda run
when the cluster is idle.

This is the difference between "runs phases every N hours" and "has
something on her mind." Generic in ColonyAI; the deployment feeds it events
and supplies the thinker (the LLM reasoning path).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional

from colony_sidecar.scope_bounds import (
    SUBJECT_PERSON_ID_MAX_CHARS,
    VIEWER_SCOPE_MAX_CHARS,
)

logger = logging.getLogger(__name__)

CONCERN_KINDS = ("question", "goal", "thread", "anomaly", "maintenance")
RECENT_RESOLUTIONS_LIMIT = 20
_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_OWNER_RESOLUTION_PROVENANCE = "owner_api"
_LEGACY_RESOLUTION_PROVENANCE = "legacy_unrecorded"


class ConcernResolutionConflict(ValueError):
    """An immutable terminal concern receipt conflicts with a new claim."""


def _resolution_material(
    *,
    concern_id: str,
    outcome: Optional[str],
    note: str,
    cascade: Optional[bool],
    resolved_by: Optional[str],
    resolved_at: str,
    provenance: str,
) -> Dict[str, Any]:
    if cascade is not None and type(cascade) is not bool:
        raise ValueError("concern resolution cascade must be boolean or null")
    note_digest = hashlib.sha256(note.encode("utf-8")).hexdigest()
    payload = {
        "schema": "ColonyConcernResolutionReceiptV1",
        "version": 1,
        "concern_id": concern_id,
        "outcome": outcome,
        "note": note,
        "note_digest": note_digest,
        "cascade": cascade,
        "resolved_by": resolved_by,
        "resolved_at": resolved_at,
        "provenance": provenance,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    record_digest = hashlib.sha256(encoded).hexdigest()
    return {
        **payload,
        "resolution_id": f"concern-resolution:{record_digest}",
        "record_digest": record_digest,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


def _cascade_source_refs(values: Any) -> List[str]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("concern cascade sources must be a list")
    refs: List[str] = []
    for value in values:
        if type(value) is not str or not value or value != value.strip():
            raise ValueError("concern cascade source reference is invalid")
        if len(value) > 512:
            raise ValueError("concern cascade source reference is too long")
        ref = value
        if ref not in refs:
            refs.append(ref)
    if len(refs) > 30:
        raise ValueError("concern cascade supports at most 30 sources")
    return refs


def _cascade_intent_material(
    *,
    resolution_id: str,
    concern_id: str,
    requested: Optional[bool],
    source_refs: List[str],
    captured_at: str,
    capture_provenance: str,
) -> Dict[str, Any]:
    if requested is not None and type(requested) is not bool:
        raise ValueError("cascade intent requested flag is invalid")
    if type(resolution_id) is not str or not resolution_id:
        raise ValueError("cascade intent resolution id is invalid")
    if type(concern_id) is not str or not concern_id:
        raise ValueError("cascade intent concern id is invalid")
    if type(captured_at) is not str or not captured_at:
        raise ValueError("cascade intent capture time is invalid")
    if capture_provenance not in {"resolution_transaction", "migration_snapshot"}:
        raise ValueError("cascade intent capture provenance is invalid")
    payload = {
        "schema": "ColonyConcernCascadeIntentV1",
        "version": 1,
        "resolution_id": resolution_id,
        "concern_id": concern_id,
        "requested": requested,
        "source_refs": _cascade_source_refs(source_refs),
        "captured_at": captured_at,
        "capture_provenance": capture_provenance,
    }
    record_digest = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return {
        **payload,
        "intent_id": f"concern-cascade-intent:{record_digest}",
        "record_digest": record_digest,
    }


def _cascade_material(
    *,
    intent_id: str,
    intent_digest: str,
    resolution_id: str,
    concern_id: str,
    requested: Optional[bool],
    source_refs: List[str],
    source_capture: str,
    status: str,
    settled_sources: List[str],
    failed_sources: List[str],
    unexpected_result_count: int,
    failure_codes: List[str],
    failure_digest: Optional[str],
    recorded_at: str,
) -> Dict[str, Any]:
    if requested is not None and type(requested) is not bool:
        raise ValueError("cascade receipt requested flag is invalid")
    if type(intent_id) is not str or not intent_id:
        raise ValueError("cascade receipt intent id is invalid")
    if not re.fullmatch(r"[a-f0-9]{64}", intent_digest or ""):
        raise ValueError("cascade receipt intent digest is invalid")
    if type(resolution_id) is not str or not resolution_id:
        raise ValueError("cascade receipt resolution id is invalid")
    if type(concern_id) is not str or not concern_id:
        raise ValueError("cascade receipt concern id is invalid")
    if source_capture not in {"resolution_transaction", "migration_snapshot"}:
        raise ValueError("cascade receipt source capture is invalid")
    if type(recorded_at) is not str or not recorded_at:
        raise ValueError("cascade receipt record time is invalid")
    refs = _cascade_source_refs(source_refs)
    settled = _cascade_source_refs(settled_sources)
    failed = _cascade_source_refs(failed_sources)
    if set(settled) & set(failed) or set(settled + failed) - set(refs):
        raise ValueError("cascade receipt source projection is invalid")
    if (
        settled != [ref for ref in refs if ref in set(settled)]
        or failed != [ref for ref in refs if ref in set(failed)]
    ):
        raise ValueError("cascade receipt source projection is not canonical")
    if type(unexpected_result_count) is not int or unexpected_result_count < 0:
        raise ValueError("cascade receipt unexpected result count is invalid")
    allowed_failure_codes = {
        "duplicate_result", "execution_error", "malformed_result",
        "missing_result", "not_settled", "operation_conflict",
        "operation_unverified", "settler_error", "unknown_result",
    }
    if not isinstance(failure_codes, (list, tuple)) or any(
        type(code) is not str or code not in allowed_failure_codes
        for code in failure_codes
    ):
        raise ValueError("cascade receipt failure codes are invalid")
    codes = sorted(set(failure_codes))
    allowed = {
        "succeeded", "failed", "not_requested", "not_applicable",
        "legacy_unknown",
    }
    if status not in allowed:
        raise ValueError("cascade receipt status is invalid")
    if status == "succeeded" and (
        requested is not True or settled != refs or failed
        or unexpected_result_count or codes or failure_digest is not None
    ):
        raise ValueError("successful cascade receipt is inconsistent")
    if status == "failed" and (
        requested is not True or not refs or set(settled + failed) != set(refs)
        or not codes or not failure_digest
    ):
        raise ValueError("failed cascade receipt is inconsistent")
    if status == "not_requested" and requested is not False:
        raise ValueError("not-requested cascade receipt is inconsistent")
    if status == "not_applicable" and (requested is not True or refs):
        raise ValueError("not-applicable cascade receipt is inconsistent")
    if status in {"not_requested", "not_applicable", "legacy_unknown"} and (
        settled or failed or unexpected_result_count or codes
        or failure_digest is not None
    ):
        raise ValueError("non-executed cascade receipt has execution evidence")
    if failure_digest is not None and not re.fullmatch(r"[a-f0-9]{64}", failure_digest):
        raise ValueError("cascade receipt failure digest is invalid")
    payload = {
        "schema": "ColonyConcernCascadeReceiptV1",
        "version": 1,
        "intent_id": intent_id,
        "intent_digest": intent_digest,
        "resolution_id": resolution_id,
        "concern_id": concern_id,
        "requested": requested,
        "source_refs": refs,
        "source_capture": source_capture,
        "status": status,
        "settled_sources": settled,
        "failed_sources": failed,
        "unexpected_result_count": unexpected_result_count,
        "failure_codes": codes,
        "failure_digest": failure_digest,
        "recorded_at": recorded_at,
    }
    record_digest = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return {
        **payload,
        "cascade_receipt_id": f"concern-cascade:{record_digest}",
        "record_digest": record_digest,
    }


def _ordered_event_time(value: Any) -> tuple[str, int]:
    """Return an exact aware timestamp and an integer ordering key."""

    if type(value) is not str or not value or value != value.strip():
        raise ValueError("external event occurred_at must be canonical text")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("external event occurred_at is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("external event occurred_at requires a timezone")
    delta = parsed.astimezone(timezone.utc) - _UTC_EPOCH
    microseconds = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    return value, microseconds


def workspace_mode() -> str:
    """Effective COLONY_WORKSPACE: explicit env > autonomy preset > off.

    An explicitly-set invalid value falls back to "off", exactly as the
    legacy reader did; the preset only fills the unset case."""
    from colony_sidecar.util.autonomy_preset import resolve
    return resolve("COLONY_WORKSPACE", ("off", "shadow", "live"), "off")


def workspace_enabled() -> bool:
    return workspace_mode() in ("shadow", "live")


def _capacity() -> int:
    try:
        return int(os.environ.get("COLONY_WORKSPACE_CAPACITY", "24"))
    except ValueError:
        return 24


def _decay_half_life_hours() -> float:
    try:
        return float(os.environ.get("COLONY_WORKSPACE_HALFLIFE_HOURS", "12"))
    except ValueError:
        return 12.0


def _evict_floor() -> float:
    try:
        return float(os.environ.get("COLONY_WORKSPACE_EVICT_FLOOR", "0.05"))
    except ValueError:
        return 0.05


def _thought_budget() -> int:
    try:
        return int(os.environ.get("COLONY_WORKSPACE_THOUGHT_BUDGET", "8"))
    except ValueError:
        return 8


def _resolved_ttl_hours() -> float:
    """How long a resolved concern suppresses re-raising the same dedup_key.

    A concern is often raised FROM a still-open source by a periodic ingest;
    once someone resolves the concern, recreating it on the very next tick
    makes the resolve cosmetic. Within this window the resolved row answers
    the upsert instead. If the source is genuinely still open after the
    window, the concern legitimately returns."""
    try:
        return float(os.environ.get("COLONY_WORKSPACE_RESOLVED_TTL_HOURS", "24"))
    except ValueError:
        return 24.0


def in_sleep_window(now: Optional[datetime] = None) -> bool:
    """COLONY_SLEEP_WINDOW = 'HH:MM-HH:MM' in the deployment's local time
    (uses the process tz). Empty disables. Wrap-around (22:00-06:00) ok."""
    win = os.environ.get("COLONY_SLEEP_WINDOW", "").strip()
    if not win or "-" not in win:
        return False
    try:
        a, b = win.split("-", 1)
        ah, am = [int(x) for x in a.split(":")]
        bh, bm = [int(x) for x in b.split(":")]
    except ValueError:
        return False
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    start, end = ah * 60 + am, bh * 60 + bm
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end   # wraps midnight


@dataclass
class Concern:
    concern_id: str
    kind: str
    summary: str
    salience: float
    sources: List[str] = field(default_factory=list)
    thoughts_spent: int = 0
    max_thoughts: int = 8
    status: str = "active"           # active | resolved | evicted
    last_note: str = ""
    created_at: float = 0.0
    last_touched: float = 0.0
    last_thought_at: Optional[float] = None
    # memory ids the thinker actually consulted while reasoning about this
    # concern (a measured provenance link, most-recent first) -- NOT a
    # render-time similarity guess. Empty until it has been thought about.
    memory_refs: List[str] = field(default_factory=list)
    dedup_key: str = ""
    subject_person_id: str = ""
    viewer_scope: str = "owner"
    shareability: str = "owner_private"
    last_material_event_id: str = ""
    last_material_event_seq: int = 0
    last_material_event_at: str = ""
    last_material_digest: str = ""
    producer_name: str = "server"
    producer_mode: str = "live"
    producer_revision: str = "workspace-concern-v2"
    promoted_at: str = ""
    promotion_ref: str = ""

    def visible_to(
        self,
        *,
        viewer_person_id: str,
        owner_person_id: str,
        audiences: set[str] | frozenset[str] = frozenset(),
    ) -> bool:
        """Return whether this concern may be projected to one viewer.

        This is deliberately small and deterministic.  Event reduction stores
        the scope; ranking or an LLM can never make a private concern visible.
        """

        viewer = str(viewer_person_id or "").strip()
        owner = str(owner_person_id or "").strip()
        if not viewer:
            return False
        if owner and viewer == owner:
            return True
        if self.shareability == "public":
            return "global" in audiences
        if self.shareability == "shared":
            return "shared" in audiences
        if self.shareability == "subject_private":
            return bool(self.subject_person_id and viewer == self.subject_person_id)
        return False

    def public(self) -> Dict[str, Any]:
        return {
            "concern_id": self.concern_id, "kind": self.kind,
            "summary": self.summary, "salience": round(self.salience, 4),
            "sources": self.sources, "thoughts_spent": self.thoughts_spent,
            "max_thoughts": self.max_thoughts, "status": self.status,
            "last_note": self.last_note, "created_at": self.created_at,
            "last_touched": self.last_touched,
            "last_thought_at": self.last_thought_at,
            "memory_refs": self.memory_refs,
            "subject_person_id": self.subject_person_id,
            "viewer_scope": self.viewer_scope,
            "shareability": self.shareability,
            "last_material_event_id": self.last_material_event_id,
            "last_material_event_seq": self.last_material_event_seq,
            "last_material_event_at": self.last_material_event_at,
            "producer_name": self.producer_name,
            "producer_mode": self.producer_mode,
            "producer_revision": self.producer_revision,
            "promoted_at": self.promoted_at,
            "promotion_ref": self.promotion_ref,
        }


class ConcernStore:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS concerns (
                concern_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL,
                salience REAL NOT NULL,
                sources TEXT,
                dedup_key TEXT,
                thoughts_spent INTEGER DEFAULT 0,
                max_thoughts INTEGER DEFAULT 8,
                status TEXT DEFAULT 'active',
                last_note TEXT,
                created_at REAL NOT NULL,
                last_touched REAL NOT NULL,
                last_thought_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_concern_status_sal
                ON concerns(status, salience);
            CREATE INDEX IF NOT EXISTS idx_concern_dedup ON concerns(dedup_key);
            CREATE TABLE IF NOT EXISTS concern_event_cursors (
                consumer_id TEXT PRIMARY KEY,
                last_seq INTEGER NOT NULL,
                bootstrap_mode TEXT NOT NULL,
                initialized_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS concern_event_receipts (
                consumer_id TEXT NOT NULL,
                event_seq INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                material_digest TEXT NOT NULL,
                disposition TEXT NOT NULL,
                concern_id TEXT,
                reason TEXT,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (consumer_id, event_seq),
                UNIQUE (consumer_id, event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_concern_event_receipts_concern
                ON concern_event_receipts(concern_id, event_seq);
            CREATE TABLE IF NOT EXISTS concern_external_event_watermarks (
                consumer_id TEXT NOT NULL,
                dedup_key TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                occurred_at_us INTEGER NOT NULL,
                operation TEXT NOT NULL,
                material_digest TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_seq INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (consumer_id,dedup_key)
            );
            CREATE INDEX IF NOT EXISTS idx_concern_external_watermarks_consumer
                ON concern_external_event_watermarks(consumer_id,event_seq);
            CREATE TABLE IF NOT EXISTS concern_event_gaps (
                gap_id INTEGER PRIMARY KEY AUTOINCREMENT,
                consumer_id TEXT NOT NULL,
                prior_cursor INTEGER NOT NULL,
                resumed_at INTEGER NOT NULL,
                reason TEXT NOT NULL,
                acknowledged_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS concern_settlements (
                settlement_ref TEXT PRIMARY KEY,
                concern_id TEXT NOT NULL UNIQUE,
                settlement_kind TEXT NOT NULL,
                evidence_refs TEXT NOT NULL,
                reason TEXT NOT NULL,
                settled_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS concern_resolutions (
                resolution_id TEXT PRIMARY KEY,
                concern_id TEXT NOT NULL UNIQUE,
                outcome TEXT,
                note TEXT NOT NULL,
                note_digest TEXT NOT NULL,
                cascade INTEGER,
                resolved_by TEXT,
                resolved_at TEXT NOT NULL,
                provenance TEXT NOT NULL,
                record_digest TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS concern_resolution_cascade_intents (
                intent_id TEXT PRIMARY KEY,
                resolution_id TEXT NOT NULL UNIQUE,
                concern_id TEXT NOT NULL UNIQUE,
                intent_json TEXT NOT NULL,
                record_digest TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS concern_resolution_cascade_receipts (
                cascade_receipt_id TEXT PRIMARY KEY,
                intent_id TEXT NOT NULL UNIQUE,
                resolution_id TEXT NOT NULL UNIQUE,
                concern_id TEXT NOT NULL UNIQUE,
                receipt_json TEXT NOT NULL,
                record_digest TEXT NOT NULL UNIQUE
            );
            """
        )
        # migration: memory_refs was added after first ship; add it to older
        # DBs. ADD COLUMN is backward-compatible (existing rows read NULL).
        try:
            self._conn.execute("ALTER TABLE concerns ADD COLUMN memory_refs TEXT")
        except sqlite3.OperationalError:
            pass  # column already present
        for name, declaration in (
            ("subject_person_id", "TEXT"),
            ("viewer_scope", "TEXT DEFAULT 'owner'"),
            ("shareability", "TEXT DEFAULT 'owner_private'"),
            ("last_material_event_id", "TEXT"),
            ("last_material_event_seq", "INTEGER DEFAULT 0"),
            ("last_material_event_at", "TEXT"),
            ("last_material_digest", "TEXT"),
            ("producer_name", "TEXT DEFAULT 'legacy'"),
            ("producer_mode", "TEXT DEFAULT 'unknown'"),
            ("producer_revision", "TEXT DEFAULT 'unversioned'"),
            ("promoted_at", "TEXT"),
            ("promotion_ref", "TEXT"),
        ):
            try:
                self._conn.execute(
                    f"ALTER TABLE concerns ADD COLUMN {name} {declaration}"
                )
            except sqlite3.OperationalError:
                pass
        try:
            self._conn.execute(
                "ALTER TABLE concern_resolutions ADD COLUMN cascade INTEGER"
            )
        except sqlite3.OperationalError:
            pass
        self._migrate_legacy_resolutions()
        self._migrate_resolution_cascade_records()
        self._conn.commit()

    def _migrate_legacy_resolutions(self) -> None:
        """Freeze truthful receipts for terminal rows predating owner receipts.

        Legacy rows do not contain an exact outcome or resolver.  Persisting
        explicit NULLs keeps those facts unknown instead of letting a later
        caller accidentally claim its request was the original decision.
        """

        rows = self._conn.execute(
            """SELECT concern_id,last_note,last_touched,created_at
               FROM concerns WHERE status='resolved'
               AND NOT EXISTS (
                   SELECT 1 FROM concern_resolutions r
                   WHERE r.concern_id=concerns.concern_id
               )"""
        ).fetchall()
        for row in rows:
            stamp = (
                row["last_touched"]
                if row["last_touched"] is not None
                else row["created_at"]
            )
            try:
                resolved_at = datetime.fromtimestamp(
                    float(stamp), timezone.utc,
                ).isoformat()
            except (OverflowError, TypeError, ValueError):
                resolved_at = _UTC_EPOCH.isoformat()
            material = _resolution_material(
                concern_id=str(row["concern_id"]),
                outcome=None,
                note=str(row["last_note"] or ""),
                cascade=None,
                resolved_by=None,
                resolved_at=resolved_at,
                provenance=_LEGACY_RESOLUTION_PROVENANCE,
            )
            self._conn.execute(
                """INSERT OR IGNORE INTO concern_resolutions
                   (resolution_id,concern_id,outcome,note,note_digest,
                    cascade,resolved_by,resolved_at,provenance,record_digest)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    material["resolution_id"], material["concern_id"],
                    material["outcome"], material["note"],
                    material["note_digest"], material["cascade"],
                    material["resolved_by"], material["resolved_at"],
                    material["provenance"], material["record_digest"],
                ),
            )

    @staticmethod
    def _stored_source_refs(value: Any) -> List[str]:
        try:
            decoded = json.loads(value or "[]")
        except (TypeError, ValueError) as exc:
            raise ValueError("concern sources are invalid") from exc
        return _cascade_source_refs(decoded)

    def _insert_cascade_intent(self, intent: Mapping[str, Any]) -> None:
        self._conn.execute(
            """INSERT INTO concern_resolution_cascade_intents
               (intent_id,resolution_id,concern_id,intent_json,record_digest)
               VALUES (?,?,?,?,?)""",
            (
                intent["intent_id"], intent["resolution_id"],
                intent["concern_id"], _canonical_json(intent),
                intent["record_digest"],
            ),
        )

    def _insert_cascade_receipt(self, receipt: Mapping[str, Any]) -> None:
        self._conn.execute(
            """INSERT INTO concern_resolution_cascade_receipts
               (cascade_receipt_id,intent_id,resolution_id,concern_id,
                receipt_json,record_digest)
               VALUES (?,?,?,?,?,?)""",
            (
                receipt["cascade_receipt_id"], receipt["intent_id"],
                receipt["resolution_id"], receipt["concern_id"],
                _canonical_json(receipt), receipt["record_digest"],
            ),
        )

    def _migrate_resolution_cascade_records(self) -> None:
        """Make prior terminal rows explicit without inventing outcomes.

        An intent created here is marked as a migration-time source snapshot,
        then receives a terminal ``legacy_unknown`` receipt unless the base
        decision proves cascade was not requested.  An existing transactional
        intent without a terminal receipt is deliberately left pending: that
        shape means a current-version process stopped between its decision and
        source-settlement phases.
        """

        rows = self._conn.execute(
            """SELECT r.*,c.sources AS concern_sources
               FROM concern_resolutions r
               LEFT JOIN concerns c ON c.concern_id=r.concern_id
               ORDER BY r.resolved_at,r.resolution_id"""
        ).fetchall()
        for row in rows:
            base = self._resolution_receipt(row)
            intent_row = self._conn.execute(
                """SELECT * FROM concern_resolution_cascade_intents
                   WHERE resolution_id=?""",
                (base["resolution_id"],),
            ).fetchone()
            if intent_row is None:
                refs = self._stored_source_refs(row["concern_sources"])
                intent = _cascade_intent_material(
                    resolution_id=base["resolution_id"],
                    concern_id=base["concern_id"],
                    requested=base["cascade"],
                    source_refs=refs,
                    captured_at=datetime.now(timezone.utc).isoformat(),
                    capture_provenance="migration_snapshot",
                )
                self._insert_cascade_intent(intent)
            else:
                intent = self._cascade_intent_receipt(intent_row, base)

            terminal = self._conn.execute(
                """SELECT * FROM concern_resolution_cascade_receipts
                   WHERE intent_id=?""",
                (intent["intent_id"],),
            ).fetchone()
            if terminal is not None:
                self._cascade_receipt(terminal, base, intent)
                continue
            if intent["capture_provenance"] != "migration_snapshot":
                continue
            status = (
                "not_requested" if intent["requested"] is False
                else "legacy_unknown"
            )
            receipt = _cascade_material(
                intent_id=intent["intent_id"],
                intent_digest=intent["record_digest"],
                resolution_id=base["resolution_id"],
                concern_id=base["concern_id"],
                requested=intent["requested"],
                source_refs=intent["source_refs"],
                source_capture=intent["capture_provenance"],
                status=status,
                settled_sources=[],
                failed_sources=[],
                unexpected_result_count=0,
                failure_codes=[],
                failure_digest=None,
                recorded_at=datetime.now(timezone.utc).isoformat(),
            )
            self._insert_cascade_receipt(receipt)

    def _row(self, r: sqlite3.Row) -> Concern:
        keys = r.keys()
        mrefs = json.loads(r["memory_refs"] or "[]") if "memory_refs" in keys else []
        return Concern(
            concern_id=r["concern_id"], kind=r["kind"], summary=r["summary"],
            salience=r["salience"], sources=json.loads(r["sources"] or "[]"),
            thoughts_spent=r["thoughts_spent"] or 0,
            max_thoughts=r["max_thoughts"] or 8, status=r["status"],
            last_note=r["last_note"] or "", created_at=r["created_at"],
            last_touched=r["last_touched"], last_thought_at=r["last_thought_at"],
            memory_refs=mrefs,
            dedup_key=(r["dedup_key"] or "") if "dedup_key" in keys else "",
            subject_person_id=(r["subject_person_id"] or "")
            if "subject_person_id" in keys else "",
            viewer_scope=(r["viewer_scope"] or "owner")
            if "viewer_scope" in keys else "owner",
            shareability=(r["shareability"] or "owner_private")
            if "shareability" in keys else "owner_private",
            last_material_event_id=(r["last_material_event_id"] or "")
            if "last_material_event_id" in keys else "",
            last_material_event_seq=int(r["last_material_event_seq"] or 0)
            if "last_material_event_seq" in keys else 0,
            last_material_event_at=(r["last_material_event_at"] or "")
            if "last_material_event_at" in keys else "",
            last_material_digest=(r["last_material_digest"] or "")
            if "last_material_digest" in keys else "",
            producer_name=(r["producer_name"] or "legacy")
            if "producer_name" in keys else "legacy",
            producer_mode=(r["producer_mode"] or "unknown")
            if "producer_mode" in keys else "unknown",
            producer_revision=(r["producer_revision"] or "unversioned")
            if "producer_revision" in keys else "unversioned",
            promoted_at=(r["promoted_at"] or "")
            if "promoted_at" in keys else "",
            promotion_ref=(r["promotion_ref"] or "")
            if "promotion_ref" in keys else "")

    def upsert(self, *, kind: str, summary: str, salience: float,
               dedup_key: str, sources: List[str],
               max_thoughts: int,
               producer_name: str = "server",
               producer_mode: str = "live",
               producer_revision: str = "workspace-concern-v2") -> Concern:
        producer = str(producer_name or "unknown").strip()[:64]
        mode = str(producer_mode or "unknown").strip().lower()[:16]
        revision = str(producer_revision or "unversioned").strip()[:128]
        if mode not in {"off", "shadow", "live", "unknown"}:
            mode = "unknown"
        now = time.time()
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM concerns WHERE dedup_key=? AND status='active'",
                (dedup_key,)).fetchone()
            if r is not None:
                merged = list(dict.fromkeys(
                    json.loads(r["sources"] or "[]") + sources))[:30]
                new_sal = min(1.0, max(r["salience"], salience) + 0.05)
                self._conn.execute(
                    "UPDATE concerns SET salience=?, sources=?, "
                    "last_touched=?,producer_name=?,producer_mode=?,"
                    "producer_revision=?,promoted_at=NULL,promotion_ref=NULL "
                    "WHERE concern_id=?",
                    (new_sal, json.dumps(merged), now, producer, mode,
                     revision, r["concern_id"]))
                self._conn.commit()
                return self._row(self._conn.execute(
                    "SELECT * FROM concerns WHERE concern_id=?",
                    (r["concern_id"],)).fetchone())
            # Recently-resolved same key: return the resolved row untouched
            # instead of minting a fresh concern, so a resolve sticks even
            # while the underlying source is still open (see _resolved_ttl).
            r2 = self._conn.execute(
                "SELECT * FROM concerns WHERE dedup_key=? AND "
                "status='resolved' ORDER BY last_touched DESC LIMIT 1",
                (dedup_key,)).fetchone()
            if r2 is not None and (now - (r2["last_touched"] or 0)) < \
                    _resolved_ttl_hours() * 3600.0:
                return self._row(r2)
            cid = f"c-{uuid.uuid4().hex[:12]}"
            self._conn.execute(
                "INSERT INTO concerns (concern_id,kind,summary,salience,"
                "sources,dedup_key,max_thoughts,created_at,last_touched,"
                "producer_name,producer_mode,producer_revision)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, kind, summary, min(1.0, salience), json.dumps(sources),
                 dedup_key, max_thoughts, now, now, producer, mode, revision))
            self._conn.commit()
            return self._row(self._conn.execute(
                "SELECT * FROM concerns WHERE concern_id=?", (cid,)).fetchone())

    def event_cursor(self, consumer_id: str) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_seq FROM concern_event_cursors WHERE consumer_id=?",
                (consumer_id,),
            ).fetchone()
        return int(row["last_seq"]) if row is not None else None

    def initialize_event_cursor(
        self,
        consumer_id: str,
        sequence: int,
        *,
        bootstrap_mode: str,
    ) -> int:
        """Create a durable reducer cursor without overwriting an existing one."""

        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO concern_event_cursors
                   (consumer_id,last_seq,bootstrap_mode,initialized_at,updated_at,last_error)
                   VALUES (?,?,?,?,?,NULL)""",
                (consumer_id, max(0, int(sequence)), bootstrap_mode, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT last_seq FROM concern_event_cursors WHERE consumer_id=?",
                (consumer_id,),
            ).fetchone()
        return int(row["last_seq"])

    def set_event_error(self, consumer_id: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE concern_event_cursors SET last_error=?, updated_at=? "
                "WHERE consumer_id=?",
                (str(message or "")[:200], datetime.now(timezone.utc).isoformat(),
                 consumer_id),
            )
            self._conn.commit()

    def acknowledge_event_gap(
        self,
        consumer_id: str,
        *,
        prior_cursor: int,
        resume_after: int,
        reason: str,
    ) -> int:
        """Audit and advance across journal records lost to retention.

        This is never automatic under the default stop policy.  It preserves
        the gap as evidence instead of pretending the missing records were
        processed.
        """

        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            current = self._conn.execute(
                "SELECT last_seq FROM concern_event_cursors WHERE consumer_id=?",
                (consumer_id,),
            ).fetchone()
            if current is None or int(current["last_seq"]) != int(prior_cursor):
                raise ValueError("event cursor changed before gap acknowledgement")
            if int(resume_after) <= int(prior_cursor):
                raise ValueError("gap resume cursor must advance")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT INTO concern_event_gaps
                       (consumer_id,prior_cursor,resumed_at,reason,acknowledged_at)
                       VALUES (?,?,?,?,?)""",
                    (consumer_id, int(prior_cursor), int(resume_after),
                     str(reason or "journal_retention_gap")[:200], now),
                )
                self._conn.execute(
                    """UPDATE concern_event_cursors
                       SET last_seq=?,updated_at=?,last_error=NULL
                       WHERE consumer_id=?""",
                    (int(resume_after), now, consumer_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return int(resume_after)

    def event_reducer_status(self, consumer_id: str) -> Dict[str, Any]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM concern_event_cursors WHERE consumer_id=?",
                (consumer_id,),
            ).fetchone()
            rows = self._conn.execute(
                """SELECT disposition,COUNT(*) AS n,MAX(event_seq) AS last_seq
                   FROM concern_event_receipts WHERE consumer_id=?
                   GROUP BY disposition""",
                (consumer_id,),
            ).fetchall()
            gaps = self._conn.execute(
                "SELECT COUNT(*) AS n,MAX(resumed_at) AS last_resume "
                "FROM concern_event_gaps WHERE consumer_id=?",
                (consumer_id,),
            ).fetchone()
            watermarks = self._conn.execute(
                "SELECT COUNT(*) AS n,MAX(event_seq) AS last_seq "
                "FROM concern_external_event_watermarks WHERE consumer_id=?",
                (consumer_id,),
            ).fetchone()
            latest_watermark = self._conn.execute(
                "SELECT occurred_at FROM concern_external_event_watermarks "
                "WHERE consumer_id=? ORDER BY occurred_at_us DESC LIMIT 1",
                (consumer_id,),
            ).fetchone()
        return {
            "initialized": cursor is not None,
            "cursor": int(cursor["last_seq"]) if cursor is not None else None,
            "bootstrap_mode": cursor["bootstrap_mode"] if cursor is not None else None,
            "initialized_at": cursor["initialized_at"] if cursor is not None else None,
            "updated_at": cursor["updated_at"] if cursor is not None else None,
            "last_error": cursor["last_error"] if cursor is not None else None,
            "gaps": {
                "count": int(gaps["n"] or 0),
                "last_resume": int(gaps["last_resume"] or 0),
            },
            "event_time_watermarks": {
                "count": int(watermarks["n"] or 0),
                "last_event_seq": int(watermarks["last_seq"] or 0),
                "latest_occurred_at": (
                    latest_watermark["occurred_at"]
                    if latest_watermark is not None else None
                ),
            },
            "dispositions": {
                row["disposition"]: {
                    "count": int(row["n"]),
                    "last_seq": int(row["last_seq"]),
                }
                for row in rows
            },
        }

    def apply_event(
        self,
        *,
        consumer_id: str,
        event_seq: int,
        event_id: str,
        event_type: str,
        material_digest: str,
        projection: Optional[Mapping[str, Any]],
        skip_reason: str = "",
    ) -> Dict[str, Any]:
        """Atomically reduce one journal event and advance its durable cursor.

        A receipt, concern mutation, and cursor update share one transaction.
        Replaying after any crash therefore cannot duplicate a concern or
        inflate salience for the same material state.
        """

        sequence = int(event_seq)
        if sequence < 1:
            raise ValueError("event sequence must be positive")
        consumer = str(consumer_id or "").strip()[:128]
        native_id = str(event_id or "").strip()[:128]
        native_type = str(event_type or "unknown").strip()[:128]
        digest = str(material_digest or "").strip()[:128]
        if not consumer or not native_id or not native_type or not digest:
            raise ValueError("event authority fields are required")

        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            existing = self._conn.execute(
                """SELECT event_seq,event_type,material_digest,disposition,concern_id
                   FROM concern_event_receipts
                   WHERE consumer_id=? AND event_id=?""",
                (consumer, native_id),
            ).fetchone()
            if existing is not None:
                if (int(existing["event_seq"]) != sequence
                        or existing["event_type"] != native_type
                        or existing["material_digest"] != digest):
                    raise ValueError("event id was replayed with conflicting content")
                current = self._conn.execute(
                    "SELECT last_seq FROM concern_event_cursors WHERE consumer_id=?",
                    (consumer,),
                ).fetchone()
                return {
                    "status": "duplicate_event",
                    "disposition": existing["disposition"],
                    "concern_id": existing["concern_id"],
                    "cursor": int(current["last_seq"]) if current is not None else None,
                }

            cursor_row = self._conn.execute(
                "SELECT last_seq FROM concern_event_cursors WHERE consumer_id=?",
                (consumer,),
            ).fetchone()
            if cursor_row is None:
                raise ValueError("event reducer cursor is not initialized")
            cursor = int(cursor_row["last_seq"])
            if sequence != cursor + 1:
                raise ValueError("event sequence is not cursor-contiguous")

            try:
                self._conn.execute("BEGIN IMMEDIATE")
                disposition = "skipped"
                concern_id: Optional[str] = None
                reason = str(skip_reason or "")[:100]

                if projection is not None:
                    external_projection = (
                        projection.get("external_event_projection") is True
                    )
                    operation_raw = projection.get("operation") or "upsert"
                    dedup_raw = projection.get("dedup_key") or ""
                    if external_projection:
                        if (
                            type(operation_raw) is not str
                            or operation_raw not in {"upsert", "resolve"}
                            or type(dedup_raw) is not str
                            or not dedup_raw
                            or dedup_raw != dedup_raw.strip()
                            or len(dedup_raw) > 200
                            or projection.get("producer_name")
                            != "external_event_concerns"
                        ):
                            raise ValueError(
                                "external event projection marker is invalid"
                            )
                    operation = str(operation_raw)
                    dedup_key = str(dedup_raw)[:200]
                    if not dedup_key:
                        raise ValueError("event projection requires a dedup key")
                    row = self._conn.execute(
                        "SELECT * FROM concerns WHERE dedup_key=? AND status='active'",
                        (dedup_key,),
                    ).fetchone()

                    apply_projection = True
                    advance_watermark = False
                    occurred_at = str(
                        projection.get("occurred_at") or ""
                    )[:64]
                    occurred_at_us = 0
                    if external_projection:
                        occurred_at, occurred_at_us = _ordered_event_time(
                            projection.get("occurred_at")
                        )
                        if len(occurred_at) > 64:
                            raise ValueError(
                                "external event occurred_at exceeds 64 characters"
                            )
                        watermark = self._conn.execute(
                            "SELECT * FROM concern_external_event_watermarks "
                            "WHERE consumer_id=? AND dedup_key=?",
                            (consumer, dedup_key),
                        ).fetchone()
                        if watermark is None:
                            advance_watermark = True
                        elif occurred_at_us < int(watermark["occurred_at_us"]):
                            disposition = "external_stale_event"
                            reason = "external_event_time_older_than_watermark"
                            apply_projection = False
                        elif occurred_at_us == int(watermark["occurred_at_us"]):
                            if (
                                operation == watermark["operation"]
                                and digest == watermark["material_digest"]
                            ):
                                disposition = "duplicate_material"
                                reason = "external_event_time_equal_material_replay"
                            else:
                                disposition = "external_event_time_conflict"
                                reason = (
                                    "external_event_time_equal_to_watermark_conflict"
                                )
                            apply_projection = False
                        else:
                            advance_watermark = True
                        if not apply_projection:
                            reference = row
                            if reference is None:
                                reference = self._conn.execute(
                                    "SELECT concern_id FROM concerns "
                                    "WHERE dedup_key=? ORDER BY last_touched DESC "
                                    "LIMIT 1",
                                    (dedup_key,),
                                ).fetchone()
                            concern_id = (
                                reference["concern_id"]
                                if reference is not None else None
                            )

                    if apply_projection and operation == "resolve":
                        if row is None:
                            disposition = "resolve_noop"
                        else:
                            concern_id = row["concern_id"]
                            self._conn.execute(
                                """UPDATE concerns SET status='resolved',salience=0.0,
                                   last_note=?,last_touched=?,last_material_event_id=?,
                                   last_material_event_seq=?,last_material_event_at=?,
                                   last_material_digest=? WHERE concern_id=?""",
                                (str(projection.get("note") or "resolved by source event")[:500],
                                 time.time(), native_id, sequence,
                                 occurred_at, digest, concern_id),
                            )
                            disposition = "resolved"
                    elif apply_projection and operation == "upsert":
                        summary = str(projection.get("summary") or native_type)[:300]
                        kind = str(projection.get("kind") or "thread")
                        kind = kind if kind in CONCERN_KINDS else "thread"
                        salience = max(0.0, min(1.0, float(
                            projection.get("salience", 0.5))))
                        raw_sources = projection.get("sources") or []
                        if external_projection:
                            if not isinstance(raw_sources, (list, tuple)):
                                raise ValueError(
                                    "external concern sources must be a sequence"
                                )
                            for value in raw_sources:
                                if (
                                    type(value) is not str
                                    or not value
                                    or value != value.strip()
                                    or len(value) > 200
                                ):
                                    raise ValueError(
                                        "external concern source is not lossless"
                                    )
                            sources = list(dict.fromkeys(raw_sources))[:30]
                        else:
                            sources = list(dict.fromkeys(
                                str(value)[:200]
                                for value in raw_sources
                                if str(value or "").strip()
                            ))[:30]
                        max_thoughts = max(1, min(100, int(
                            projection.get("max_thoughts") or _thought_budget())))
                        subject = str(
                            projection.get("subject_person_id") or ""
                        )
                        viewer_scope = str(
                            projection.get("viewer_scope") or "owner"
                        )
                        if len(subject) > SUBJECT_PERSON_ID_MAX_CHARS:
                            raise ValueError("concern subject exceeds safe bound")
                        if (
                            not viewer_scope
                            or len(viewer_scope) > VIEWER_SCOPE_MAX_CHARS
                        ):
                            raise ValueError("concern viewer scope exceeds safe bound")
                        shareability = str(
                            projection.get("shareability") or "owner_private")
                        if shareability not in {
                            "owner_private", "subject_private", "shared", "public",
                        }:
                            raise ValueError("invalid concern shareability")
                        producer = str(
                            projection.get("producer_name") or "event_concerns"
                        )[:64]
                        producer_mode = str(
                            projection.get("producer_mode") or "unknown"
                        ).strip().lower()[:16]
                        if producer_mode not in {"off", "shadow", "live", "unknown"}:
                            producer_mode = "unknown"
                        producer_revision = str(
                            projection.get("producer_revision")
                            or "event-concern-reducer-v2"
                        )[:128]

                        if row is not None:
                            concern_id = row["concern_id"]
                            prior_digest = (row["last_material_digest"] or "") \
                                if "last_material_digest" in row.keys() else ""
                            if prior_digest == digest:
                                disposition = "duplicate_material"
                            else:
                                merged = list(dict.fromkeys(
                                    json.loads(row["sources"] or "[]") + sources
                                ))[:30]
                                new_salience = min(
                                    1.0, max(float(row["salience"]), salience) + 0.05,
                                )
                                self._conn.execute(
                                    """UPDATE concerns SET kind=?,summary=?,salience=?,
                                       sources=?,last_touched=?,subject_person_id=?,
                                       viewer_scope=?,shareability=?,
                                       last_material_event_id=?,last_material_event_seq=?,
                                       last_material_event_at=?,last_material_digest=?,
                                       producer_name=?,producer_mode=?,producer_revision=?,
                                       promoted_at=NULL,promotion_ref=NULL
                                       WHERE concern_id=?""",
                                    (kind, summary, new_salience, json.dumps(merged),
                                     time.time(), subject, viewer_scope, shareability,
                                     native_id, sequence, occurred_at, digest,
                                     producer, producer_mode, producer_revision,
                                     concern_id),
                                )
                                disposition = "updated"
                        else:
                            resolved = self._conn.execute(
                                """SELECT * FROM concerns WHERE dedup_key=?
                                   AND status='resolved'
                                   ORDER BY last_touched DESC LIMIT 1""",
                                (dedup_key,),
                            ).fetchone()
                            within_ttl = resolved is not None and (
                                time.time() - float(resolved["last_touched"] or 0)
                            ) < _resolved_ttl_hours() * 3600.0
                            if within_ttl and not external_projection:
                                concern_id = resolved["concern_id"]
                                disposition = "suppressed_resolved"
                            else:
                                concern_id = f"c-{uuid.uuid4().hex[:12]}"
                                touched = time.time()
                                self._conn.execute(
                                    """INSERT INTO concerns
                                       (concern_id,kind,summary,salience,sources,
                                        dedup_key,max_thoughts,created_at,last_touched,
                                        subject_person_id,viewer_scope,shareability,
                                        last_material_event_id,last_material_event_seq,
                                        last_material_event_at,last_material_digest,
                                        producer_name,producer_mode,producer_revision)
                                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    (concern_id, kind, summary, salience,
                                     json.dumps(sources), dedup_key, max_thoughts,
                                     touched, touched, subject, viewer_scope,
                                     shareability, native_id, sequence, occurred_at,
                                     digest, producer, producer_mode,
                                     producer_revision),
                                )
                                disposition = (
                                    "reopened"
                                    if external_projection and resolved is not None
                                    else "created"
                                )
                    elif apply_projection:
                        raise ValueError("unsupported event projection operation")

                    if external_projection and advance_watermark:
                        self._conn.execute(
                            """INSERT INTO concern_external_event_watermarks
                               (consumer_id,dedup_key,occurred_at,occurred_at_us,
                                operation,material_digest,event_id,event_seq,updated_at)
                               VALUES (?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(consumer_id,dedup_key) DO UPDATE SET
                                 occurred_at=excluded.occurred_at,
                                 occurred_at_us=excluded.occurred_at_us,
                                 operation=excluded.operation,
                                 material_digest=excluded.material_digest,
                                 event_id=excluded.event_id,
                                 event_seq=excluded.event_seq,
                                 updated_at=excluded.updated_at""",
                            (
                                consumer, dedup_key, occurred_at, occurred_at_us,
                                operation, digest, native_id, sequence, now_iso,
                            ),
                        )

                self._conn.execute(
                    """INSERT INTO concern_event_receipts
                       (consumer_id,event_seq,event_id,event_type,material_digest,
                        disposition,concern_id,reason,processed_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (consumer, sequence, native_id, native_type, digest,
                     disposition, concern_id, reason, now_iso),
                )
                self._conn.execute(
                    """UPDATE concern_event_cursors
                       SET last_seq=?,updated_at=?,last_error=NULL
                       WHERE consumer_id=?""",
                    (sequence, now_iso, consumer),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        return {
            "status": "processed",
            "disposition": disposition,
            "concern_id": concern_id,
            "cursor": sequence,
        }

    def promote_concern(
        self,
        concern_id: str,
        *,
        expected_material_digest: str,
        promotion_ref: str,
        now: Optional[datetime] = None,
    ) -> Concern:
        """Explicitly promote one immutable shadow material version to live.

        The source mode remains visible; promotion is a separate attestation.
        Exact replay is idempotent and a changed digest/ref is rejected.
        """

        reference = str(promotion_ref or "").strip()[:256]
        expected = str(expected_material_digest or "").strip()[:128]
        if not reference or not expected:
            raise ValueError("promotion reference and material digest are required")
        observed = now or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        promoted = observed.astimezone(timezone.utc).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM concerns WHERE concern_id=?", (concern_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown concern")
            material = str(row["last_material_digest"] or "")
            if material != expected:
                raise ValueError("concern material changed before promotion")
            prior_ref = str(row["promotion_ref"] or "")
            if prior_ref:
                if prior_ref != reference:
                    raise ValueError("concern already has a different promotion")
                return self._row(row)
            updated = self._conn.execute(
                "UPDATE concerns SET promoted_at=?,promotion_ref=? "
                "WHERE concern_id=? AND last_material_digest=? "
                "AND (promotion_ref IS NULL OR promotion_ref='')",
                (promoted, reference, concern_id, expected),
            )
            self._conn.commit()
            current = self._conn.execute(
                "SELECT * FROM concerns WHERE concern_id=?", (concern_id,),
            ).fetchone()
            if (
                updated.rowcount != 1
                or current is None
                or str(current["last_material_digest"] or "") != expected
                or str(current["promotion_ref"] or "") != reference
            ):
                raise ValueError("concern changed during promotion")
        return self._row(current)

    def active(self, limit: int = 100) -> List[Concern]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM concerns WHERE status='active'"
                " ORDER BY salience DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _producer_mode_exclusions(
        values: Iterable[tuple[str, str]],
    ) -> tuple[tuple[str, str], ...]:
        return tuple(dict.fromkeys(
            (
                str(producer or "legacy").strip()[:64],
                str(mode or "unknown").strip().lower()[:16],
            )
            for producer, mode in values
            if str(producer or "").strip() and str(mode or "").strip()
        ))

    def active_for_maintenance(
        self,
        *,
        excluded_producer_modes: Iterable[tuple[str, str]] = (),
        limit: int = 500,
    ) -> List[Concern]:
        exclusions = self._producer_mode_exclusions(excluded_producer_modes)
        where = "status='active'"
        params: List[Any] = []
        if exclusions:
            terms = " OR ".join(
                "(COALESCE(producer_name,'legacy')=? AND "
                "COALESCE(producer_mode,'unknown')=?)"
                for _ in exclusions
            )
            where += f" AND NOT ({terms})"
            for producer, mode in exclusions:
                params.extend((producer, mode))
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM concerns WHERE {where} "
                "ORDER BY salience DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row(row) for row in rows]

    def active_without_producer(
        self, producer_name: str, *, limit: int = 100,
    ) -> List[Concern]:
        """Return active rows while excluding one producer at query time.

        This avoids a fixed-window starvation bug without loading an
        unbounded number of held rows into memory.  The helper is generic;
        schedulers retain ownership of which producer, if any, is ineligible.
        """

        return self.active_without_producers((producer_name,), limit=limit)

    def active_without_producers(
        self, producer_names: Iterable[str], *, limit: int = 100,
    ) -> List[Concern]:
        """Return active rows excluding a bounded set of held producers."""

        values = (
            (producer_names,)
            if isinstance(producer_names, str)
            else producer_names
        )
        producers = tuple(dict.fromkeys(
            str(value or "").strip()[:64]
            for value in values
            if str(value or "").strip()
        ))
        if not producers:
            return self.active(limit=limit)
        bounded = max(1, min(int(limit), 500))
        marks = ",".join("?" for _ in producers)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM concerns WHERE status='active' "
                f"AND (producer_name IS NULL OR producer_name NOT IN ({marks})) "
                "ORDER BY salience DESC LIMIT ?",
                (*producers, bounded),
            ).fetchall()
        return [self._row(row) for row in rows]

    def active_for_viewer(
        self,
        *,
        viewer_person_id: str,
        owner_person_id: str,
        audiences: set[str] | frozenset[str] = frozenset(),
        limit: int = 100,
    ) -> List[Concern]:
        # Fetch beyond the requested cap so private rows do not starve visible
        # rows below them.  Workspace capacity is bounded, so this remains
        # cheap and avoids encoding policy into SQL fragments.
        rows = self.active(limit=max(_capacity(), limit, 200))
        return [
            concern for concern in rows
            if concern.visible_to(
                viewer_person_id=viewer_person_id,
                owner_person_id=owner_person_id,
                audiences=audiences,
            )
        ][:limit]

    def get(self, concern_id: str) -> Optional[Concern]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM concerns WHERE concern_id=?",
                (concern_id,)).fetchone()
        return self._row(r) if r else None

    def set_salience(self, concern_id: str, salience: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE concerns SET salience=?, last_touched=? "
                "WHERE concern_id=?",
                (max(0.0, min(1.0, salience)), time.time(), concern_id))
            self._conn.commit()

    def record_thought(self, concern_id: str, note: str, *,
                       resolved: bool, salience: float) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE concerns SET thoughts_spent=thoughts_spent+1,"
                " last_note=?, last_thought_at=?, last_touched=?,"
                " salience=?, status=? WHERE concern_id=?",
                (note[:500], now, now, max(0.0, min(1.0, salience)),
                 "resolved" if resolved else "active", concern_id))
            self._conn.commit()

    @staticmethod
    def _resolution_receipt(row: sqlite3.Row) -> Dict[str, Any]:
        stored_cascade = row["cascade"]
        if stored_cascade is None:
            cascade = None
        elif type(stored_cascade) is int and stored_cascade in (0, 1):
            cascade = bool(stored_cascade)
        else:
            raise ValueError("concern resolution receipt cascade is invalid")
        receipt = {
            "schema": "ColonyConcernResolutionReceiptV1",
            "version": 1,
            "resolution_id": row["resolution_id"],
            "record_digest": row["record_digest"],
            "concern_id": row["concern_id"],
            "outcome": row["outcome"],
            "note": row["note"],
            "note_digest": row["note_digest"],
            "cascade": cascade,
            "resolved_by": row["resolved_by"],
            "resolved_at": row["resolved_at"],
            "provenance": row["provenance"],
        }
        expected = _resolution_material(
            concern_id=receipt["concern_id"],
            outcome=receipt["outcome"],
            note=receipt["note"],
            cascade=receipt["cascade"],
            resolved_by=receipt["resolved_by"],
            resolved_at=receipt["resolved_at"],
            provenance=receipt["provenance"],
        )
        if any(
            receipt[name] != expected[name]
            for name in ("note_digest", "record_digest", "resolution_id")
        ):
            raise ValueError("concern resolution receipt integrity check failed")
        return receipt

    @staticmethod
    def _cascade_intent_receipt(
        row: sqlite3.Row,
        base: Mapping[str, Any],
    ) -> Dict[str, Any]:
        try:
            payload = json.loads(row["intent_json"])
            if type(payload) is not dict:
                raise ValueError
            expected = _cascade_intent_material(
                resolution_id=payload["resolution_id"],
                concern_id=payload["concern_id"],
                requested=payload["requested"],
                source_refs=payload["source_refs"],
                captured_at=payload["captured_at"],
                capture_provenance=payload["capture_provenance"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("concern cascade intent integrity check failed") from exc
        if (
            set(payload) != set(expected)
            or payload != expected
            or row["intent_id"] != expected["intent_id"]
            or row["resolution_id"] != expected["resolution_id"]
            or row["concern_id"] != expected["concern_id"]
            or row["record_digest"] != expected["record_digest"]
            or payload["resolution_id"] != base["resolution_id"]
            or payload["concern_id"] != base["concern_id"]
            or payload["requested"] is not base["cascade"]
        ):
            raise ValueError("concern cascade intent integrity check failed")
        return payload

    @staticmethod
    def _cascade_receipt(
        row: sqlite3.Row,
        base: Mapping[str, Any],
        intent: Mapping[str, Any],
    ) -> Dict[str, Any]:
        try:
            payload = json.loads(row["receipt_json"])
            if type(payload) is not dict:
                raise ValueError
            expected = _cascade_material(
                intent_id=payload["intent_id"],
                intent_digest=payload["intent_digest"],
                resolution_id=payload["resolution_id"],
                concern_id=payload["concern_id"],
                requested=payload["requested"],
                source_refs=payload["source_refs"],
                source_capture=payload["source_capture"],
                status=payload["status"],
                settled_sources=payload["settled_sources"],
                failed_sources=payload["failed_sources"],
                unexpected_result_count=payload["unexpected_result_count"],
                failure_codes=payload["failure_codes"],
                failure_digest=payload["failure_digest"],
                recorded_at=payload["recorded_at"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("concern cascade receipt integrity check failed") from exc
        linkage = {
            "intent_id": intent["intent_id"],
            "intent_digest": intent["record_digest"],
            "resolution_id": base["resolution_id"],
            "concern_id": base["concern_id"],
            "requested": intent["requested"],
            "source_refs": intent["source_refs"],
            "source_capture": intent["capture_provenance"],
        }
        if (
            set(payload) != set(expected)
            or payload != expected
            or any(payload[name] != value for name, value in linkage.items())
            or row["cascade_receipt_id"] != expected["cascade_receipt_id"]
            or row["intent_id"] != expected["intent_id"]
            or row["resolution_id"] != expected["resolution_id"]
            or row["concern_id"] != expected["concern_id"]
            or row["record_digest"] != expected["record_digest"]
        ):
            raise ValueError("concern cascade receipt integrity check failed")
        return payload

    def _cascade_records(
        self,
        base: Mapping[str, Any],
    ) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        intent_row = self._conn.execute(
            """SELECT * FROM concern_resolution_cascade_intents
               WHERE resolution_id=?""",
            (base["resolution_id"],),
        ).fetchone()
        if intent_row is None:
            raise ValueError("concern cascade intent is missing")
        intent = self._cascade_intent_receipt(intent_row, base)
        terminal_row = self._conn.execute(
            """SELECT * FROM concern_resolution_cascade_receipts
               WHERE intent_id=?""",
            (intent["intent_id"],),
        ).fetchone()
        terminal = (
            self._cascade_receipt(terminal_row, base, intent)
            if terminal_row is not None else None
        )
        return intent, terminal

    @staticmethod
    def _attach_cascade_evidence(
        base: Mapping[str, Any],
        intent: Mapping[str, Any],
        terminal: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        receipt = dict(base)
        if terminal is not None:
            receipt["cascade_evidence"] = dict(terminal)
            return receipt
        if not (
            base["provenance"] == _OWNER_RESOLUTION_PROVENANCE
            and base["cascade"] is True
            and intent["requested"] is True
            and intent["source_refs"]
            and intent["capture_provenance"] == "resolution_transaction"
        ):
            raise ValueError("concern cascade terminal receipt is missing")
        receipt["cascade_evidence"] = {
            "schema": "ColonyConcernCascadeStateV1",
            "version": 1,
            "intent_id": intent["intent_id"],
            "intent_digest": intent["record_digest"],
            "resolution_id": base["resolution_id"],
            "concern_id": base["concern_id"],
            "requested": True,
            "source_refs": list(intent["source_refs"]),
            "source_capture": intent["capture_provenance"],
            "status": "pending",
            "started_at": intent["captured_at"],
        }
        return receipt

    def get_resolution(self, concern_id: str) -> Optional[Dict[str, Any]]:
        """Return the immutable decision and its validated cascade projection."""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM concern_resolutions WHERE concern_id=?",
                (concern_id,),
            ).fetchone()
            if row is None:
                return None
            base = self._resolution_receipt(row)
            intent, terminal = self._cascade_records(base)
            return self._attach_cascade_evidence(base, intent, terminal)

    def recent_resolutions(
        self, limit: int = RECENT_RESOLUTIONS_LIMIT,
    ) -> List[Dict[str, Any]]:
        """Return a bounded, integrity-checked newest-first receipt list."""

        bounded = max(0, min(int(limit), RECENT_RESOLUTIONS_LIMIT))
        if bounded == 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM concern_resolutions
                   ORDER BY resolved_at DESC,resolution_id DESC LIMIT ?""",
                (bounded,),
            ).fetchall()
            receipts = []
            for row in rows:
                base = self._resolution_receipt(row)
                intent, terminal = self._cascade_records(base)
                receipts.append(
                    self._attach_cascade_evidence(base, intent, terminal)
                )
            return receipts

    def resolve_with_owner_record(
        self,
        concern_id: str,
        *,
        outcome: str,
        note: str,
        cascade: bool,
        resolved_by: str,
    ) -> tuple[Dict[str, Any], bool]:
        """Resolve once and bind the exact owner decision transactionally.

        The returned boolean is true only for the first write.  A byte-exact
        semantic replay returns the original receipt; a different outcome,
        note, cascade choice, or resolver conflicts.  A legacy receipt also
        conflicts because its missing facts cannot truthfully be filled in
        after the event.
        """

        if type(cascade) is not bool:
            raise ValueError("concern resolution cascade must be boolean")
        expected = {
            "concern_id": str(concern_id),
            "outcome": str(outcome),
            "note": str(note),
            "cascade": cascade,
            "resolved_by": str(resolved_by),
            "provenance": _OWNER_RESOLUTION_PROVENANCE,
        }
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT * FROM concern_resolutions WHERE concern_id=?",
                    (concern_id,),
                ).fetchone()
                if existing is not None:
                    base = self._resolution_receipt(existing)
                    intent, terminal = self._cascade_records(base)
                    receipt = self._attach_cascade_evidence(base, intent, terminal)
                    if any(base[name] != value for name, value in expected.items()):
                        raise ConcernResolutionConflict(
                            "immutable concern resolution replay mismatch"
                        )
                    self._conn.commit()
                    return receipt, False

                concern = self._conn.execute(
                    "SELECT status,sources FROM concerns WHERE concern_id=?",
                    (concern_id,),
                ).fetchone()
                if concern is None:
                    raise ValueError("unknown concern")
                if concern["status"] != "active":
                    raise ConcernResolutionConflict(
                        "concern is terminal without an exact owner resolution receipt"
                    )

                source_refs = self._stored_source_refs(concern["sources"])
                resolved_at = datetime.now(timezone.utc).isoformat()
                material = _resolution_material(
                    concern_id=expected["concern_id"],
                    outcome=expected["outcome"],
                    note=expected["note"],
                    cascade=expected["cascade"],
                    resolved_by=expected["resolved_by"],
                    resolved_at=resolved_at,
                    provenance=expected["provenance"],
                )
                self._conn.execute(
                    """INSERT INTO concern_resolutions
                       (resolution_id,concern_id,outcome,note,note_digest,
                        cascade,resolved_by,resolved_at,provenance,record_digest)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        material["resolution_id"], material["concern_id"],
                        material["outcome"], material["note"],
                        material["note_digest"], int(material["cascade"]),
                        material["resolved_by"], material["resolved_at"],
                        material["provenance"], material["record_digest"],
                    ),
                )
                intent = _cascade_intent_material(
                    resolution_id=material["resolution_id"],
                    concern_id=material["concern_id"],
                    requested=material["cascade"],
                    source_refs=source_refs,
                    captured_at=resolved_at,
                    capture_provenance="resolution_transaction",
                )
                self._insert_cascade_intent(intent)
                terminal: Optional[Dict[str, Any]] = None
                terminal_status = (
                    "not_requested" if material["cascade"] is False
                    else "not_applicable" if not source_refs
                    else None
                )
                if terminal_status is not None:
                    terminal = _cascade_material(
                        intent_id=intent["intent_id"],
                        intent_digest=intent["record_digest"],
                        resolution_id=material["resolution_id"],
                        concern_id=material["concern_id"],
                        requested=material["cascade"],
                        source_refs=source_refs,
                        source_capture=intent["capture_provenance"],
                        status=terminal_status,
                        settled_sources=[],
                        failed_sources=[],
                        unexpected_result_count=0,
                        failure_codes=[],
                        failure_digest=None,
                        recorded_at=resolved_at,
                    )
                    self._insert_cascade_receipt(terminal)
                now = time.time()
                changed = self._conn.execute(
                    """UPDATE concerns SET thoughts_spent=thoughts_spent+1,
                       last_note=?,last_thought_at=?,last_touched=?,salience=0.0,
                       status='resolved' WHERE concern_id=? AND status='active'""",
                    (expected["note"][:500], now, now, concern_id),
                ).rowcount
                if changed != 1:
                    raise ConcernResolutionConflict(
                        "concern changed before its owner resolution was recorded"
                    )
                self._conn.commit()
                return self._attach_cascade_evidence(
                    material, intent, terminal,
                ), True
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _cascade_execution_summary(
        intent: Mapping[str, Any],
        results: Any,
        error: Optional[BaseException],
    ) -> Dict[str, Any]:
        refs = list(intent["source_refs"])
        accepted: Dict[str, Mapping[str, Any]] = {}
        diagnostics: List[Dict[str, Any]] = []
        codes: set[str] = set()
        unexpected = 0
        if not isinstance(results, (list, tuple)):
            results = []
            unexpected += 1
            codes.add("malformed_result")
        elif len(results) > 60:
            unexpected += len(results) - 60
            codes.add("malformed_result")
            results = results[:60]
        for item in results:
            if not isinstance(item, Mapping):
                unexpected += 1
                codes.add("malformed_result")
                continue
            source = item.get("source")
            if type(source) is not str:
                unexpected += 1
                codes.add("malformed_result")
                continue
            try:
                normalized = _cascade_source_refs([source])[0]
            except (IndexError, ValueError):
                unexpected += 1
                codes.add("malformed_result")
                continue
            if normalized not in refs:
                unexpected += 1
                codes.add("unknown_result")
                continue
            if normalized in accepted:
                unexpected += 1
                codes.add("duplicate_result")
                continue
            accepted[normalized] = item

        settled: List[str] = []
        failed: List[str] = []
        for ref in refs:
            item = accepted.get(ref)
            if item is None:
                failed.append(ref)
                codes.add("missing_result")
                diagnostics.append({"source": ref, "code": "missing_result"})
                continue
            raw_error = item.get("error")
            if raw_error:
                failed.append(ref)
                error_code = (
                    raw_error
                    if raw_error in {
                        "operation_conflict", "operation_unverified",
                        "settler_error",
                    }
                    else "settler_error"
                )
                codes.add(error_code)
                supplied_digest = item.get("error_digest")
                error_digest = (
                    supplied_digest
                    if type(supplied_digest) is str
                    and re.fullmatch(r"[a-f0-9]{64}", supplied_digest)
                    else hashlib.sha256(
                        str(raw_error).encode("utf-8", errors="replace")
                    ).hexdigest()
                )
                diagnostics.append({
                    "source": ref,
                    "code": error_code,
                    "error_digest": error_digest,
                })
            elif item.get("settled") is not True:
                failed.append(ref)
                codes.add("not_settled")
                diagnostics.append({"source": ref, "code": "not_settled"})
            else:
                settled.append(ref)
                diagnostics.append({"source": ref, "code": "settled"})

        if error is not None:
            codes.add("execution_error")
            diagnostics.append({
                "code": "execution_error",
                "error_digest": hashlib.sha256(
                    str(error).encode("utf-8", errors="replace")
                ).hexdigest(),
            })
        failed_status = bool(failed or unexpected or error is not None)
        failure_digest = None
        if failed_status:
            failure_digest = hashlib.sha256(_canonical_json({
                "intent_id": intent["intent_id"],
                "failure_codes": sorted(codes),
                "unexpected_result_count": unexpected,
                "diagnostics": diagnostics,
            }).encode("utf-8")).hexdigest()
        return {
            "status": "failed" if failed_status else "succeeded",
            "settled_sources": settled,
            "failed_sources": failed,
            "unexpected_result_count": unexpected,
            "failure_codes": sorted(codes),
            "failure_digest": failure_digest,
        }

    def finalize_owner_cascade(
        self,
        concern_id: str,
        *,
        results: Any,
        error: Optional[BaseException] = None,
    ) -> Dict[str, Any]:
        """Append one terminal outcome for a transaction-bound cascade intent."""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM concern_resolutions WHERE concern_id=?",
                    (concern_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("unknown concern resolution")
                base = self._resolution_receipt(row)
                intent, existing = self._cascade_records(base)
                if not (
                    base["provenance"] == _OWNER_RESOLUTION_PROVENANCE
                    and intent["capture_provenance"] == "resolution_transaction"
                    and intent["requested"] is True
                    and intent["source_refs"]
                ):
                    raise ConcernResolutionConflict(
                        "concern does not have a pending cascade intent"
                    )
                summary = self._cascade_execution_summary(intent, results, error)
                recorded_at = (
                    existing["recorded_at"] if existing is not None
                    else datetime.now(timezone.utc).isoformat()
                )
                terminal = _cascade_material(
                    intent_id=intent["intent_id"],
                    intent_digest=intent["record_digest"],
                    resolution_id=base["resolution_id"],
                    concern_id=base["concern_id"],
                    requested=intent["requested"],
                    source_refs=intent["source_refs"],
                    source_capture=intent["capture_provenance"],
                    recorded_at=recorded_at,
                    **summary,
                )
                if existing is not None:
                    if existing != terminal:
                        raise ConcernResolutionConflict(
                            "immutable cascade outcome replay mismatch"
                        )
                else:
                    self._insert_cascade_receipt(terminal)
                self._conn.commit()
                return self._attach_cascade_evidence(base, intent, terminal)
            except Exception:
                self._conn.rollback()
                raise

    def settle_with_evidence(
        self,
        concern_id: str,
        *,
        settlement_kind: str,
        settlement_ref: str,
        evidence_refs: List[str],
        reason: str,
    ) -> Dict[str, Any]:
        """Atomically settle one concern from server-validated evidence.

        This does not mutate the upstream commitment/service/relationship
        source.  The P3 cognition spine is the intended caller; model output
        alone has no access to this method.
        """

        kind = str(settlement_kind or "").strip()
        if kind not in {"no_action", "project_outcome"}:
            raise ValueError("unsupported concern settlement kind")
        ref = str(settlement_ref or "").strip()[:256]
        refs = list(dict.fromkeys(
            str(value or "").strip()[:256]
            for value in evidence_refs or []
            if str(value or "").strip()
        ))[:60]
        if not ref or not refs or not str(reason or "").strip():
            raise ValueError("settlement reference, evidence, and reason are required")
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM concern_settlements WHERE concern_id=?",
                (concern_id,),
            ).fetchone()
            if existing is not None:
                row = dict(existing)
                if (
                    row["settlement_ref"] != ref
                    or row["settlement_kind"] != kind
                    or json.loads(row["evidence_refs"] or "[]") != refs
                    or row["reason"] != str(reason)[:800]
                ):
                    raise ValueError("immutable concern settlement replay mismatch")
                row["evidence_refs"] = refs
                return row
            concern = self._conn.execute(
                "SELECT status FROM concerns WHERE concern_id=?",
                (concern_id,),
            ).fetchone()
            if concern is None:
                raise ValueError("unknown concern")
            if concern["status"] != "active":
                raise ValueError("concern is already terminal without this settlement")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT INTO concern_settlements
                       (settlement_ref,concern_id,settlement_kind,evidence_refs,
                        reason,settled_at) VALUES (?,?,?,?,?,?)""",
                    (ref, concern_id, kind, json.dumps(refs),
                     str(reason)[:800], now_iso),
                )
                self._conn.execute(
                    """UPDATE concerns SET status='resolved',salience=0.0,
                       last_note=?,last_touched=? WHERE concern_id=?""",
                    (str(reason)[:500], time.time(), concern_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_settlement(concern_id) or {}

    def get_settlement(self, concern_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM concern_settlements WHERE concern_id=?",
                (concern_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["evidence_refs"] = json.loads(result["evidence_refs"] or "[]")
        return result

    def set_memory_refs(self, concern_id: str, ids: List[str], cap: int = 12) -> None:
        """Record which memories the thinker consulted about this concern, most
        recent first, deduped and capped. A real, measured provenance link."""
        clean = [str(i) for i in (ids or []) if i]
        if not clean:
            return
        with self._lock:
            r = self._conn.execute(
                "SELECT memory_refs FROM concerns WHERE concern_id=?",
                (concern_id,)).fetchone()
            if r is None:
                return
            prior = []
            try:
                prior = json.loads((r["memory_refs"] if "memory_refs" in r.keys() else None) or "[]")
            except Exception:
                prior = []
            merged = list(dict.fromkeys(clean + prior))[:cap]
            self._conn.execute(
                "UPDATE concerns SET memory_refs=? WHERE concern_id=?",
                (json.dumps(merged), concern_id))
            self._conn.commit()

    def resolve_by_dedup(self, dedup_key: str, note: str) -> int:
        """Resolve every ACTIVE concern carrying this dedup_key (reverse
        cascade: when the source itself gets settled directly, the concern
        raised from it must leave her mind too). Returns count resolved."""
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT concern_id FROM concerns WHERE dedup_key=? "
                "AND status='active'", (dedup_key,)).fetchall()
            for r in rows:
                self._conn.execute(
                    "UPDATE concerns SET status='resolved', salience=0.0,"
                    " last_note=?, last_touched=? WHERE concern_id=?",
                    (note[:500], now, r["concern_id"]))
            self._conn.commit()
            return len(rows)

    def evict_below(
        self,
        floor: float,
        keep: int,
        *,
        excluded_producer_modes: Iterable[tuple[str, str]] = (),
    ) -> int:
        """Evict active concerns under the floor, and any beyond the capacity
        cap (lowest salience first). Returns count evicted."""
        with self._lock:
            exclusions = self._producer_mode_exclusions(
                excluded_producer_modes,
            )
            where = "status='active'"
            params: List[Any] = []
            if exclusions:
                terms = " OR ".join(
                    "(COALESCE(producer_name,'legacy')=? AND "
                    "COALESCE(producer_mode,'unknown')=?)"
                    for _ in exclusions
                )
                where += f" AND NOT ({terms})"
                for producer, mode in exclusions:
                    params.extend((producer, mode))
            rows = self._conn.execute(
                "SELECT concern_id, salience FROM concerns "
                f"WHERE {where} ORDER BY salience DESC",
                params,
            ).fetchall()
            to_evict = [r["concern_id"] for r in rows[keep:]]
            to_evict += [r["concern_id"] for r in rows[:keep]
                         if r["salience"] < floor]
            for cid in set(to_evict):
                self._conn.execute(
                    "UPDATE concerns SET status='evicted' WHERE concern_id=?",
                    (cid,))
            self._conn.commit()
            return len(set(to_evict))


class WorkspaceEngine:
    """Salience dynamics + the thinking scheduler.

    The thinker is an injected async callable `thinker(concern) -> dict` with
    keys: progress(bool), resolve(bool), note(str), action(optional dict
    {"kind": "initiative"|"experiment"|"memory"|"none", ...}). Kept out of
    this module so ColonyAI stays model-agnostic and tests inject a fake.
    """

    def __init__(self, store: ConcernStore, *,
                 thinker: Optional[Callable[[Concern], Awaitable[Dict[str, Any]]]] = None,
                 journal: Any = None,
                 on_action: Optional[Callable[[Concern, Dict[str, Any]], Awaitable[None]]] = None,
                 event_reducer: Any = None,
                 external_event_reducer: Any = None,
                 turn_event_reducer: Any = None,
                 cognition_spine: Any = None) -> None:
        self.store = store
        self._thinker = thinker
        self._journal = journal
        self._on_action = on_action
        self.event_reducer = event_reducer
        self.external_event_reducer = external_event_reducer
        self.turn_event_reducer = turn_event_reducer
        self.cognition_spine = cognition_spine

    # -- salience ---------------------------------------------------------
    def bump(self, *, kind: str, summary: str, dedup_key: str,
             salience: float = 0.5, sources: Optional[List[str]] = None,
             max_thoughts: Optional[int] = None,
             producer_name: str = "workspace",
             producer_mode: Optional[str] = None,
             producer_revision: str = "workspace-engine-v2") -> Concern:
        kind = kind if kind in CONCERN_KINDS else "thread"
        return self.store.upsert(
            kind=kind, summary=summary[:300], salience=salience,
            dedup_key=dedup_key[:200], sources=sources or [],
            max_thoughts=max_thoughts or _thought_budget(),
            producer_name=producer_name,
            producer_mode=producer_mode or workspace_mode(),
            producer_revision=producer_revision)

    def decay(self) -> int:
        """Exponential time-decay of every active concern; evict the floor
        and anything over capacity. Returns the number evicted."""
        hl = _decay_half_life_hours() * 3600.0
        now = time.time()
        shadow_turns = (("turn_concerns", "shadow"),)
        for c in self.store.active_for_maintenance(
            excluded_producer_modes=shadow_turns,
            limit=500,
        ):
            dt = max(0.0, now - c.last_touched)
            factor = 0.5 ** (dt / hl) if hl > 0 else 1.0
            self.store.set_salience(c.concern_id, c.salience * factor)
        return self.store.evict_below(
            _evict_floor(), _capacity(),
            excluded_producer_modes=shadow_turns,
        )

    def top(self) -> Optional[Concern]:
        active = self.store.active(limit=1)
        return active[0] if active else None

    # -- thinking ---------------------------------------------------------
    async def think_once(self) -> Optional[Dict[str, Any]]:
        """Pop the most salient thinkable concern and run one thought.
        Returns the outcome dict, or None if nothing to think about."""
        # Once P3 is live, every autonomous thought must be a durable queue
        # job.  Refuse an accidental fallback to the legacy direct LLM path.
        try:
            from colony_sidecar.cognition.goal_spine import cognition_spine_exclusive
            if cognition_spine_exclusive():
                return None
        except Exception:
            pass
        if self._thinker is None:
            return None
        concern = None
        excluded_producers = ("external_event_concerns", "turn_concerns")
        excluding = getattr(self.store, "active_without_producers", None)
        if callable(excluding):
            candidates = excluding(excluded_producers, limit=20)
        else:
            candidates = [
                item for item in self.store.active(limit=200)
                if str(getattr(item, "producer_name", "") or "")
                not in excluded_producers
            ][:20]
        for c in candidates:
            # External reports and conversational evidence may only enter
            # cognition through the durable, read-only ThoughtJob/current-mode
            # admission path. The legacy direct thinker and its on_action
            # callback are never an allowed fallback, even when P3 is disabled
            # or temporarily unattached.
            if c.thoughts_spent < c.max_thoughts:
                concern = c
                break
        if concern is None:
            return None
        try:
            outcome = await self._thinker(concern) or {}
        except Exception as exc:
            logger.warning("workspace thinker failed: %s", exc)
            return None
        progressed = bool(outcome.get("progress"))
        resolved = bool(outcome.get("resolve"))
        note = str(outcome.get("note", ""))[:500]
        # progress sustains salience; no progress decays it harder so
        # rumination on a stuck concern fades instead of looping forever.
        new_sal = concern.salience * (0.9 if progressed else 0.6)
        self.store.record_thought(concern.concern_id, note,
                                  resolved=resolved, salience=new_sal)
        # persist the memory ids the thinker actually recalled about this
        # concern (provenance for the memory-field beams; render draws a beam
        # only when a ref id is also a point on the sampled field).
        refs = outcome.get("memory_refs")
        if refs:
            try:
                self.store.set_memory_refs(concern.concern_id, refs)
            except Exception:
                logger.debug("set_memory_refs failed", exc_info=True)
        self._log(f"thought on {concern.kind}: {concern.summary[:60]} "
                  f"-> {'resolved' if resolved else 'progress' if progressed else 'no progress'}",
                  note)
        action = outcome.get("action")
        if action and self._on_action is not None and workspace_mode() == "live":
            try:
                await self._on_action(concern, action)
            except Exception:
                logger.debug("workspace on_action failed", exc_info=True)
        return {"concern_id": concern.concern_id, "resolved": resolved,
                "progress": progressed, "note": note, "action": action}

    def _log(self, desc: str, note: str) -> None:
        if self._journal is None:
            return
        try:
            self._journal.record("workspace", desc, reasoning=note,
                                  decision="noted", outcome="thought")
        except Exception:
            logger.debug("workspace journal write failed", exc_info=True)

    # -- read side --------------------------------------------------------
    def snapshot(
        self,
        limit: int = 24,
        *,
        viewer_person_id: str = "",
        owner_person_id: str = "",
        audiences: set[str] | frozenset[str] = frozenset(),
        unrestricted: bool = True,
    ) -> Dict[str, Any]:
        active = (
            self.store.active(limit=limit)
            if unrestricted
            else self.store.active_for_viewer(
                viewer_person_id=viewer_person_id,
                owner_person_id=owner_person_id,
                audiences=audiences,
                limit=limit,
            )
        )
        result = {"mode": workspace_mode(),
                "capacity": _capacity(),
                "sleeping": in_sleep_window(),
                "concerns": [c.public() for c in active]}
        owner_receipts_visible = unrestricted or bool(
            viewer_person_id
            and owner_person_id
            and viewer_person_id == owner_person_id
            and "owner" in audiences
        )
        if owner_receipts_visible:
            result["recent_resolutions"] = self.store.recent_resolutions()
        if self.event_reducer is not None:
            try:
                result["event_reducer"] = self.event_reducer.status()
            except Exception:
                result["event_reducer"] = {
                    "enabled": True,
                    "healthy": False,
                    "error": "status_unavailable",
                }
        if self.external_event_reducer is not None:
            try:
                result["external_event_reducer"] = (
                    self.external_event_reducer.status()
                )
            except Exception:
                result["external_event_reducer"] = {
                    "enabled": True,
                    "healthy": False,
                    "error": "status_unavailable",
                }
        if self.turn_event_reducer is not None:
            try:
                result["turn_event_reducer"] = self.turn_event_reducer.status()
            except Exception:
                result["turn_event_reducer"] = {
                    "enabled": True,
                    "healthy": False,
                    "error": "status_unavailable",
                }
        return result
