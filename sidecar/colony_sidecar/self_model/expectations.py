"""Expectation engine: predictions with teeth (Mind M3a).

The agent forms explicit predictions ("this commitment gets done by its due
date", "this contact replies within a day"), and a checker resolves them at
their horizon against reality. A miss is a SURPRISE, the attention signal
biological cognition runs on: surprises raise workspace salience, and the
hit/miss record yields a per-domain calibration score (Brier) that feeds the
selfhood benchmark. Without this, "she has a world model" is a claim; with
it, calibration is a number that can improve.

Resolvers are pluggable by subject prefix so the store stays generic: a
deployment (or another subsystem) registers how to check a class of
predictions. One built-in resolver covers commitment predictions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from colony_sidecar.scope_bounds import VIEWER_SCOPE_MAX_CHARS

logger = logging.getLogger(__name__)

OUTCOMES = ("pending", "hit", "miss", "unresolved")
EXPECTATION_VERSION = 2
_SHAREABILITY = frozenset(
    {"owner_private", "subject_private", "shared", "public"}
)
_OUTCOME_SOURCE_KINDS = frozenset({
    "colony_event", "host_event", "transport_receipt", "service_probe",
    "commitment_receipt", "task_receipt", "relationship_receipt",
    "project_receipt", "work_receipt",
})
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,255}$")
_EVIDENCE_PREFIXES = frozenset({
    "journal", "event", "receipt", "host-event", "colony-event",
    "commitment", "contact", "task", "service", "relationship",
    "project", "work-order", "observation", "expectation", "health",
    "execution-result", "execution-attempt", "project-terminal",
    "presence", "approval", "channel", "device", "capability",
})


def expectations_mode() -> str:
    """Effective COLONY_EXPECTATIONS: explicit env > autonomy preset > off.

    Expectations are binary in behavior (they only measure), so "on" is the
    canonical enabled value; the historical "shadow"/"live" spellings remain
    accepted aliases. An explicitly-set invalid value falls back to "off",
    exactly as the legacy reader treated any unrecognized value.
    """
    from colony_sidecar.util.autonomy_preset import resolve
    return resolve("COLONY_EXPECTATIONS", ("off", "on", "shadow", "live"),
                   "off")


def expectations_enabled() -> bool:
    return expectations_mode() != "off"


def _now() -> float:
    return time.time()


@dataclass
class ExpectationV2:
    prediction_id: str
    subject: str            # e.g. "commitment:cid-..", "contact:cid-.."
    domain: str             # calibration bucket, e.g. "commitment", "cadence"
    expectation: str        # human-readable
    confidence: float       # 0..1
    horizon: float          # epoch when it resolves
    source: str
    outcome: str = "pending"
    resolved_at: Optional[float] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    schema_version: int = 1
    subject_person_id: str = "owner"
    viewer_scope: str = "owner"
    shareability: str = "owner_private"
    evidence_refs: tuple[str, ...] = ()
    source_kind: str = "legacy"
    cohort: str = "legacy"
    outcome_observation_id: str = ""
    outcome_observed_at: Optional[float] = None
    outcome_evidence_refs: tuple[str, ...] = ()
    resolution_digest: str = ""

    def public(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["schema"] = "ExpectationV2" if self.schema_version == 2 else "PredictionV1"
        d["evidence_refs"] = list(self.evidence_refs)
        d["outcome_evidence_refs"] = list(self.outcome_evidence_refs)
        d["horizon_iso"] = datetime.fromtimestamp(
            self.horizon, tz=timezone.utc).isoformat()
        if self.outcome_observed_at is not None:
            d["outcome_observed_at_iso"] = datetime.fromtimestamp(
                self.outcome_observed_at, tz=timezone.utc,
            ).isoformat()
        return d


# Backwards-compatible public name used throughout Colony.  Existing V1 rows
# are adapted into the expanded record with ``schema_version=1``.
Prediction = ExpectationV2


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _refs(values: Iterable[Any], *, required: bool = True) -> tuple[str, ...]:
    result: list[str] = []
    for raw in values or ():
        value = str(raw or "").strip()
        if not _SAFE_REF.fullmatch(value):
            raise ValueError("invalid expectation evidence reference")
        if value.split(":", 1)[0] not in _EVIDENCE_PREFIXES:
            raise ValueError("unsupported expectation evidence origin")
        if value not in result:
            result.append(value)
        if len(result) > 40:
            raise ValueError("expectation evidence exceeds 40 references")
    if required and not result:
        raise ValueError("ExpectationV2 requires durable evidence")
    return tuple(result)


def _scope(
    subject_person_id: str, viewer_scope: str, shareability: str,
) -> tuple[str, str, str]:
    subject = str(subject_person_id or "").strip()
    viewer = str(viewer_scope or "").strip()
    if len(subject) > 128 or len(viewer) > VIEWER_SCOPE_MAX_CHARS:
        raise ValueError("expectation subject/viewer scope exceeds safe bounds")
    sharing = str(shareability or "").strip().lower()
    if not _SAFE_REF.fullmatch(subject) or not _SAFE_REF.fullmatch(viewer):
        raise ValueError("expectation subject/viewer scope is invalid")
    if sharing not in _SHAREABILITY:
        raise ValueError("expectation shareability is invalid")
    if sharing == "owner_private" and viewer != "owner":
        raise ValueError("owner-private expectation requires owner viewer")
    if sharing == "subject_private" and viewer != f"person:{subject}":
        raise ValueError("subject-private expectation requires exact subject viewer")
    if sharing == "public" and viewer != "public":
        raise ValueError("public expectation requires public viewer")
    if sharing == "shared" and not (
        viewer == "shared" or viewer.startswith("shared:")
    ):
        raise ValueError("shared expectation requires shared viewer")
    return subject, viewer, sharing


def _event_epoch(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a timestamp")
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be ISO-8601 or epoch") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        result = parsed.timestamp()
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field_name} must be positive")
    return result


@dataclass(frozen=True)
class OutcomeObservationV1:
    """Receipt-bearing binary outcome.  Narrative text is intentionally absent."""

    observation_id: str
    prediction_id: str
    value: bool
    observed_at: float
    evidence_refs: tuple[str, ...]
    source_kind: str
    subject_person_id: str
    viewer_scope: str
    shareability: str
    schema: str = "OutcomeObservationV1"
    version: int = 1

    def __post_init__(self) -> None:
        if not _SAFE_REF.fullmatch(self.observation_id):
            raise ValueError("outcome observation id is invalid")
        if not _SAFE_REF.fullmatch(self.prediction_id):
            raise ValueError("outcome prediction id is invalid")
        if type(self.value) is not bool:
            raise ValueError("outcome value must be boolean")
        _event_epoch(self.observed_at, "observed_at")
        if _refs(self.evidence_refs) != self.evidence_refs:
            raise ValueError("outcome evidence references are not canonical")
        if self.source_kind not in _OUTCOME_SOURCE_KINDS:
            raise ValueError("outcome source is not a trusted structured source")
        if _scope(
            self.subject_person_id, self.viewer_scope, self.shareability,
        ) != (self.subject_person_id, self.viewer_scope, self.shareability):
            raise ValueError("outcome scope is not canonical")
        if self.schema != "OutcomeObservationV1" or self.version != 1:
            raise ValueError("invalid outcome observation schema")

    @classmethod
    def create(
        cls, *, observation_id: str, prediction_id: str, value: bool,
        observed_at: Any, evidence_refs: Iterable[Any], source_kind: str,
        subject_person_id: str, viewer_scope: str, shareability: str,
    ) -> "OutcomeObservationV1":
        subject, viewer, sharing = _scope(
            subject_person_id, viewer_scope, shareability,
        )
        return cls(
            observation_id=str(observation_id or "").strip(),
            prediction_id=str(prediction_id or "").strip(),
            value=value,
            observed_at=_event_epoch(observed_at, "observed_at"),
            evidence_refs=_refs(evidence_refs),
            source_kind=str(source_kind or "").strip().lower(),
            subject_person_id=subject,
            viewer_scope=viewer,
            shareability=sharing,
        )

    @property
    def digest(self) -> str:
        return _digest({
            "schema": self.schema,
            "version": self.version,
            "observation_id": self.observation_id,
            "prediction_id": self.prediction_id,
            "value": self.value,
            "observed_at": self.observed_at,
            "evidence_refs": list(self.evidence_refs),
            "source_kind": self.source_kind,
            "subject_person_id": self.subject_person_id,
            "viewer_scope": self.viewer_scope,
            "shareability": self.shareability,
        })


class ExpectationStore:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS predictions (
                prediction_id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                domain TEXT NOT NULL,
                expectation TEXT NOT NULL,
                confidence REAL NOT NULL,
                horizon REAL NOT NULL,
                source TEXT,
                outcome TEXT DEFAULT 'pending',
                resolved_at REAL,
                detail TEXT,
                dedup_key TEXT,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pred_outcome_horizon
                ON predictions(outcome, horizon);
            CREATE INDEX IF NOT EXISTS idx_pred_domain ON predictions(domain);
            CREATE INDEX IF NOT EXISTS idx_pred_dedup ON predictions(dedup_key);
            CREATE TABLE IF NOT EXISTS expectation_outcome_observations (
                observation_id TEXT PRIMARY KEY,
                observation_digest TEXT NOT NULL,
                prediction_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                ingested_at REAL NOT NULL
            );
            """
        )
        self._migrate_v2()
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pred_scope ON predictions "
            "(subject_person_id,viewer_scope,outcome,created_at)",
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outcome_prediction ON "
            "expectation_outcome_observations(prediction_id)",
        )
        self._conn.commit()

    def _migrate_v2(self) -> None:
        """Additive/read-compatible migration; no V1 row is rewritten."""

        columns = {
            row["name"] for row in self._conn.execute(
                "PRAGMA table_info(predictions)",
            ).fetchall()
        }
        additions = {
            "schema_version": "INTEGER NOT NULL DEFAULT 1",
            "subject_person_id": "TEXT NOT NULL DEFAULT 'owner'",
            "viewer_scope": "TEXT NOT NULL DEFAULT 'owner'",
            "shareability": "TEXT NOT NULL DEFAULT 'owner_private'",
            "evidence_refs": "TEXT NOT NULL DEFAULT '[]'",
            "source_kind": "TEXT NOT NULL DEFAULT 'legacy'",
            "cohort": "TEXT NOT NULL DEFAULT 'legacy'",
            "outcome_observation_id": "TEXT",
            "outcome_observed_at": "REAL",
            "outcome_evidence_refs": "TEXT NOT NULL DEFAULT '[]'",
            "resolution_digest": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE predictions ADD COLUMN {name} {declaration}",
                )

    def _row(self, r: sqlite3.Row) -> Prediction:
        keys = set(r.keys())
        return Prediction(
            prediction_id=r["prediction_id"], subject=r["subject"],
            domain=r["domain"], expectation=r["expectation"],
            confidence=r["confidence"], horizon=r["horizon"],
            source=r["source"] or "", outcome=r["outcome"],
            resolved_at=r["resolved_at"],
            detail=json.loads(r["detail"] or "{}"), created_at=r["created_at"],
            schema_version=int(r["schema_version"]) if "schema_version" in keys else 1,
            subject_person_id=(r["subject_person_id"] if "subject_person_id" in keys else "owner"),
            viewer_scope=(r["viewer_scope"] if "viewer_scope" in keys else "owner"),
            shareability=(r["shareability"] if "shareability" in keys else "owner_private"),
            evidence_refs=tuple(json.loads(r["evidence_refs"] or "[]"))
            if "evidence_refs" in keys else (),
            source_kind=(r["source_kind"] if "source_kind" in keys else "legacy"),
            cohort=(r["cohort"] if "cohort" in keys else "legacy"),
            outcome_observation_id=(r["outcome_observation_id"] or "")
            if "outcome_observation_id" in keys else "",
            outcome_observed_at=(r["outcome_observed_at"]
                                 if "outcome_observed_at" in keys else None),
            outcome_evidence_refs=tuple(json.loads(
                r["outcome_evidence_refs"] or "[]",
            )) if "outcome_evidence_refs" in keys else (),
            resolution_digest=(r["resolution_digest"] or "")
            if "resolution_digest" in keys else "",
        )

    def create(self, *, subject: str, domain: str, expectation: str,
               confidence: float, horizon: float, source: str,
               dedup_key: str, detail: Optional[Dict[str, Any]] = None,
               schema_version: int = 1,
               subject_person_id: str = "owner",
               viewer_scope: str = "owner",
               shareability: str = "owner_private",
               evidence_refs: Iterable[Any] = (),
               source_kind: str = "legacy",
               cohort: str = "legacy",
               ) -> Optional[Prediction]:
        version = int(schema_version)
        if version not in {1, 2}:
            raise ValueError("unsupported expectation schema version")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("expectation confidence must be numeric")
        probability = float(confidence)
        if not math.isfinite(probability):
            raise ValueError("expectation confidence must be finite")
        if version == 2 and not 0.0 <= probability <= 1.0:
            raise ValueError("ExpectationV2 confidence must be between 0 and 1")
        if isinstance(horizon, bool) or not isinstance(horizon, (int, float)):
            raise ValueError("expectation horizon must be epoch seconds")
        horizon_value = float(horizon)
        if not math.isfinite(horizon_value) or horizon_value <= 0:
            raise ValueError("expectation horizon must be positive and finite")
        person, viewer, sharing = _scope(
            subject_person_id, viewer_scope, shareability,
        )
        refs = _refs(evidence_refs, required=version == 2)
        origin = str(source_kind or "").strip().lower()
        if version == 2 and origin not in _OUTCOME_SOURCE_KINDS:
            raise ValueError("ExpectationV2 source is not evidence-bearing")
        cohort_name = str(cohort or domain or "default").strip()[:100]
        if not cohort_name:
            raise ValueError("expectation cohort is required")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                # Dedup against a live prediction, AND against an already-
                # resolved one for the same horizon.  The write transaction
                # makes the check idempotent across sidecar connections.
                exists = self._conn.execute(
                    "SELECT 1 FROM predictions WHERE dedup_key=? AND "
                    "subject_person_id=? AND viewer_scope=? AND "
                    "(outcome='pending' OR horizon=?)",
                    (dedup_key, person, viewer, horizon_value),
                ).fetchone()
                if exists:
                    self._conn.commit()
                    return None
                pid = f"p-{uuid.uuid4().hex[:12]}"
                now = _now()
                self._conn.execute(
                    "INSERT INTO predictions (prediction_id,subject,domain,"
                    "expectation,confidence,horizon,source,detail,dedup_key,"
                    "created_at,schema_version,subject_person_id,viewer_scope,"
                    "shareability,evidence_refs,source_kind,cohort) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, subject, domain, expectation,
                     max(0.0, min(1.0, probability)), horizon_value, source,
                     json.dumps(detail or {}), dedup_key, now, version, person,
                     viewer, sharing, json.dumps(list(refs)), origin, cohort_name))
                r = self._conn.execute(
                    "SELECT * FROM predictions WHERE prediction_id=?",
                    (pid,),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self._row(r)

    def create_v2(
        self,
        *,
        subject: str,
        domain: str,
        expectation: str,
        confidence: float,
        horizon: float,
        source: str,
        dedup_key: str,
        evidence_refs: Iterable[Any],
        source_kind: str,
        subject_person_id: str,
        viewer_scope: str,
        shareability: str,
        cohort: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Optional[ExpectationV2]:
        return self.create(
            subject=subject,
            domain=domain,
            expectation=expectation,
            confidence=confidence,
            horizon=horizon,
            source=source,
            dedup_key=dedup_key,
            detail=detail,
            schema_version=2,
            subject_person_id=subject_person_id,
            viewer_scope=viewer_scope,
            shareability=shareability,
            evidence_refs=evidence_refs,
            source_kind=source_kind,
            cohort=cohort,
        )

    def due(self, now: Optional[float] = None) -> List[Prediction]:
        now = now or _now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM predictions WHERE outcome='pending' AND "
                "horizon <= ? ORDER BY horizon ASC LIMIT 200", (now,)).fetchall()
        return [self._row(r) for r in rows]

    def pending(self, limit: int = 100) -> List[Prediction]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM predictions WHERE outcome='pending' "
                "ORDER BY horizon ASC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def for_subjects(
        self, subjects: Iterable[str], *, limit: int = 100,
    ) -> List[Prediction]:
        """Return a stable bounded cohort, including already-resolved rows.

        Evidence reducers need resolved rows on crash replay so the same
        OutcomeObservation can be presented again and recognized as a
        duplicate instead of disappearing from the sink receipt.
        """

        normalized = tuple(sorted(dict.fromkeys(
            str(subject or "").strip() for subject in subjects
            if str(subject or "").strip()
        )))
        if not normalized:
            return []
        if len(normalized) > 20:
            raise ValueError("expectation subject cohort exceeds 20 entries")
        bound = max(1, min(500, int(limit)))
        placeholders = ",".join("?" for _ in normalized)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM predictions WHERE subject IN ({placeholders}) "
                "ORDER BY created_at ASC,prediction_id ASC LIMIT ?",
                (*normalized, bound),
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, prediction_id: str) -> Optional[Prediction]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM predictions WHERE prediction_id=?",
                (str(prediction_id or ""),),
            ).fetchone()
        return self._row(row) if row is not None else None

    def resolve(self, prediction_id: str, outcome: str) -> None:
        if outcome not in OUTCOMES:
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT schema_version FROM predictions WHERE prediction_id=?",
                (prediction_id,),
            ).fetchone()
            if (
                row is not None and int(row["schema_version"] or 1) == 2
                and outcome in {"hit", "miss"}
            ):
                raise ValueError(
                    "ExpectationV2 hit/miss requires an outcome observation",
                )
            self._conn.execute(
                "UPDATE predictions SET outcome=?, resolved_at=? "
                "WHERE prediction_id=? AND outcome='pending'",
                (outcome, _now(), prediction_id))
            self._conn.commit()

    def resolve_v2(
        self,
        observation: OutcomeObservationV1,
    ) -> Dict[str, Any]:
        """Ingest one outcome receipt and seal the first valid resolution.

        A positive event after the horizon is a miss.  This closes the legacy
        bug where a late commitment could look on-time merely because the
        checker ran after fulfillment.
        """

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                prediction_row = self._conn.execute(
                    "SELECT * FROM predictions WHERE prediction_id=?",
                    (observation.prediction_id,),
                ).fetchone()
                if prediction_row is None:
                    raise ValueError("outcome references an unknown expectation")
                prediction = self._row(prediction_row)
                if prediction.schema_version != 2:
                    raise ValueError(
                        "receipt-bound resolution requires ExpectationV2",
                    )
                if (
                    prediction.subject_person_id != observation.subject_person_id
                    or prediction.viewer_scope != observation.viewer_scope
                    or prediction.shareability != observation.shareability
                ):
                    raise ValueError(
                        "outcome scope does not match expectation scope",
                    )

                prior = self._conn.execute(
                    "SELECT observation_digest,prediction_id FROM "
                    "expectation_outcome_observations WHERE observation_id=?",
                    (observation.observation_id,),
                ).fetchone()
                if prior is not None:
                    if (
                        prior["observation_digest"] != observation.digest
                        or prior["prediction_id"] != observation.prediction_id
                    ):
                        raise ValueError("outcome observation replay conflict")
                    self._conn.commit()
                    return {
                        "disposition": "duplicate",
                        "prediction_id": observation.prediction_id,
                        "outcome": prediction.outcome,
                        "late": bool(
                            observation.value
                            and observation.observed_at > prediction.horizon
                        ),
                    }
                if prediction.outcome != "pending":
                    raise ValueError("expectation resolution is already sealed")

                late = bool(
                    observation.value
                    and observation.observed_at > prediction.horizon
                )
                outcome = "hit" if observation.value and not late else "miss"
                resolution = {
                    "prediction_id": prediction.prediction_id,
                    "observation_id": observation.observation_id,
                    "observation_digest": observation.digest,
                    "outcome": outcome,
                    "late": late,
                    "horizon": prediction.horizon,
                    "observed_at": observation.observed_at,
                }
                self._conn.execute(
                    "INSERT INTO expectation_outcome_observations "
                    "(observation_id,observation_digest,prediction_id,payload,"
                    "ingested_at) VALUES (?,?,?,?,?)",
                    (
                        observation.observation_id, observation.digest,
                        observation.prediction_id,
                        _canonical({
                            "schema": observation.schema,
                            "version": observation.version,
                            "observation_id": observation.observation_id,
                            "prediction_id": observation.prediction_id,
                            "value": observation.value,
                            "observed_at": observation.observed_at,
                            "evidence_refs": list(observation.evidence_refs),
                            "source_kind": observation.source_kind,
                            "subject_person_id": observation.subject_person_id,
                            "viewer_scope": observation.viewer_scope,
                            "shareability": observation.shareability,
                        }),
                        _now(),
                    ),
                )
                updated = self._conn.execute(
                    "UPDATE predictions SET outcome=?,resolved_at=?,"
                    "outcome_observation_id=?,outcome_observed_at=?,"
                    "outcome_evidence_refs=?,resolution_digest=? "
                    "WHERE prediction_id=? AND outcome='pending'",
                    (
                        outcome, _now(), observation.observation_id,
                        observation.observed_at,
                        json.dumps(list(observation.evidence_refs)),
                        _digest(resolution), prediction.prediction_id,
                    ),
                ).rowcount
                if updated != 1:
                    raise ValueError("expectation resolution lost first-writer race")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {
            "disposition": "resolved",
            "prediction_id": prediction.prediction_id,
            "outcome": outcome,
            "late": late,
            "resolution_digest": _digest(resolution),
        }

    def resolved_since(self, since: float,
                       domain: Optional[str] = None) -> List[Prediction]:
        q = ("SELECT * FROM predictions WHERE outcome IN ('hit','miss') "
             "AND resolved_at >= ?")
        params: List[Any] = [since]
        if domain:
            q += " AND domain=?"
            params.append(domain)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [self._row(r) for r in rows]

    def domains(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT domain FROM predictions WHERE "
                "outcome IN ('hit','miss')").fetchall()
        return [r["domain"] for r in rows]

    def projected(
        self,
        *,
        subject_person_id: str,
        viewer_scope: str,
        outcomes: Sequence[str] = ("pending", "hit", "miss", "unresolved"),
        limit: int = 100,
    ) -> List[Prediction]:
        """Strict observer projection; no cross-subject/scope fallback."""

        person = str(subject_person_id or "").strip()[:128]
        viewer = str(viewer_scope or "").strip()
        if len(viewer) > VIEWER_SCOPE_MAX_CHARS:
            raise ValueError("viewer scope exceeds the safe bound")
        allowed = tuple(item for item in outcomes if item in OUTCOMES)
        if not allowed:
            return []
        marks = ",".join("?" for _ in allowed)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM predictions WHERE subject_person_id=? AND "
                f"(viewer_scope=? OR shareability='public') AND outcome IN ({marks}) "
                "ORDER BY created_at DESC LIMIT ?",
                (person, viewer, *allowed, max(1, min(200, int(limit)))),
            ).fetchall()
        return [self._row(row) for row in rows]

    def resolved_projected_since(
        self,
        since: float,
        *,
        subject_person_id: str,
        viewer_scope: str,
    ) -> List[Prediction]:
        person = str(subject_person_id or "").strip()[:128]
        viewer = str(viewer_scope or "").strip()
        if len(viewer) > VIEWER_SCOPE_MAX_CHARS:
            raise ValueError("viewer scope exceeds the safe bound")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM predictions WHERE subject_person_id=? AND "
                "(viewer_scope=? OR shareability='public') AND "
                "outcome IN ('hit','miss') AND resolved_at>=? "
                "ORDER BY resolved_at ASC",
                (person, viewer, float(since)),
            ).fetchall()
        return [self._row(row) for row in rows]

    def projected_for_viewer(
        self,
        *,
        viewer_scope: str,
        outcomes: Sequence[str] = ("pending", "hit", "miss", "unresolved"),
        limit: int = 100,
    ) -> List[Prediction]:
        """All subjects explicitly addressed to one authenticated viewer."""

        viewer = str(viewer_scope or "").strip()
        if len(viewer) > VIEWER_SCOPE_MAX_CHARS:
            raise ValueError("viewer scope exceeds the safe bound")
        allowed = tuple(item for item in outcomes if item in OUTCOMES)
        if not allowed:
            return []
        marks = ",".join("?" for _ in allowed)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM predictions WHERE "
                f"(viewer_scope=? OR shareability='public') AND outcome IN ({marks}) "
                "ORDER BY created_at DESC LIMIT ?",
                (viewer, *allowed, max(1, min(200, int(limit)))),
            ).fetchall()
        return [self._row(row) for row in rows]

    def resolved_for_viewer_since(
        self, since: float, *, viewer_scope: str,
    ) -> List[Prediction]:
        viewer = str(viewer_scope or "").strip()
        if len(viewer) > VIEWER_SCOPE_MAX_CHARS:
            raise ValueError("viewer scope exceeds the safe bound")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM predictions WHERE "
                "(viewer_scope=? OR shareability='public') AND "
                "outcome IN ('hit','miss') AND resolved_at>=? "
                "ORDER BY resolved_at ASC",
                (viewer, float(since)),
            ).fetchall()
        return [self._row(row) for row in rows]


