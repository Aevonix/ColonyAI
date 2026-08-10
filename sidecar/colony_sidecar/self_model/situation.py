"""Evidence-derived, scoped present-tense situation spine (Mind M3/P6).

The situation spine is a reducer, not a second world model.  It accepts only
typed observations backed by durable event/receipt references, keeps the
latest observation for a bounded set of present-tense categories, and emits
immutable snapshots.  Model prose is never an observation and a situation
verdict never grants a capability or an action authority.

The module is deliberately deployment-neutral.  ``JournalSituationAdapter``
understands Colony's durable host-journal envelope plus the structured event
families shared with the host.  Deployments may construct ``SituationObservationV1``
directly for attested probes/transports without patching Colony.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

from colony_sidecar.events.journal import current_sequence, replay_events
from colony_sidecar.scope_bounds import VIEWER_SCOPE_MAX_CHARS


SITUATION_VERSION = 1
OBSERVATION_VERSION = 1
VERDICT_VERSION = 1

SITUATION_CATEGORIES = (
    "owner_engagement",
    "activity",
    "conversation",
    "person",
    "channel",
    "service",
    "approval",
    "commitment",
    "project",
    "resource",
    "capability",
    "relationship",
)
_CATEGORY_SET = frozenset(SITUATION_CATEGORIES)
_SHAREABILITY = frozenset(
    {"owner_private", "subject_private", "shared", "public"}
)
_SOURCE_KINDS = frozenset({
    "colony_event",
    "host_event",
    "service_probe",
    "transport_receipt",
    "presence_observation",
    "approval_receipt",
    "project_receipt",
    "work_receipt",
})
_DISALLOWED_SOURCE_KINDS = frozenset(
    {"model", "model_assertion", "inference", "prose", "llm"}
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,255}$")
_SAFE_STATE = re.compile(r"^[a-z][a-z0-9_.:\-]{0,63}$")
_SAFE_ATTRIBUTE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EVIDENCE_PREFIXES = frozenset({
    "journal", "event", "receipt", "host-event", "colony-event",
    "health", "presence", "approval", "project", "work-order",
    "commitment", "channel", "service", "device", "capability",
    "relationship", "situation-snapshot", "observation",
})
_JSON_SCALARS = (str, int, float, bool, type(None))

_DEFAULT_TTLS: Dict[str, float] = {
    "owner_engagement": 120.0,
    "activity": 300.0,
    "conversation": 300.0,
    "person": 300.0,
    "channel": 180.0,
    "service": 120.0,
    "approval": 300.0,
    "commitment": 3600.0,
    "project": 300.0,
    "resource": 300.0,
    "capability": 300.0,
    "relationship": 86400.0,
}


def situation_spine_mode() -> str:
    """Return the explicit migration mode; unset/invalid always fails off."""

    value = os.environ.get("COLONY_SITUATION_SPINE", "off").strip().lower()
    return value if value in {"off", "shadow", "live"} else "off"


def situation_spine_enabled() -> bool:
    return situation_spine_mode() in {"shadow", "live"}


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _epoch(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a timestamp")
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        raw = str(value or "").strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be ISO-8601 or epoch seconds") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        result = parsed.timestamp()
    if not math.isfinite(result) or result <= 0.0 or result > 32_503_680_000.0:
        raise ValueError(f"{field} is outside the supported range")
    return result


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _safe_id(value: Any, field: str, *, maximum: int = 256) -> str:
    result = str(value or "").strip()
    if len(result) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    if not _SAFE_ID.fullmatch(result):
        raise ValueError(f"{field} is invalid")
    return result


def _evidence_refs(values: Iterable[Any], *, maximum: int = 40) -> tuple[str, ...]:
    refs: list[str] = []
    for raw in values or ():
        value = _safe_id(raw, "evidence reference")
        if value.split(":", 1)[0] not in _EVIDENCE_PREFIXES:
            raise ValueError("evidence reference has an unsupported origin")
        if value not in refs:
            refs.append(value)
        if len(refs) > maximum:
            raise ValueError(f"evidence references exceed {maximum}")
    if not refs:
        raise ValueError("at least one durable evidence reference is required")
    return tuple(refs)


def _attributes(value: Optional[Mapping[str, Any]]) -> tuple[tuple[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping) or len(value) > 20:
        raise ValueError("attributes must be an object with at most 20 fields")
    result: list[tuple[str, Any]] = []
    for raw_key, raw_value in sorted(value.items()):
        key = str(raw_key or "").strip().lower()
        if not _SAFE_ATTRIBUTE.fullmatch(key):
            raise ValueError("attribute key is invalid")
        if not isinstance(raw_value, _JSON_SCALARS):
            raise ValueError("situation attributes must be JSON scalars")
        if isinstance(raw_value, float) and not math.isfinite(raw_value):
            raise ValueError("situation attributes must be finite")
        item = raw_value[:300] if isinstance(raw_value, str) else raw_value
        result.append((key, item))
    return tuple(result)


def _validate_scope(
    subject_person_id: str,
    viewer_scope: str,
    shareability: str,
) -> tuple[str, str, str]:
    subject = _safe_id(subject_person_id, "subject_person_id", maximum=128)
    viewer = _safe_id(
        viewer_scope, "viewer_scope", maximum=VIEWER_SCOPE_MAX_CHARS,
    )
    sharing = str(shareability or "").strip().lower()
    if sharing not in _SHAREABILITY:
        raise ValueError("invalid shareability")
    if sharing == "owner_private" and viewer != "owner":
        raise ValueError("owner-private observations require owner viewer scope")
    if sharing == "subject_private" and viewer != f"person:{subject}":
        raise ValueError("subject-private observations require exact subject scope")
    if sharing == "public" and viewer != "public":
        raise ValueError("public observations require public viewer scope")
    if sharing == "shared" and not (
        viewer == "shared" or viewer.startswith("shared:")
    ):
        raise ValueError("shared observations require a shared viewer scope")
    return subject, viewer, sharing


@dataclass(frozen=True)
class SituationObservationV1:
    """One typed, time-bounded observation with durable provenance."""

    observation_id: str
    category: str
    entity_id: str
    state: str
    active: bool
    observed_at: float
    fresh_until: float
    evidence_refs: tuple[str, ...]
    source_kind: str
    subject_person_id: str
    viewer_scope: str
    shareability: str
    attributes: tuple[tuple[str, Any], ...] = ()
    schema: str = "SituationObservationV1"
    version: int = OBSERVATION_VERSION

    def __post_init__(self) -> None:
        _safe_id(self.observation_id, "observation_id")
        if self.category not in _CATEGORY_SET:
            raise ValueError("unsupported situation category")
        _safe_id(self.entity_id, "entity_id")
        if not _SAFE_STATE.fullmatch(self.state):
            raise ValueError("invalid situation state")
        if type(self.active) is not bool:
            raise ValueError("active must be a boolean")
        observed = _epoch(self.observed_at, "observed_at")
        fresh = _epoch(self.fresh_until, "fresh_until")
        if fresh < observed or fresh - observed > 31_536_000.0:
            raise ValueError("freshness interval is invalid or unbounded")
        if _evidence_refs(self.evidence_refs) != self.evidence_refs:
            raise ValueError("evidence references are not canonical")
        source = str(self.source_kind or "").strip().lower()
        if source in _DISALLOWED_SOURCE_KINDS or source not in _SOURCE_KINDS:
            raise ValueError("source_kind is not a trusted observation source")
        if _validate_scope(
            self.subject_person_id, self.viewer_scope, self.shareability,
        ) != (self.subject_person_id, self.viewer_scope, self.shareability):
            raise ValueError("observation scope is not canonical")
        if _attributes(dict(self.attributes)) != self.attributes:
            raise ValueError("attributes are not canonical")
        if self.schema != "SituationObservationV1" or self.version != 1:
            raise ValueError("invalid situation observation schema")

    @classmethod
    def create(
        cls,
        *,
        observation_id: str,
        category: str,
        entity_id: str,
        state: str,
        active: bool,
        observed_at: Any,
        fresh_until: Any = None,
        ttl_seconds: Optional[float] = None,
        evidence_refs: Iterable[Any],
        source_kind: str,
        subject_person_id: str,
        viewer_scope: str,
        shareability: str,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> "SituationObservationV1":
        observed = _epoch(observed_at, "observed_at")
        if fresh_until is None:
            ttl = float(
                ttl_seconds
                if ttl_seconds is not None
                else _DEFAULT_TTLS.get(category, 300.0)
            )
            if ttl < 1.0 or ttl > 31_536_000.0:
                raise ValueError("ttl_seconds is outside the supported range")
            fresh = observed + ttl
        else:
            fresh = _epoch(fresh_until, "fresh_until")
        subject, viewer, sharing = _validate_scope(
            subject_person_id, viewer_scope, shareability,
        )
        return cls(
            observation_id=_safe_id(observation_id, "observation_id"),
            category=str(category or "").strip().lower(),
            entity_id=_safe_id(entity_id, "entity_id"),
            state=str(state or "").strip().lower(),
            active=active,
            observed_at=observed,
            fresh_until=fresh,
            evidence_refs=_evidence_refs(evidence_refs),
            source_kind=str(source_kind or "").strip().lower(),
            subject_person_id=subject,
            viewer_scope=viewer,
            shareability=sharing,
            attributes=_attributes(attributes),
        )

    @property
    def digest(self) -> str:
        return _digest(self.payload())

    def payload(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "observation_id": self.observation_id,
            "category": self.category,
            "entity_id": self.entity_id,
            "state": self.state,
            "active": self.active,
            "observed_at": self.observed_at,
            "fresh_until": self.fresh_until,
            "evidence_refs": list(self.evidence_refs),
            "source_kind": self.source_kind,
            "subject_person_id": self.subject_person_id,
            "viewer_scope": self.viewer_scope,
            "shareability": self.shareability,
            "attributes": dict(self.attributes),
        }


def _queue_worker_count(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


async def task_queue_resource_observation(
    queue: Any,
    *,
    observed_at: Any = None,
) -> SituationObservationV1:
    """Project canonical queue readiness and worker liveness as one resource.

    This is a direct probe of the queue's existing in-process readiness gate
    and durable worker registry.  It does not infer capacity from queued work
    or configured workers: execution is available only when maintenance is
    ready and at least one heartbeat-fresh worker is below capacity.
    """

    readiness_fn = getattr(queue, "execution_readiness", None)
    stats_fn = getattr(queue, "get_queue_stats", None)
    if not callable(readiness_fn) or not callable(stats_fn):
        raise ValueError(
            "task queue resource probe requires readiness and worker truth"
        )
    readiness = readiness_fn()
    if not isinstance(readiness, Mapping):
        raise ValueError("task queue execution readiness must be an object")
    execution_ready = readiness.get("ready")
    if type(execution_ready) is not bool:
        raise ValueError("task queue execution readiness must be boolean")

    stats = await stats_fn()
    registered = _queue_worker_count(
        getattr(stats, "registered_workers", None), "registered_workers",
    )
    active = _queue_worker_count(
        getattr(stats, "active_workers", None), "active_workers",
    )
    stale = _queue_worker_count(
        getattr(stats, "stale_workers", None), "stale_workers",
    )
    available = _queue_worker_count(
        getattr(stats, "available_workers", None), "available_workers",
    )
    if registered != active + stale or available > active:
        raise ValueError("task queue worker freshness counts are inconsistent")
    heartbeat_ttl = getattr(stats, "worker_heartbeat_ttl_secs", None)
    if (
        isinstance(heartbeat_ttl, bool)
        or not isinstance(heartbeat_ttl, (int, float))
        or not math.isfinite(float(heartbeat_ttl))
        or float(heartbeat_ttl) < 1.0
    ):
        raise ValueError("task queue worker heartbeat TTL is invalid")

    observed = _epoch(
        time.time() if observed_at is None else observed_at,
        "observed_at",
    )
    capacity_available = execution_ready and available > 0
    attributes = {
        "active_workers": active,
        "available_workers": available,
        "capacity_available": capacity_available,
        "execution_ready": execution_ready,
        "registered_workers": registered,
        "stale_workers": stale,
    }
    attestation = {
        "schema": "TaskQueueExecutionReadinessProbeV1",
        "version": 1,
        "observed_at": observed,
        "worker_heartbeat_ttl_secs": float(heartbeat_ttl),
        **attributes,
    }
    attestation_digest = _digest(attestation)
    owner = (
        os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
        or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
        or "owner"
    )
    return SituationObservationV1.create(
        observation_id=f"so-{attestation_digest[:24]}",
        category="resource",
        entity_id="task-queue-execution",
        state="available" if capacity_available else "unavailable",
        active=True,
        observed_at=observed,
        ttl_seconds=min(
            _DEFAULT_TTLS["resource"], float(heartbeat_ttl),
        ),
        evidence_refs=(
            f"health:task-queue-execution:{attestation_digest[:24]}",
        ),
        source_kind="service_probe",
        subject_person_id=owner,
        viewer_scope="owner",
        shareability="owner_private",
        attributes=attributes,
    )


@dataclass(frozen=True)
class SituationFactV1:
    observation_id: str
    category: str
    entity_id: str
    state: str
    active: bool
    observed_at: float
    fresh_until: float
    freshness: str
    evidence_refs: tuple[str, ...]
    subject_person_id: str
    viewer_scope: str
    shareability: str
    attributes: tuple[tuple[str, Any], ...] = ()

    def public(self) -> Dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "category": self.category,
            "entity_id": self.entity_id,
            "state": self.state,
            "active": self.active,
            "observed_at": self.observed_at,
            "observed_at_iso": _iso(self.observed_at),
            "fresh_until": self.fresh_until,
            "fresh_until_iso": _iso(self.fresh_until),
            "freshness": self.freshness,
            "evidence_refs": list(self.evidence_refs),
            "subject_person_id": self.subject_person_id,
            "viewer_scope": self.viewer_scope,
            "shareability": self.shareability,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True)
class SituationSnapshotV1:
    snapshot_id: str
    snapshot_digest: str
    subject_person_id: str
    viewer_scope: str
    shareability: str
    as_of: float
    facts: tuple[SituationFactV1, ...]
    coverage: tuple[tuple[str, str], ...]
    omitted: tuple[tuple[str, int], ...]
    evidence_refs: tuple[str, ...]
    schema: str = "SituationSnapshotV1"
    version: int = SITUATION_VERSION

    def freshness(self, category: str) -> str:
        return dict(self.coverage).get(category, "unknown")

    def active_facts(self, category: str) -> tuple[SituationFactV1, ...]:
        return tuple(
            fact for fact in self.facts
            if fact.category == category and fact.active
        )

    def fresh_active_facts(self, category: str) -> tuple[SituationFactV1, ...]:
        """Return only current active facts for an operational decision.

        Stale facts remain visible through ``active_facts`` for explanation,
        but they must not veto a decision merely because another entity made
        the category-level coverage fresh.
        """

        return tuple(
            fact for fact in self.facts
            if fact.category == category
            and fact.active
            and fact.freshness == "fresh"
        )

    def public(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "snapshot_id": self.snapshot_id,
            "snapshot_digest": self.snapshot_digest,
            "subject_person_id": self.subject_person_id,
            "viewer_scope": self.viewer_scope,
            "shareability": self.shareability,
            "as_of": self.as_of,
            "as_of_iso": _iso(self.as_of),
            "coverage": dict(self.coverage),
            "facts": [fact.public() for fact in self.facts],
            "omitted": dict(self.omitted),
            "evidence_refs": list(self.evidence_refs),
        }


def _observation_from_payload(payload: Mapping[str, Any]) -> SituationObservationV1:
    return SituationObservationV1.create(
        observation_id=payload["observation_id"],
        category=payload["category"],
        entity_id=payload["entity_id"],
        state=payload["state"],
        active=payload["active"],
        observed_at=payload["observed_at"],
        fresh_until=payload["fresh_until"],
        evidence_refs=payload["evidence_refs"],
        source_kind=payload["source_kind"],
        subject_person_id=payload["subject_person_id"],
        viewer_scope=payload["viewer_scope"],
        shareability=payload["shareability"],
        attributes=payload.get("attributes") or {},
    )


def _fact_from_payload(payload: Mapping[str, Any]) -> SituationFactV1:
    return SituationFactV1(
        observation_id=str(payload["observation_id"]),
        category=str(payload["category"]),
        entity_id=str(payload["entity_id"]),
        state=str(payload["state"]),
        active=bool(payload["active"]),
        observed_at=float(payload["observed_at"]),
        fresh_until=float(payload["fresh_until"]),
        freshness=str(payload["freshness"]),
        evidence_refs=tuple(payload.get("evidence_refs") or ()),
        subject_person_id=str(payload["subject_person_id"]),
        viewer_scope=str(payload["viewer_scope"]),
        shareability=str(payload["shareability"]),
        attributes=_attributes(payload.get("attributes") or {}),
    )


class SituationStore:
    """SQLite ledger for observations, latest facts, and immutable snapshots."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._closed = False
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS situation_observations (
                observation_id TEXT PRIMARY KEY,
                observation_digest TEXT NOT NULL,
                category TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                subject_person_id TEXT NOT NULL,
                viewer_scope TEXT NOT NULL,
                payload TEXT NOT NULL,
                recorded_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS situation_facts (
                subject_person_id TEXT NOT NULL,
                viewer_scope TEXT NOT NULL,
                category TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                observation_id TEXT NOT NULL,
                observed_at REAL NOT NULL,
                fresh_until REAL NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(subject_person_id,viewer_scope,category,entity_id)
            );
            CREATE INDEX IF NOT EXISTS idx_situation_fact_scope
                ON situation_facts(subject_person_id,viewer_scope,category,observed_at);
            CREATE TABLE IF NOT EXISTS situation_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                snapshot_digest TEXT NOT NULL UNIQUE,
                subject_person_id TEXT NOT NULL,
                viewer_scope TEXT NOT NULL,
                as_of REAL NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS situation_event_cursors (
                consumer_id TEXT PRIMARY KEY,
                last_seq INTEGER NOT NULL,
                bootstrap_mode TEXT NOT NULL,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS situation_event_receipts (
                consumer_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_seq INTEGER NOT NULL,
                event_digest TEXT NOT NULL,
                disposition TEXT NOT NULL,
                observation_ids TEXT NOT NULL,
                processed_at REAL NOT NULL,
                PRIMARY KEY(consumer_id,event_id)
            );
            CREATE TABLE IF NOT EXISTS situation_event_gaps (
                gap_id INTEGER PRIMARY KEY AUTOINCREMENT,
                consumer_id TEXT NOT NULL,
                prior_cursor INTEGER NOT NULL,
                resume_after INTEGER NOT NULL,
                reason TEXT NOT NULL,
                acknowledged_at REAL NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        """Release the feature-owned SQLite handle; exact retries are safe."""

        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def _apply_locked(self, observation: SituationObservationV1) -> str:
        payload = _canonical(observation.payload())
        existing = self._conn.execute(
            "SELECT observation_digest FROM situation_observations "
            "WHERE observation_id=?", (observation.observation_id,),
        ).fetchone()
        if existing is not None:
            if existing["observation_digest"] != observation.digest:
                raise ValueError("observation id replayed with conflicting content")
            return "duplicate"

        current = self._conn.execute(
            "SELECT observation_id,observed_at,payload FROM situation_facts "
            "WHERE subject_person_id=? AND viewer_scope=? AND category=? "
            "AND entity_id=?",
            (
                observation.subject_person_id, observation.viewer_scope,
                observation.category, observation.entity_id,
            ),
        ).fetchone()
        if current is not None and float(current["observed_at"]) == observation.observed_at:
            current_payload = json.loads(current["payload"])
            if _digest(current_payload) != observation.digest:
                raise ValueError("same-time observation conflicts with current fact")

        self._conn.execute(
            "INSERT INTO situation_observations "
            "(observation_id,observation_digest,category,entity_id,"
            "subject_person_id,viewer_scope,payload,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                observation.observation_id, observation.digest,
                observation.category, observation.entity_id,
                observation.subject_person_id, observation.viewer_scope,
                payload, time.time(),
            ),
        )
        if current is not None and float(current["observed_at"]) > observation.observed_at:
            return "historical"
        self._conn.execute(
            "INSERT INTO situation_facts "
            "(subject_person_id,viewer_scope,category,entity_id,observation_id,"
            "observed_at,fresh_until,payload) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(subject_person_id,viewer_scope,category,entity_id) "
            "DO UPDATE SET observation_id=excluded.observation_id,"
            "observed_at=excluded.observed_at,fresh_until=excluded.fresh_until,"
            "payload=excluded.payload",
            (
                observation.subject_person_id, observation.viewer_scope,
                observation.category, observation.entity_id,
                observation.observation_id, observation.observed_at,
                observation.fresh_until, payload,
            ),
        )
        return "applied"

    def ingest(self, observation: SituationObservationV1) -> Dict[str, Any]:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                disposition = self._apply_locked(observation)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {
            "disposition": disposition,
            "observation_id": observation.observation_id,
        }

    @staticmethod
    def _visible(row: sqlite3.Row, viewer_scope: str) -> bool:
        payload = json.loads(row["payload"])
        sharing = payload.get("shareability")
        declared = payload.get("viewer_scope")
        if sharing == "public":
            return True
        return declared == viewer_scope

    def snapshot(
        self,
        *,
        subject_person_id: str,
        viewer_scope: str,
        as_of: Optional[float] = None,
        per_category_limit: int = 20,
        total_limit: int = 160,
    ) -> SituationSnapshotV1:
        subject = _safe_id(subject_person_id, "subject_person_id", maximum=128)
        viewer = _safe_id(
            viewer_scope, "viewer_scope", maximum=VIEWER_SCOPE_MAX_CHARS,
        )
        point = _epoch(as_of if as_of is not None else time.time(), "as_of")
        per_limit = max(1, min(50, int(per_category_limit)))
        overall = max(1, min(200, int(total_limit)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM situation_facts WHERE subject_person_id=? "
                "ORDER BY category ASC,observed_at DESC,entity_id ASC",
                (subject,),
            ).fetchall()

        grouped: Dict[str, list[SituationFactV1]] = {
            category: [] for category in SITUATION_CATEGORIES
        }
        omitted: Dict[str, int] = {}
        for row in rows:
            if not self._visible(row, viewer):
                continue
            payload = json.loads(row["payload"])
            category = str(payload["category"])
            if len(grouped[category]) >= per_limit:
                omitted[category] = omitted.get(category, 0) + 1
                continue
            freshness = "fresh" if float(payload["fresh_until"]) >= point else "stale"
            fact = SituationFactV1(
                observation_id=str(payload["observation_id"]),
                category=category,
                entity_id=str(payload["entity_id"]),
                state=str(payload["state"]),
                active=bool(payload["active"]),
                observed_at=float(payload["observed_at"]),
                fresh_until=float(payload["fresh_until"]),
                freshness=freshness,
                evidence_refs=tuple(payload["evidence_refs"]),
                subject_person_id=str(payload["subject_person_id"]),
                viewer_scope=str(payload["viewer_scope"]),
                shareability=str(payload["shareability"]),
                attributes=_attributes(payload.get("attributes") or {}),
            )
            grouped[category].append(fact)

        facts: list[SituationFactV1] = []
        coverage: list[tuple[str, str]] = []
        for category in SITUATION_CATEGORIES:
            available = grouped[category]
            if not available:
                coverage.append((category, "unknown"))
                continue
            coverage.append((
                category,
                "fresh" if any(f.freshness == "fresh" for f in available)
                else "stale",
            ))
            for fact in available:
                if len(facts) >= overall:
                    omitted[category] = omitted.get(category, 0) + 1
                else:
                    facts.append(fact)

        refs = tuple(dict.fromkeys(
            ref for fact in facts for ref in fact.evidence_refs
        ))[:100]
        sharing = (
            "owner_private" if viewer == "owner"
            else "subject_private" if viewer == f"person:{subject}"
            else "public" if viewer == "public"
            else "shared"
        )
        authority = {
            "schema": "SituationSnapshotV1",
            "version": 1,
            "subject_person_id": subject,
            "viewer_scope": viewer,
            "shareability": sharing,
            "as_of": point,
            "facts": [fact.public() for fact in facts],
            "coverage": dict(coverage),
            "omitted": omitted,
            "evidence_refs": list(refs),
        }
        digest = _digest(authority)
        snapshot = SituationSnapshotV1(
            snapshot_id=f"ss-{digest[:24]}",
            snapshot_digest=digest,
            subject_person_id=subject,
            viewer_scope=viewer,
            shareability=sharing,
            as_of=point,
            facts=tuple(facts),
            coverage=tuple(coverage),
            omitted=tuple(sorted(omitted.items())),
            evidence_refs=refs,
        )
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO situation_snapshots "
                "(snapshot_id,snapshot_digest,subject_person_id,viewer_scope,"
                "as_of,payload,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    snapshot.snapshot_id, snapshot.snapshot_digest, subject,
                    viewer, point, _canonical(snapshot.public()), time.time(),
                ),
            )
            self._conn.commit()
        return snapshot

    def get_snapshot(
        self, snapshot_id: str, *, viewer_scope: str,
    ) -> Optional[SituationSnapshotV1]:
        snapshot_key = _safe_id(snapshot_id, "snapshot_id")
        viewer = _safe_id(
            viewer_scope, "viewer_scope", maximum=VIEWER_SCOPE_MAX_CHARS,
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT payload,viewer_scope FROM situation_snapshots "
                "WHERE snapshot_id=?", (snapshot_key,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        if row["viewer_scope"] != viewer and payload.get("shareability") != "public":
            return None
        return SituationSnapshotV1(
            snapshot_id=payload["snapshot_id"],
            snapshot_digest=payload["snapshot_digest"],
            subject_person_id=payload["subject_person_id"],
            viewer_scope=payload["viewer_scope"],
            shareability=payload["shareability"],
            as_of=float(payload["as_of"]),
            facts=tuple(_fact_from_payload(fact) for fact in payload["facts"]),
            coverage=tuple(
                (category, (payload.get("coverage") or {}).get(category, "unknown"))
                for category in SITUATION_CATEGORIES
            ),
            omitted=tuple(sorted((payload.get("omitted") or {}).items())),
            evidence_refs=tuple(payload.get("evidence_refs") or ()),
        )

    def initialize_cursor(
        self, consumer_id: str, sequence: int, *, bootstrap_mode: str,
    ) -> int:
        consumer = _safe_id(consumer_id, "consumer_id", maximum=128)
        seq = max(0, int(sequence))
        if bootstrap_mode not in {"tail", "replay"}:
            raise ValueError("invalid bootstrap mode")
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO situation_event_cursors "
                "(consumer_id,last_seq,bootstrap_mode,updated_at) VALUES (?,?,?,?)",
                (consumer, seq, bootstrap_mode, time.time()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT last_seq FROM situation_event_cursors WHERE consumer_id=?",
                (consumer,),
            ).fetchone()
        return int(row["last_seq"])

    def cursor(self, consumer_id: str) -> Optional[int]:
        consumer = _safe_id(consumer_id, "consumer_id", maximum=128)
        with self._lock:
            row = self._conn.execute(
                "SELECT last_seq FROM situation_event_cursors WHERE consumer_id=?",
                (consumer,),
            ).fetchone()
        return int(row["last_seq"]) if row is not None else None

    def set_cursor_error(self, consumer_id: str, error: str) -> None:
        consumer = _safe_id(consumer_id, "consumer_id", maximum=128)
        with self._lock:
            self._conn.execute(
                "UPDATE situation_event_cursors SET last_error=?,updated_at=? "
                "WHERE consumer_id=?", (str(error)[:200], time.time(), consumer),
            )
            self._conn.commit()

    def acknowledge_gap(
        self,
        consumer_id: str,
        *,
        prior_cursor: int,
        resume_after: int,
        reason: str,
    ) -> int:
        consumer = _safe_id(consumer_id, "consumer_id", maximum=128)
        prior = int(prior_cursor)
        resume = int(resume_after)
        if resume <= prior:
            raise ValueError("situation gap resume cursor must advance")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT last_seq FROM situation_event_cursors "
                    "WHERE consumer_id=?", (consumer,),
                ).fetchone()
                if row is None or int(row["last_seq"]) != prior:
                    raise ValueError("situation gap cursor changed")
                now = time.time()
                self._conn.execute(
                    "INSERT INTO situation_event_gaps "
                    "(consumer_id,prior_cursor,resume_after,reason,acknowledged_at) "
                    "VALUES (?,?,?,?,?)",
                    (consumer, prior, resume, str(reason)[:200], now),
                )
                self._conn.execute(
                    "UPDATE situation_event_cursors SET last_seq=?,last_error='',"
                    "updated_at=? WHERE consumer_id=?",
                    (resume, now, consumer),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return resume

    def reduce_event(
        self,
        *,
        consumer_id: str,
        event_seq: int,
        event_id: str,
        event_digest: str,
        observations: Sequence[SituationObservationV1],
        disposition: str,
    ) -> Dict[str, Any]:
        consumer = _safe_id(consumer_id, "consumer_id", maximum=128)
        native_id = _safe_id(event_id, "event_id")
        digest = _safe_id(event_digest, "event_digest")
        seq = int(event_seq)
        if seq < 1:
            raise ValueError("event sequence must be positive")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                prior = self._conn.execute(
                    "SELECT * FROM situation_event_receipts WHERE consumer_id=? "
                    "AND event_id=?", (consumer, native_id),
                ).fetchone()
                cursor_row = self._conn.execute(
                    "SELECT last_seq FROM situation_event_cursors "
                    "WHERE consumer_id=?", (consumer,),
                ).fetchone()
                cursor = (
                    int(cursor_row["last_seq"])
                    if cursor_row is not None else None
                )
                if prior is not None:
                    if (
                        int(prior["event_seq"]) != seq
                        or prior["event_digest"] != digest
                    ):
                        raise ValueError(
                            "event id replayed with conflicting content",
                        )
                    self._conn.commit()
                    return {
                        "disposition": "duplicate_event",
                        "cursor": cursor,
                        "observation_ids": json.loads(prior["observation_ids"]),
                    }
                if cursor is None:
                    raise ValueError("situation event cursor is not initialized")
                if seq != cursor + 1:
                    raise ValueError("event sequence is not cursor-contiguous")
                applied = [self._apply_locked(item) for item in observations]
                ids = [item.observation_id for item in observations]
                self._conn.execute(
                    "INSERT INTO situation_event_receipts "
                    "(consumer_id,event_id,event_seq,event_digest,disposition,"
                    "observation_ids,processed_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        consumer, native_id, seq, digest, disposition,
                        _canonical(ids), time.time(),
                    ),
                )
                self._conn.execute(
                    "UPDATE situation_event_cursors SET last_seq=?,last_error='',"
                    "updated_at=? WHERE consumer_id=?",
                    (seq, time.time(), consumer),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {
            "disposition": disposition,
            "cursor": seq,
            "observation_ids": ids,
            "observation_dispositions": applied,
        }

    def status(self, consumer_id: str) -> Dict[str, Any]:
        consumer = _safe_id(consumer_id, "consumer_id", maximum=128)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM situation_event_cursors WHERE consumer_id=?",
                (consumer,),
            ).fetchone()
            counts = self._conn.execute(
                "SELECT disposition,COUNT(*) n FROM situation_event_receipts "
                "WHERE consumer_id=? GROUP BY disposition", (consumer,),
            ).fetchall()
            gaps = self._conn.execute(
                "SELECT COUNT(*) AS n,MAX(resume_after) AS last_resume "
                "FROM situation_event_gaps WHERE consumer_id=?", (consumer,),
            ).fetchone()
        return {
            "initialized": row is not None,
            "cursor": int(row["last_seq"]) if row is not None else None,
            "bootstrap_mode": row["bootstrap_mode"] if row is not None else None,
            "last_error": row["last_error"] if row is not None else "",
            "gaps": {
                "count": int(gaps["n"] or 0),
                "last_resume": int(gaps["last_resume"] or 0),
            },
            "dispositions": {item["disposition"]: int(item["n"]) for item in counts},
        }


class JournalSituationAdapter:
    """Convert attested structured event families into observations.

    Free-form fields such as ``summary`` and ``description`` are deliberately
    ignored.  An event name/status may select a state; prose cannot.
    """

    _STATE_KEYS = ("state", "status", "availability", "health")
    _ENTITY_KEYS: Dict[str, tuple[str, ...]] = {
        "owner_engagement": ("owner_person_id", "person_id"),
        "activity": ("activity_id", "session_id", "id"),
        "conversation": ("conversation_id", "session_id", "channel_id"),
        "person": ("person_id", "contact_id"),
        "channel": ("channel_id", "channel", "transport", "surface_id"),
        "service": ("service_id", "service", "component"),
        "approval": ("approval_id", "action_digest"),
        "commitment": ("commitment_id",),
        "project": ("project_id",),
        "resource": ("resource_id", "device_id", "work_order_id", "id"),
        "capability": ("capability_id", "capability"),
        "relationship": ("relationship_id", "contact_id", "person_id"),
    }

    @staticmethod
    def _scope(data: Mapping[str, Any]) -> tuple[str, str, str]:
        subject = str(
            data.get("subject_person_id") or data.get("person_id")
            or data.get("contact_id")
            or os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
            or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
            or "owner"
        )
        sharing = str(
            data.get("shareability") or data.get("privacy_scope")
            or "owner_private"
        ).strip().lower()
        if sharing in {"shared", "public"} and data.get("boundary_attested") is not True:
            sharing = "owner_private"
        if sharing == "subject_private":
            viewer = f"person:{subject}"
        elif sharing == "public":
            viewer = "public"
        elif sharing == "shared":
            group = str(data.get("viewer_group") or "").strip()
            viewer = f"shared:{group}" if group else "shared"
        else:
            sharing, viewer = "owner_private", "owner"
        return _validate_scope(subject, viewer, sharing)

    @staticmethod
    def _state(data: Mapping[str, Any], suffix: str) -> str:
        candidate = ""
        for key in JournalSituationAdapter._STATE_KEYS:
            value = str(data.get(key) or "").strip().lower()
            if value:
                candidate = value
                break
        candidate = candidate or suffix.replace("-", "_") or "observed"
        candidate = re.sub(r"[^a-z0-9_.:\-]", "_", candidate)[:64]
        return candidate if _SAFE_STATE.fullmatch(candidate) else "observed"

    @staticmethod
    def _category(event_type: str) -> Optional[str]:
        if event_type == "conversation.turn":
            return "conversation"
        prefix = event_type.split(".", 1)[0]
        return {
            "presence": "person",
            "person": "person",
            "owner": "owner_engagement",
            "activity": "activity",
            "conversation": "conversation",
            "channel": "channel",
            "delivery": "channel",
            "outreach": "channel",
            "service": "service",
            "health": "service",
            "approval": "approval",
            "commitment": "commitment",
            "project": "project",
            "work_order": "resource",
            "work": "resource",
            "resource": "resource",
            "device": "resource",
            "capability": "capability",
            "relationship": "relationship",
            "contact": "relationship",
        }.get(prefix)

    @staticmethod
    def _active(category: str, state: str) -> bool:
        terminal: Dict[str, frozenset[str]] = {
            "conversation": frozenset({"ended", "closed"}),
            "person": frozenset({"left", "absent"}),
            "approval": frozenset({
                "approved", "denied", "expired", "cancelled", "consumed",
            }),
            "commitment": frozenset({"fulfilled", "cancelled", "resolved"}),
            "project": frozenset({
                "completed", "verified", "failed", "abandoned",
                "cancelled", "resolved",
            }),
            "relationship": frozenset({"ended", "closed", "resolved"}),
        }
        # Service/channel/resource facts remain active even when negative: a
        # current "offline" probe is precisely what policy must see.
        return state not in terminal.get(category, frozenset())

    @classmethod
    def adapt(
        cls,
        event: Mapping[str, Any],
        *,
        source_kind: str = "colony_event",
    ) -> tuple[tuple[SituationObservationV1, ...], str, str]:
        if not isinstance(event, Mapping):
            raise ValueError("event must be an object")
        seq = int(event.get("seq") or 0)
        event_id = _safe_id(
            event.get("ulid") or event.get("event_id") or f"journal-seq-{seq}",
            "event_id",
        )
        event_type = str(event.get("type") or "").strip().lower()
        if seq < 1 or not re.fullmatch(r"[a-z0-9][a-z0-9_.:\-]{0,127}", event_type):
            raise ValueError("malformed event identity")
        data = event.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("event data must be an object")
        occurred = _epoch(
            event.get("occurredAt") or event.get("occurred_at")
            or event.get("recordedAt"),
            "occurred_at",
        )
        raw_digest = _digest({
            "type": event_type, "occurred_at": occurred, "data": data,
        })
        if event_type == "cognition.external.service_state":
            from colony_sidecar.cognition.external_events import (
                validate_external_journal_projection,
            )

            projection = validate_external_journal_projection(event_type, data)
            external = projection["attributes"]
            entity = str(external["service"])
            state = str(external["state"])
            subject, viewer, sharing = _validate_scope(
                str(projection["subject_person_id"]),
                str(projection["viewer_scope"]),
                str(projection["shareability"]),
            )
            evidence = (f"journal:{seq}:{event_id}",)
            attributes: Dict[str, Any] = {"event_type": event_type}
            for key in ("latency_ms", "observed_samples"):
                if key in external:
                    attributes[key] = external[key]
            observation = SituationObservationV1.create(
                observation_id=(
                    f"so-{_digest((event_id, 'service', entity))[:24]}"
                ),
                category="service",
                entity_id=entity,
                state=state,
                active=cls._active("service", state),
                observed_at=projection["external_occurred_at"],
                ttl_seconds=_DEFAULT_TTLS["service"],
                evidence_refs=evidence,
                source_kind=source_kind,
                subject_person_id=subject,
                viewer_scope=viewer,
                shareability=sharing,
                attributes=attributes,
            )
            return (observation,), "projected", raw_digest

        category = cls._category(event_type)
        if category is None:
            return (), "unmapped_event_type", raw_digest

        suffix = event_type.rsplit(".", 1)[-1]
        state = cls._state(data, suffix)
        subject, viewer, sharing = cls._scope(data)
        entity = ""
        for key in cls._ENTITY_KEYS[category]:
            raw = str(data.get(key) or "").strip()
            if raw and _SAFE_ID.fullmatch(raw):
                entity = raw
                break
        if not entity and category == "owner_engagement":
            entity = subject
        if not entity:
            return (), f"missing_{category}_entity", raw_digest
        evidence = (f"journal:{seq}:{event_id}",)
        active = cls._active(category, state)
        attributes: Dict[str, Any] = {"event_type": event_type}
        for key in (
            "channel_id", "channel", "surface_id", "transport", "interruption_cost",
            "pending_count", "capacity_available", "recipient_person_id",
        ):
            value = data.get(key)
            if isinstance(value, _JSON_SCALARS) and value is not None:
                attributes[key] = value
        primary = SituationObservationV1.create(
            observation_id=f"so-{_digest((event_id, category, entity))[:24]}",
            category=category,
            entity_id=entity,
            state=state,
            active=active,
            observed_at=occurred,
            ttl_seconds=_DEFAULT_TTLS[category],
            evidence_refs=evidence,
            source_kind=source_kind,
            subject_person_id=subject,
            viewer_scope=viewer,
            shareability=sharing,
            attributes=attributes,
        )
        observations: list[SituationObservationV1] = [primary]

        # A conversation turn is also real, short-lived evidence of the
        # participant and channel.  It says nothing about room occupancy.
        if event_type == "conversation.turn":
            contact = str(data.get("contact_id") or "").strip()
            channel = str(data.get("channel_id") or "").strip()
            if contact and _SAFE_ID.fullmatch(contact):
                observations.append(SituationObservationV1.create(
                    observation_id=f"so-{_digest((event_id, 'person', contact))[:24]}",
                    category="person", entity_id=contact, state="recently_active",
                    active=True, observed_at=occurred,
                    ttl_seconds=_DEFAULT_TTLS["person"], evidence_refs=evidence,
                    source_kind=source_kind, subject_person_id=subject,
                    viewer_scope=viewer, shareability=sharing,
                    attributes={"channel_id": channel} if channel else {},
                ))
            if channel and _SAFE_ID.fullmatch(channel):
                observations.append(SituationObservationV1.create(
                    observation_id=f"so-{_digest((event_id, 'channel', channel))[:24]}",
                    category="channel", entity_id=channel, state="recently_active",
                    active=True, observed_at=occurred,
                    ttl_seconds=_DEFAULT_TTLS["channel"], evidence_refs=evidence,
                    source_kind=source_kind, subject_person_id=subject,
                    viewer_scope=viewer, shareability=sharing,
                ))
        return tuple(observations), "projected", raw_digest


class SituationReducer:
    """Idempotently consume the durable host journal into ``SituationStore``."""

    def __init__(
        self,
        store: SituationStore,
        *,
        consumer_id: str = "situation-spine-v1",
        replay_fn: Callable[..., Mapping[str, Any]] = replay_events,
        current_sequence_fn: Callable[[], int] = current_sequence,
        adapter: Any = JournalSituationAdapter,
    ) -> None:
        self.store = store
        self.consumer_id = consumer_id
        self._replay = replay_fn
        self._current_sequence = current_sequence_fn
        self._adapter = adapter

    @property
    def mode(self) -> str:
        return situation_spine_mode()

    def _bootstrap_mode(self) -> str:
        value = os.environ.get(
            "COLONY_SITUATION_BOOTSTRAP", "tail",
        ).strip().lower()
        return value if value in {"tail", "replay"} else "tail"

    @staticmethod
    def _replay_integrity_error(snapshot: Mapping[str, Any]) -> str:
        if snapshot.get("replayError"):
            return "journal_unavailable"
        try:
            corrupt = int(snapshot.get("corruptCount") or 0)
            first = int(snapshot.get("firstAvailableSeq") or 0)
            high = int(snapshot.get("journalLastSeq") or 0)
        except (TypeError, ValueError):
            return "journal_metadata_invalid"
        if corrupt < 0 or first < 0 or high < 0 or (first and first > high):
            return "journal_metadata_invalid"
        if corrupt:
            return f"journal_corruption_detected:{corrupt}"
        if not isinstance(snapshot.get("events") or (), (list, tuple)):
            return "journal_metadata_invalid"
        return ""

    @staticmethod
    def _gap_policy() -> str:
        return os.environ.get(
            "COLONY_SITUATION_GAP_POLICY", "stop",
        ).strip().lower()

    def _stop(self, error: str, *, processed: int = 0,
              cursor: Optional[int] = None) -> Dict[str, Any]:
        self.store.set_cursor_error(self.consumer_id, error)
        result: Dict[str, Any] = {
            "enabled": True, "mode": self.mode,
            "processed": int(processed), "error": error,
        }
        if cursor is not None:
            result["cursor"] = int(cursor)
        return result

    def run_once(self, *, limit: int = 100) -> Dict[str, Any]:
        if self.mode == "off":
            return {"enabled": False, "processed": 0, "mode": "off"}
        cursor = self.store.cursor(self.consumer_id)
        if cursor is None:
            head = self._replay(after_seq=0, limit=1)
            bootstrap = self._bootstrap_mode()
            integrity_error = self._replay_integrity_error(head)
            if integrity_error:
                initial = 0
            else:
                first = int(head.get("firstAvailableSeq") or 0)
                high = int(head.get("journalLastSeq") or 0)
                initial = high if bootstrap == "tail" else max(0, first - 1)
            cursor = self.store.initialize_cursor(
                self.consumer_id, initial, bootstrap_mode=bootstrap,
            )
            if integrity_error:
                return self._stop(integrity_error, cursor=cursor)
            if bootstrap == "tail":
                return {
                    "enabled": True, "mode": self.mode, "processed": 0,
                    "bootstrapped": True, "bootstrap_mode": bootstrap,
                    "cursor": cursor,
                }

        batch = self._replay(
            after_seq=cursor, limit=max(1, min(500, int(limit))),
        )
        integrity_error = self._replay_integrity_error(batch)
        if integrity_error:
            return self._stop(integrity_error, cursor=cursor)
        journal_high = int(batch.get("journalLastSeq") or 0)
        if cursor > journal_high:
            return self._stop(
                f"event_journal_rewind:{cursor}:{journal_high}",
                cursor=cursor,
            )
        first = int(batch.get("firstAvailableSeq") or 0)
        if first and cursor < first - 1:
            error = f"journal_retention_gap:{cursor}:{first}"
            if self._gap_policy() != "acknowledge":
                return self._stop(error, cursor=cursor)
            cursor = self.store.acknowledge_gap(
                self.consumer_id,
                prior_cursor=cursor,
                resume_after=first - 1,
                reason=error,
            )
            batch = self._replay(
                after_seq=cursor, limit=max(1, min(500, int(limit))),
            )
            integrity_error = self._replay_integrity_error(batch)
            if integrity_error:
                return self._stop(integrity_error, cursor=cursor)
            journal_high = int(batch.get("journalLastSeq") or 0)
            if cursor > journal_high:
                return self._stop(
                    f"event_journal_rewind:{cursor}:{journal_high}",
                    cursor=cursor,
                )

        counts: Dict[str, int] = {}
        last_seq = cursor
        for event in batch.get("events") or ():
            try:
                if not isinstance(event, Mapping):
                    raise ValueError("journal event is not an object")
                sequence = int(event.get("seq") or 0)
                if sequence <= last_seq:
                    raise ValueError("journal event sequence is not increasing")
                if sequence != last_seq + 1:
                    error = f"journal_sequence_gap:{last_seq}:{sequence}"
                    if self._gap_policy() != "acknowledge":
                        return self._stop(
                            error, processed=sum(counts.values()),
                            cursor=last_seq,
                        )
                    last_seq = self.store.acknowledge_gap(
                        self.consumer_id,
                        prior_cursor=last_seq,
                        resume_after=sequence - 1,
                        reason=error,
                    )
                observations, disposition, digest = self._adapter.adapt(event)
                result = self.store.reduce_event(
                    consumer_id=self.consumer_id,
                    event_seq=int(event["seq"]),
                    event_id=str(event.get("ulid") or f"journal-seq-{event['seq']}"),
                    event_digest=digest,
                    observations=observations,
                    disposition=disposition,
                )
                key = str(result["disposition"])
            except Exception as exc:
                self.store.set_cursor_error(
                    self.consumer_id, f"malformed_event:{type(exc).__name__}",
                )
                return {
                    "enabled": True,
                    "mode": self.mode,
                    "processed": sum(counts.values()),
                    "error": f"malformed_event:{type(exc).__name__}",
                }
            counts[key] = counts.get(key, 0) + 1
            last_seq = int(result.get("cursor") or last_seq)
        journal_high = int(batch.get("journalLastSeq") or 0)
        if not bool(batch.get("hasMore")) and journal_high > last_seq:
            error = f"journal_sequence_gap:{last_seq}:{journal_high + 1}"
            if self._gap_policy() != "acknowledge":
                return self._stop(
                    error, processed=sum(counts.values()), cursor=last_seq,
                )
            last_seq = self.store.acknowledge_gap(
                self.consumer_id,
                prior_cursor=last_seq,
                resume_after=journal_high,
                reason=error,
            )
        return {
            "enabled": True,
            "mode": self.mode,
            "processed": sum(counts.values()),
            "dispositions": counts,
            "cursor": last_seq,
            "has_more": bool(batch.get("hasMore")),
        }

    def status(self) -> Dict[str, Any]:
        result = self.store.status(self.consumer_id)
        result.update({
            "mode": self.mode,
            "enabled": self.mode != "off",
            "healthy": not bool(result.get("last_error")),
            "journal_high_water": int(self._current_sequence()),
        })
        return result


@dataclass(frozen=True)
class SituationPolicyVerdictV1:
    verdict_id: str
    action: str
    allowed: bool
    reason: str
    evidence_refs: tuple[str, ...]
    snapshot_id: str
    operation: str
    does_not_grant_authority: bool = True
    schema: str = "SituationPolicyVerdictV1"
    version: int = VERDICT_VERSION

    def __post_init__(self) -> None:
        _safe_id(self.verdict_id, "verdict_id")
        _safe_id(self.snapshot_id, "snapshot_id")
        if self.action not in {"allow", "ask", "hold"}:
            raise ValueError("invalid situation policy action")
        if self.allowed is not (self.action == "allow"):
            raise ValueError("situation policy allowed/action mismatch")
        if self.does_not_grant_authority is not True:
            raise ValueError("situation verdict cannot grant authority")
        if _evidence_refs(self.evidence_refs) != self.evidence_refs:
            raise ValueError("situation verdict evidence is not canonical")
        if self.schema != "SituationPolicyVerdictV1" or self.version != 1:
            raise ValueError("invalid situation policy schema")

    def as_policy_result(self) -> Dict[str, Any]:
        """Shape consumed by P3's deterministic policy normalizer."""

        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "situation_verdict_ref": f"situation-verdict:{self.verdict_id}",
            "does_not_grant_authority": True,
        }


class AppropriatenessGate:
    """Pure situational policy.  It can hold/ask; it cannot authorize work."""

    _DEGRADED = frozenset({
        "degraded", "down", "offline", "unavailable", "unhealthy", "failed",
    })
    _BUSY = frozenset({
        "busy", "do_not_disturb", "sleeping", "driving", "in_call",
    })

    def __init__(self, *, mode_fn: Callable[[], str] = situation_spine_mode) -> None:
        self._mode_fn = mode_fn

    @staticmethod
    def _verdict(
        snapshot: SituationSnapshotV1,
        operation: str,
        action: str,
        reason: str,
        refs: Iterable[str],
    ) -> SituationPolicyVerdictV1:
        evidence = tuple(dict.fromkeys((
            f"situation-snapshot:{snapshot.snapshot_id}", *refs,
        )))[:40]
        authority = {
            "snapshot_id": snapshot.snapshot_id,
            "operation": operation,
            "action": action,
            "reason": reason,
            "evidence_refs": evidence,
            "does_not_grant_authority": True,
        }
        return SituationPolicyVerdictV1(
            verdict_id=_digest(authority)[:24],
            action=action,
            allowed=action == "allow",
            reason=reason,
            evidence_refs=evidence,
            snapshot_id=snapshot.snapshot_id,
            operation=operation,
        )

    def evaluate(
        self,
        snapshot: SituationSnapshotV1,
        *,
        operation: str,
        required_categories: Iterable[str] = (),
        target_channel: str = "",
        recipient_person_id: str = "",
    ) -> SituationPolicyVerdictV1:
        op = str(operation or "").strip().lower()
        if not _SAFE_STATE.fullmatch(op):
            raise ValueError("operation is invalid")
        mode = self._mode_fn()
        if mode != "live":
            reason = "situation_spine_off" if mode == "off" else "situation_shadow_observation_only"
            return self._verdict(snapshot, op, "hold", reason, ())

        required = tuple(dict.fromkeys(str(item).strip() for item in required_categories))
        if any(item not in _CATEGORY_SET for item in required):
            raise ValueError("required category is unsupported")
        for category in required:
            freshness = snapshot.freshness(category)
            if freshness == "unknown":
                return self._verdict(
                    snapshot, op, "ask", f"situation_unknown:{category}", (),
                )
            if freshness == "stale":
                stale = snapshot.active_facts(category)
                return self._verdict(
                    snapshot, op, "hold", f"situation_stale:{category}",
                    (ref for fact in stale for ref in fact.evidence_refs),
                )

        if "service" in required:
            service_facts = snapshot.fresh_active_facts("service")
            degraded = [
                fact for fact in service_facts if fact.state in self._DEGRADED
            ]
            if degraded:
                return self._verdict(
                    snapshot, op, "hold", "required_service_degraded",
                    (ref for fact in degraded for ref in fact.evidence_refs),
                )

        for category in ("resource", "capability"):
            if category not in required:
                continue
            unavailable = [
                fact for fact in snapshot.fresh_active_facts(category)
                if fact.state in self._DEGRADED
                or dict(fact.attributes).get("capacity_available") is False
            ]
            if unavailable:
                return self._verdict(
                    snapshot, op, "hold", f"required_{category}_unavailable",
                    (ref for fact in unavailable for ref in fact.evidence_refs),
                )

        if "approval" in required:
            pending_approvals = [
                fact for fact in snapshot.fresh_active_facts("approval")
                if fact.state in {
                    "pending", "required", "awaiting_approval", "requested",
                }
            ]
            if pending_approvals:
                return self._verdict(
                    snapshot, op, "ask", "pending_approval_requires_decision",
                    (
                        ref for fact in pending_approvals
                        for ref in fact.evidence_refs
                    ),
                )

        if target_channel:
            channel = [
                fact for fact in snapshot.active_facts("channel")
                if fact.entity_id == target_channel
            ]
            if not channel:
                return self._verdict(
                    snapshot, op, "ask", "target_channel_state_unknown", (),
                )
            if channel[0].freshness != "fresh" or channel[0].state in self._DEGRADED:
                return self._verdict(
                    snapshot, op, "hold", "target_channel_unavailable",
                    channel[0].evidence_refs,
                )

        if op in {"outreach", "proactive_delivery", "interrupt"}:
            engagement = snapshot.fresh_active_facts("owner_engagement")
            if not engagement or snapshot.freshness("owner_engagement") != "fresh":
                return self._verdict(
                    snapshot, op, "ask", "owner_availability_unknown", (),
                )
            busy = [fact for fact in engagement if fact.state in self._BUSY]
            if busy:
                return self._verdict(
                    snapshot, op, "hold", "owner_interruption_cost_high",
                    (ref for fact in busy for ref in fact.evidence_refs),
                )
            active_conversations = snapshot.fresh_active_facts("conversation")
            if active_conversations:
                return self._verdict(
                    snapshot, op, "hold", "active_conversation_in_progress",
                    (ref for fact in active_conversations for ref in fact.evidence_refs),
                )
            if recipient_person_id:
                others = [
                    fact for fact in snapshot.fresh_active_facts("person")
                    if fact.entity_id != recipient_person_id
                ]
                if others:
                    return self._verdict(
                        snapshot, op, "hold", "recipient_privacy_context_uncertain",
                        (ref for fact in others for ref in fact.evidence_refs),
                    )

        return self._verdict(
            snapshot, op, "allow", "fresh_situation_allows", snapshot.evidence_refs,
        )

    def for_goal_proposal(
        self,
        proposal: Any,
        concern: Any,
        snapshot: SituationSnapshotV1,
        *,
        required_categories: Iterable[str] = ("resource", "service"),
    ) -> Dict[str, Any]:
        """P3 adapter; scope equality is checked before situational policy."""

        if (
            str(getattr(proposal, "subject_person_id", ""))
            != snapshot.subject_person_id
            or str(getattr(proposal, "viewer_scope", "")) != snapshot.viewer_scope
            or str(getattr(concern, "subject_person_id", ""))
            != snapshot.subject_person_id
        ):
            return self._verdict(
                snapshot, "project_start", "hold", "situation_scope_mismatch", (),
            ).as_policy_result()
        return self.evaluate(
            snapshot,
            operation="project_start",
            required_categories=required_categories,
        ).as_policy_result()