class ExpectationEngine:
    """Generate, check, and score predictions.

    Resolvers: `register_resolver(prefix, fn)` where fn(prediction) returns
    True (hit), False (miss), or None (cannot resolve yet -> left pending).
    """

    def __init__(self, store: ExpectationStore, *,
                 workspace: Any = None, journal: Any = None) -> None:
        self.store = store
        self._workspace = workspace
        self._journal = journal
        self._resolvers: Dict[
            str, Callable[[Prediction], Optional[Any]]
        ] = {}
        self.register_resolver("commitment:", self._resolve_commitment)

    def register_resolver(self, prefix: str,
                          fn: Callable[[Prediction], Optional[Any]]) -> None:
        self._resolvers[prefix] = fn

    def ingest_outcome(
        self, observation: OutcomeObservationV1,
    ) -> Dict[str, Any]:
        """Public idempotent path for a subsystem's structured outcome."""

        result = self.store.resolve_v2(observation)
        if result.get("disposition") == "resolved":
            prediction = self.store.get(observation.prediction_id)
            if prediction is not None:
                outcome = str(result["outcome"])
                self._log(
                    f"prediction {outcome}: {prediction.expectation}",
                    outcome=outcome,
                )
                if outcome == "miss":
                    self._surprise(prediction)
        return result

    # -- generation -------------------------------------------------------
    def generate_from_commitments(self) -> int:
        """A commitment with a due date -> a prediction it gets fulfilled by
        then. Confidence from the agent's commitment track record."""
        cstore = self._commitments()
        if cstore is None:
            return 0
        try:
            pend = cstore.list(status=["pending"], limit=100).get(
                "commitments", [])
        except Exception:
            return 0
        conf = self._commitment_confidence()
        n = 0
        for c in pend:
            due = c.get("due_at")
            cid = c.get("id")
            if not due or not cid:
                continue
            try:
                horizon = datetime.fromisoformat(
                    str(due).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if horizon <= _now():
                # the due date already passed: there is nothing left to
                # predict, and an instantly-due prediction would just be
                # scored the moment it is created
                continue
            desc = (c.get("description") or "commitment")[:120]
            p = self.store.create_v2(
                subject=f"commitment:{cid}", domain="commitment",
                expectation=f"'{desc}' fulfilled by its due date",
                confidence=conf, horizon=horizon, source="cadence-model",
                dedup_key=f"commitment:{cid}",
                evidence_refs=(f"commitment:{cid}",),
                source_kind="commitment_receipt",
                subject_person_id=str(c.get("person_id") or "owner")[:128],
                viewer_scope="owner",
                shareability="owner_private",
                cohort="commitment:due-date",
                detail={"commitment_id": cid, "due_at": str(due)[:64]})
            if p is not None:
                n += 1
        return n

    def _generate_structured(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        kind: str,
        id_field: str,
        horizon_field: str,
        domain: str,
        expectation_template: str,
        source_kind: str,
        maximum: int = 100,
    ) -> int:
        """Bounded adapter for already-structured subsystem records.

        Callers must supply ``evidence_refs`` and a concrete horizon.  Text is
        display-only; it never resolves the prediction.
        """

        count = 0
        bound = max(0, min(100, int(maximum)))
        for record in records[:bound]:
            native_id = str(record.get(id_field) or "").strip()
            if not native_id or not _SAFE_REF.fullmatch(native_id):
                continue
            try:
                horizon = _event_epoch(record.get(horizon_field), horizon_field)
                if horizon <= _now():
                    continue
                evidence = _refs(record.get("evidence_refs") or ())
                confidence = float(record.get("confidence", 0.65))
                subject_person_id = str(
                    record.get("subject_person_id")
                    or record.get("person_id") or "owner"
                )[:128]
                viewer = str(record.get("viewer_scope") or "owner")
                sharing = str(record.get("shareability") or "owner_private")
                label = str(record.get("label") or native_id)[:120]
                prediction = self.store.create_v2(
                    subject=f"{kind}:{native_id}",
                    domain=domain,
                    expectation=expectation_template.format(label=label),
                    confidence=confidence,
                    horizon=horizon,
                    source=f"{kind}-adapter-v2",
                    dedup_key=f"{kind}:{native_id}",
                    evidence_refs=evidence,
                    source_kind=source_kind,
                    subject_person_id=subject_person_id,
                    viewer_scope=viewer,
                    shareability=sharing,
                    cohort=f"{domain}:{kind}",
                    detail={
                        f"{kind}_id": native_id,
                        "generator": f"{kind}-adapter-v2",
                    },
                )
            except (TypeError, ValueError):
                continue
            if prediction is not None:
                count += 1
        return count

    def generate_contact_cadence(
        self, records: Sequence[Mapping[str, Any]], *, maximum: int = 100,
    ) -> int:
        return self._generate_structured(
            records, kind="contact", id_field="contact_id",
            horizon_field="next_contact_due_at", domain="contact_cadence",
            expectation_template="contact cadence for {label} is satisfied",
            source_kind="relationship_receipt", maximum=maximum,
        )

    def generate_task_duration(
        self, records: Sequence[Mapping[str, Any]], *, maximum: int = 100,
    ) -> int:
        return self._generate_structured(
            records, kind="task", id_field="task_id",
            horizon_field="expected_complete_at", domain="task_duration",
            expectation_template="task {label} completes within its estimate",
            source_kind="task_receipt", maximum=maximum,
        )

    def generate_service_recovery(
        self, records: Sequence[Mapping[str, Any]], *, maximum: int = 100,
    ) -> int:
        return self._generate_structured(
            records, kind="service", id_field="service_id",
            horizon_field="expected_recovery_at", domain="service_recovery",
            expectation_template="service {label} recovers by its horizon",
            source_kind="service_probe", maximum=maximum,
        )

    def generate_relationship_followup(
        self, records: Sequence[Mapping[str, Any]], *, maximum: int = 100,
    ) -> int:
        return self._generate_structured(
            records, kind="relationship", id_field="relationship_id",
            horizon_field="followup_due_at", domain="relationship_followup",
            expectation_template="relationship follow-up for {label} occurs",
            source_kind="relationship_receipt", maximum=maximum,
        )

    def _commitment_confidence(self) -> float:
        """Historical fulfillment rate as the prior; defaults to 0.7."""
        cstore = self._commitments()
        if cstore is None:
            return 0.7
        try:
            done = cstore.list(status=["fulfilled"], limit=200).get("total", 0)
            missed = cstore.list(status=["overdue"], limit=200).get("total", 0)
            if done + missed >= 5:
                return max(0.1, min(0.95, (done + 1) / (done + missed + 2)))
        except Exception:
            pass
        return 0.7

    # -- checking ---------------------------------------------------------
    def check(self, now: Optional[float] = None) -> Dict[str, int]:
        """Resolve every due prediction. Misses emit surprise. Returns
        {"hit": n, "miss": n, "unresolved": n}."""
        counts = {"hit": 0, "miss": 0, "unresolved": 0}
        point = float(now) if now is not None else _now()
        for p in self.store.due(now=point):
            verdict = self._resolve(p)
            if verdict is None:
                # give it one grace period then mark unresolved so it stops
                # recurring; unresolved is excluded from calibration
                if point - p.horizon > 86400:
                    self.store.resolve(p.prediction_id, "unresolved")
                    counts["unresolved"] += 1
                continue
            if isinstance(verdict, OutcomeObservationV1):
                try:
                    result = self.store.resolve_v2(verdict)
                except ValueError:
                    logger.debug(
                        "receipt-bound expectation resolution rejected",
                        exc_info=True,
                    )
                    continue
                outcome = str(result["outcome"])
            elif p.schema_version == 1 and isinstance(verdict, bool):
                # Compatibility adapter for registered legacy resolvers.  A
                # bare boolean is never sufficient to settle ExpectationV2.
                outcome = "hit" if verdict else "miss"
                self.store.resolve(p.prediction_id, outcome)
            else:
                continue
            counts[outcome] += 1
            self._log(f"prediction {outcome}: {p.expectation}",
                      outcome=outcome)
            if outcome == "miss":
                self._surprise(p)
        return counts

    def _resolve(self, p: Prediction) -> Optional[Any]:
        for prefix, fn in self._resolvers.items():
            if p.subject.startswith(prefix):
                try:
                    return fn(p)
                except Exception:
                    logger.debug("resolver %s failed", prefix, exc_info=True)
                    return None
        return None

    def _resolve_commitment(self, p: Prediction) -> Optional[Any]:
        cstore = self._commitments()
        cid = p.detail.get("commitment_id")
        if cstore is None or not cid:
            return None
        c = cstore.get(cid)
        if c is None:
            return None
        status = c.get("status")
        if p.schema_version == 2:
            if status == "fulfilled":
                observed = c.get("fulfilled_at")
                if not observed:
                    resolution = (c.get("metadata") or {}).get("resolution") or {}
                    observed = resolution.get("at")
                if not observed:
                    return None
                value = True
            elif status in ("overdue", "cancelled"):
                resolution = (c.get("metadata") or {}).get("resolution") or {}
                observed = resolution.get("at") or max(_now(), p.horizon)
                value = False
            elif status == "pending":
                # The structured store still says pending at/after the due
                # horizon.  That is explicit negative state, not prose.
                observed = max(_now(), p.horizon)
                value = False
            else:
                return None
            evidence = (f"commitment:{cid}",)
            identity = _digest({
                "prediction_id": p.prediction_id,
                "status": status,
                "observed_at": str(observed),
                "evidence_refs": evidence,
            })
            return OutcomeObservationV1.create(
                observation_id=f"eo-{identity[:24]}",
                prediction_id=p.prediction_id,
                value=value,
                observed_at=observed,
                evidence_refs=evidence,
                source_kind="commitment_receipt",
                subject_person_id=p.subject_person_id,
                viewer_scope=p.viewer_scope,
                shareability=p.shareability,
            )
        if status == "fulfilled":
            return True
        if status in ("overdue", "cancelled"):
            return False
        # still pending past its due date -> missed
        return False

    def _surprise(self, p: Prediction) -> None:
        ws = self._workspace
        if ws is None:
            return
        try:
            # keyed by SUBJECT, not prediction id: repeated misses about the
            # same thing merge into one strengthening concern instead of
            # spawning a fresh anomaly per scoring pass
            ws.bump(kind="anomaly",
                    summary=f"surprise: expected {p.expectation} (conf "
                            f"{p.confidence:.2f}) but it did not hold",
                    dedup_key=f"surprise:{p.subject}",
                    salience=min(0.9, 0.5 + p.confidence),
                    sources=[p.subject])
        except Exception:
            logger.debug("surprise -> workspace failed", exc_info=True)

    # -- calibration ------------------------------------------------------
    def calibration(self, domain: Optional[str] = None, *,
                    since: Optional[float] = None) -> Dict[str, Any]:
        """Brier score per domain over resolved predictions. Lower is better
        (0 = perfect, 0.25 = a coin flip at p=0.5). Feeds the benchmark."""
        since = since if since is not None else 0.0
        domains = [domain] if domain else self.store.domains()
        out: Dict[str, Any] = {}
        for d in domains:
            resolved = self.store.resolved_since(since, domain=d)
            if not resolved:
                continue
            brier = sum((p.confidence - (1.0 if p.outcome == "hit" else 0.0)) ** 2
                        for p in resolved) / len(resolved)
            hits = sum(1 for p in resolved if p.outcome == "hit")
            out[d] = {"brier": round(brier, 4), "n": len(resolved),
                      "hit_rate": round(hits / len(resolved), 4),
                      "scoring_rule": "brier",
                      "formula": "mean((confidence - observed_binary)^2)"}
        return out

    @staticmethod
    def _confidence_bin(confidence: float) -> str:
        low = min(9, max(0, int(float(confidence) * 10))) / 10.0
        return f"{low:.1f}-{low + 0.1:.1f}"

    @staticmethod
    def _horizon_bucket(prediction: Prediction) -> str:
        seconds = max(0.0, prediction.horizon - prediction.created_at)
        if seconds <= 3600:
            return "lte_1h"
        if seconds <= 86400:
            return "1h_1d"
        if seconds <= 604800:
            return "1d_7d"
        return "gt_7d"

    def calibration_report(
        self,
        *,
        since: Optional[float] = None,
        subject_person_id: Optional[str] = None,
        viewer_scope: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Transparent proper-score report by declared and derived cohorts."""

        start = since if since is not None else 0.0
        if subject_person_id is not None or viewer_scope is not None:
            if not subject_person_id or not viewer_scope:
                raise ValueError("scoped calibration requires subject and viewer")
            rows = self.store.resolved_projected_since(
                start,
                subject_person_id=subject_person_id,
                viewer_scope=viewer_scope,
            )
        else:
            rows = []
            for domain in self.store.domains():
                rows.extend(self.store.resolved_since(start, domain=domain))
        return self._calibration_report_for_rows(rows)

    def _calibration_report_for_rows(
        self, rows: Sequence[Prediction],
    ) -> Dict[str, Any]:
        groups: Dict[tuple[str, str, str, str], list[Prediction]] = {}
        for prediction in rows:
            key = (
                prediction.domain,
                prediction.cohort or "legacy",
                self._confidence_bin(prediction.confidence),
                self._horizon_bucket(prediction),
            )
            groups.setdefault(key, []).append(prediction)
        cohorts: list[Dict[str, Any]] = []
        for key in sorted(groups):
            values = groups[key]
            errors = [
                (item.confidence - (1.0 if item.outcome == "hit" else 0.0)) ** 2
                for item in values
            ]
            cohorts.append({
                "domain": key[0],
                "cohort": key[1],
                "confidence_bin": key[2],
                "horizon_bucket": key[3],
                "n": len(values),
                "brier": round(sum(errors) / len(errors), 4),
                "hit_rate": round(
                    sum(1 for item in values if item.outcome == "hit") / len(values),
                    4,
                ),
            })
        domains: Dict[str, Any] = {}
        by_domain: Dict[str, list[Prediction]] = {}
        for item in rows:
            by_domain.setdefault(item.domain, []).append(item)
        for name, values in sorted(by_domain.items()):
            errors = [
                (item.confidence - (1.0 if item.outcome == "hit" else 0.0)) ** 2
                for item in values
            ]
            domains[name] = {
                "brier": round(sum(errors) / len(errors), 4),
                "n": len(values),
                "hit_rate": round(
                    sum(1 for item in values if item.outcome == "hit") / len(values),
                    4,
                ),
                "scoring_rule": "brier",
                "formula": "mean((confidence - observed_binary)^2)",
            }
        return {
            "scoring_rule": "brier",
            "proper": True,
            "lower_is_better": True,
            "formula": "mean((forecast_probability - observed_binary)^2)",
            "resolved_n": len(rows),
            "domains": domains,
            "cohorts": cohorts,
        }

    # -- wiring -----------------------------------------------------------
    def _commitments(self) -> Any:
        try:
            from colony_sidecar.api.routers import host
            return getattr(host, "_commitment_store", None)
        except Exception:
            return None

    def _log(self, desc: str, *, outcome: str) -> None:
        if self._journal is None:
            return
        try:
            self._journal.record("expectation", desc, decision="noted",
                                  outcome=outcome)
        except Exception:
            logger.debug("expectation journal write failed", exc_info=True)

    def observer_projection(
        self,
        *,
        subject_person_id: str,
        viewer_scope: str,
        limit: int = 50,
    ) -> Dict[str, Any]:
        records = self.store.projected(
            subject_person_id=subject_person_id,
            viewer_scope=viewer_scope,
            limit=limit,
        )
        return {
            "mode": expectations_mode(),
            "subject_person_id": subject_person_id,
            "viewer_scope": viewer_scope,
            "pending": [p.public() for p in records if p.outcome == "pending"],
            "resolved": [p.public() for p in records if p.outcome != "pending"],
            "calibration": self.calibration_report(
                subject_person_id=subject_person_id,
                viewer_scope=viewer_scope,
            ),
        }

    def viewer_projection(
        self, *, viewer_scope: str, limit: int = 50,
    ) -> Dict[str, Any]:
        """Compatibility projection for an authenticated owner/deck viewer."""

        records = self.store.projected_for_viewer(
            viewer_scope=viewer_scope, limit=limit,
        )
        start = 0.0
        resolved = self.store.resolved_for_viewer_since(
            start, viewer_scope=viewer_scope,
        )
        calibration = self._calibration_report_for_rows(resolved)
        return {
            "mode": expectations_mode(),
            "viewer_scope": viewer_scope,
            "pending": [p.public() for p in records if p.outcome == "pending"],
            "resolved": [p.public() for p in records if p.outcome != "pending"],
            "calibration": calibration,
        }

    def snapshot(
        self,
        limit: int = 50,
        *,
        subject_person_id: Optional[str] = None,
        viewer_scope: str = "owner",
    ) -> Dict[str, Any]:
        # Legacy endpoint shape retained while its read is now scope-filtered.
        projection = (
            self.observer_projection(
                subject_person_id=subject_person_id,
                viewer_scope=viewer_scope,
                limit=limit,
            )
            if subject_person_id is not None
            else self.viewer_projection(viewer_scope=viewer_scope, limit=limit)
        )
        return {
            "mode": projection["mode"],
            "pending": projection["pending"],
            "calibration": projection["calibration"]["domains"],
            "calibration_report": projection["calibration"],
        }
