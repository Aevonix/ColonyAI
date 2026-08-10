"""Reference-only persistence and coverage for P8 recipient simulation.

Coverage is event based.  A selected outbound item is first registered as a
``sample``; a later ``evaluation`` event references the same outbound item.
That makes a missing simulation visible instead of deriving a misleading
success rate solely from results that happened to be written.

Rows retain stable references, action/risk codes, and SHA-256 digests only.
Draft/fact content, fact and arc references, dependency errors, credentials,
and host topology are intentionally not represented by this schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterable, Optional

from colony_sidecar.tom.recipient_simulator import (
    FAIL_BEHAVIOR_BY_RISK,
    REALTIME_VOICE_SURFACES,
    RecipientSimulationRequestV1,
    RecipientSimulationResultV1,
    is_realtime_voice_surface,
    recipient_simulator_mode,
)
from colony_sidecar.tom.visibility import ViewerContextV1, content_digest


SCHEMA_VERSION = 1
EVENT_KINDS = frozenset({"sample", "evaluation"})
MAX_AUDIT_CODES = 64
MAX_PROJECTED_EVENTS = 256
MAX_MISSING_REFS = 64
COVERAGE_FETCH_SIZE = 512

AUDIT_RISK_CODES = frozenset({
    "arc_projection_incomplete",
    "fact_ref_not_recipient_authorized",
    "high_salience_provenance_unknown",
    "recipient_identity_unattested",
    "simulation_dependency_error",
    "stress_topic_pressure",
})
AUDIT_REPAIR_CODES = frozenset({
    "add_fact_provenance",
    "review_arc_context",
    "soften_pressure_language",
})
AUDIT_ACTIONS = frozenset({
    "hold", "no_effect", "observe", "observe_async", "observe_only",
    "repair", "review", "send",
})
AUDIT_EVALUATION_PATHS = frozenset({
    "async_observation", "disabled", "pre_send_advisory",
    "shadow_observation",
})
AUDIT_SURFACES = REALTIME_VOICE_SURFACES.union({
    "api", "assistant", "chat", "deck", "discord", "email",
    "facetime", "google_chat", "imessage", "matrix", "mms",
    "operator_deck", "rcs", "signal", "slack", "sms", "teams",
    "telegram", "text", "web", "whatsapp", "whatsapp_call",
})

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,191}$")
_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")


class RecipientAuditConflictError(ValueError):
    """An audit identity/outbound reference changed immutable content."""


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _ref(value: Any, *, field: str, allow_empty: bool = False) -> str:
    normalized = str(value or "").strip()
    if allow_empty and not normalized:
        return ""
    if not _REF_RE.fullmatch(normalized):
        raise ValueError(f"{field} is not a bounded opaque reference")
    return normalized


def _sha(value: Any, *, field: str, allow_empty: bool = False) -> str:
    normalized = str(value or "").strip().lower()
    if allow_empty and not normalized:
        return ""
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return normalized


def _refs(values: Iterable[Any], *, field: str) -> tuple[str, ...]:
    normalized = tuple(sorted(dict.fromkeys(
        _ref(value, field=field) for value in values
    )))
    if len(normalized) > MAX_AUDIT_CODES:
        raise ValueError(f"{field} exceeds bounded reference limit")
    return normalized


def _iso(value: datetime | str, *, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class RecipientAuditEventV1:
    event_id: str
    idempotency_key: str
    event_kind: str
    outbound_item_ref: str
    recipient_person_id: str
    scope_revision: str
    high_salience: bool
    occurred_at: str
    simulation_ref: str = ""
    request_digest: str = ""
    result_digest: str = ""
    draft_digest: str = ""
    surface: str = ""
    risk_class: str = ""
    evaluated: bool = False
    would_action: str = ""
    effective_action: str = ""
    evaluation_path: str = ""
    risk_codes: tuple[str, ...] = ()
    repair_codes: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported recipient audit version")
        event_id = _ref(self.event_id, field="event_id")
        idempotency_key = _ref(
            self.idempotency_key, field="idempotency_key")
        event_kind = str(self.event_kind or "").strip().lower()
        if event_kind not in EVENT_KINDS:
            raise ValueError("unknown recipient audit event kind")
        outbound = _ref(self.outbound_item_ref, field="outbound_item_ref")
        recipient = _ref(
            self.recipient_person_id, field="recipient_person_id")
        revision = _ref(self.scope_revision, field="scope_revision")
        occurred_at = _iso(self.occurred_at, field="occurred_at")
        simulation = _ref(
            self.simulation_ref, field="simulation_ref", allow_empty=True)
        request_digest = _sha(
            self.request_digest, field="request_digest", allow_empty=True)
        result_digest = _sha(
            self.result_digest, field="result_digest", allow_empty=True)
        draft_digest = _sha(self.draft_digest, field="draft_digest")
        surface = _ref(self.surface, field="surface", allow_empty=True).lower()
        risk_class = _ref(
            self.risk_class, field="risk_class", allow_empty=True).lower()
        would = _ref(
            self.would_action, field="would_action", allow_empty=True).lower()
        effective = _ref(
            self.effective_action,
            field="effective_action",
            allow_empty=True,
        ).lower()
        path = _ref(
            self.evaluation_path,
            field="evaluation_path",
            allow_empty=True,
        ).lower()
        risk_codes = _refs(self.risk_codes, field="risk_code")
        repair_codes = _refs(self.repair_codes, field="repair_code")

        evaluation_values = (
            simulation, request_digest, result_digest, surface,
            risk_class, would, effective, path,
        )
        if event_kind == "sample":
            if any(evaluation_values) or self.evaluated \
                    or risk_codes or repair_codes:
                raise ValueError("sample events cannot contain evaluation data")
        else:
            if not all(evaluation_values):
                raise ValueError(
                    "evaluation events require complete digest metadata")
            if risk_class not in {"low", "medium", "high", "critical"}:
                raise ValueError("evaluation risk class is not auditable")
            if surface not in AUDIT_SURFACES:
                raise ValueError("evaluation surface is not auditable")
            if would not in AUDIT_ACTIONS or effective not in AUDIT_ACTIONS:
                raise ValueError("evaluation action is not auditable")
            if path not in AUDIT_EVALUATION_PATHS:
                raise ValueError("evaluation path is not auditable")
            for code in risk_codes:
                severity, separator, name = code.partition(":")
                if separator != ":" \
                        or severity not in {"low", "medium", "high", "critical"} \
                        or name not in AUDIT_RISK_CODES:
                    raise ValueError("evaluation risk code is not auditable")
            for code in repair_codes:
                priority, separator, name = code.partition(":")
                if separator != ":" \
                        or priority not in {"low", "medium", "high", "critical"} \
                        or name not in AUDIT_REPAIR_CODES:
                    raise ValueError("evaluation repair code is not auditable")

        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "event_kind", event_kind)
        object.__setattr__(self, "outbound_item_ref", outbound)
        object.__setattr__(self, "recipient_person_id", recipient)
        object.__setattr__(self, "scope_revision", revision)
        object.__setattr__(self, "high_salience", bool(self.high_salience))
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "simulation_ref", simulation)
        object.__setattr__(self, "request_digest", request_digest)
        object.__setattr__(self, "result_digest", result_digest)
        object.__setattr__(self, "draft_digest", draft_digest)
        object.__setattr__(self, "surface", surface)
        object.__setattr__(self, "risk_class", risk_class)
        object.__setattr__(self, "evaluated", bool(self.evaluated))
        object.__setattr__(self, "would_action", would)
        object.__setattr__(self, "effective_action", effective)
        object.__setattr__(self, "evaluation_path", path)
        object.__setattr__(self, "risk_codes", risk_codes)
        object.__setattr__(self, "repair_codes", repair_codes)

    def public(self) -> dict[str, Any]:
        """Return stable references/codes/digests and no message content."""

        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "idempotency_key": self.idempotency_key,
            "event_kind": self.event_kind,
            "outbound_item_ref": self.outbound_item_ref,
            "recipient_person_id": self.recipient_person_id,
            "scope_revision": self.scope_revision,
            "high_salience": self.high_salience,
            "occurred_at": self.occurred_at,
            "draft_digest": self.draft_digest,
            "evaluated": self.evaluated,
        }
        if self.event_kind == "evaluation":
            value.update({
                "simulation_ref": self.simulation_ref,
                "request_digest": self.request_digest,
                "result_digest": self.result_digest,
                "draft_digest": self.draft_digest,
                "surface": self.surface,
                "risk_class": self.risk_class,
                "would_action": self.would_action,
                "effective_action": self.effective_action,
                "evaluation_path": self.evaluation_path,
                "risk_codes": list(self.risk_codes),
                "repair_codes": list(self.repair_codes),
            })
        return value

    @property
    def audit_digest(self) -> str:
        return _digest(self.public())


def _attested_recipient(recipient: ViewerContextV1) -> tuple[str, str]:
    if not isinstance(recipient, ViewerContextV1) \
            or not recipient.attested \
            or not recipient.viewer_person_id \
            or not recipient.scope_revision:
        raise ValueError("recipient audit requires an attested recipient")
    return recipient.viewer_person_id, recipient.scope_revision


def sample_event(
    *,
    event_id: str,
    idempotency_key: str,
    outbound_item_ref: str,
    recipient: ViewerContextV1,
    high_salience: bool,
    draft_text: str,
    sampled_at: datetime | str,
) -> RecipientAuditEventV1:
    """Build a reference-only selection marker before simulation runs."""

    person, revision = _attested_recipient(recipient)
    return RecipientAuditEventV1(
        event_id=event_id,
        idempotency_key=idempotency_key,
        event_kind="sample",
        outbound_item_ref=outbound_item_ref,
        recipient_person_id=person,
        scope_revision=revision,
        high_salience=high_salience,
        occurred_at=_iso(sampled_at, field="sampled_at"),
        draft_digest=content_digest(draft_text),
    )


def evaluation_event_from_result(
    *,
    event_id: str,
    idempotency_key: str,
    outbound_item_ref: str,
    request: RecipientSimulationRequestV1,
    result: RecipientSimulationResultV1,
    evaluated_at: datetime | str,
) -> RecipientAuditEventV1:
    """Reduce a simulation into digest/code evidence without private refs."""

    if not isinstance(request, RecipientSimulationRequestV1):
        raise ValueError("request must be RecipientSimulationRequestV1")
    if not isinstance(result, RecipientSimulationResultV1):
        raise ValueError("result must be RecipientSimulationResultV1")
    result_payload = result.public()
    result_payload.pop("audit_digest", None)
    if result.audit_digest != _digest(result_payload):
        raise ValueError("simulation result audit digest is invalid")
    if result.request_digest != request.audit_digest:
        raise ValueError("simulation result request digest does not match request")
    if result.simulation_id != request.simulation_id:
        raise ValueError("simulation result reference does not match request")
    if result.risk_class != request.risk_class:
        raise ValueError("simulation result risk class does not match request")
    if result.external_effect or result.authority_granted \
            or result.synchronous_gate:
        raise ValueError("simulation audit cannot record granted authority/effect")
    if request.surface not in AUDIT_SURFACES:
        raise ValueError("simulation surface is not auditable")
    if result.mode not in {"off", "shadow", "live"}:
        raise ValueError("simulation result mode is not auditable")
    if result.fail_behavior != FAIL_BEHAVIOR_BY_RISK[request.risk_class]:
        raise ValueError("simulation fail behavior does not match risk class")
    if result.would_recommend not in AUDIT_ACTIONS \
            or result.recommended_action not in AUDIT_ACTIONS:
        raise ValueError("simulation action is not auditable")
    if result.evaluation_path not in AUDIT_EVALUATION_PATHS:
        raise ValueError("simulation evaluation path is not auditable")
    unknown_risks = sorted(
        risk.code for risk in result.risks if risk.code not in AUDIT_RISK_CODES)
    if unknown_risks:
        raise ValueError(
            f"simulation risk code is not auditable: {unknown_risks[0]}")
    unknown_repairs = sorted(
        repair.code for repair in result.repairs
        if repair.code not in AUDIT_REPAIR_CODES)
    if unknown_repairs:
        raise ValueError(
            f"simulation repair code is not auditable: {unknown_repairs[0]}")
    severity_rank = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    if any(risk.code == "simulation_dependency_error" for risk in result.risks):
        expected_would = result.fail_behavior
    else:
        rank = max(
            (severity_rank[risk.severity] for risk in result.risks),
            default=0,
        )
        if rank >= 3:
            expected_would = "hold"
        elif rank == 2:
            expected_would = "repair" if result.repairs else "review"
        elif rank == 1:
            expected_would = "observe"
        else:
            expected_would = "send"
    if not result.evaluated:
        expected_would = "no_effect"
        expected_action = "no_effect"
        expected_path = "disabled"
        if result.mode != "off" or result.risks or result.repairs \
                or result.authorized_fact_refs or result.active_arc_refs \
                or result.fact_projection_digest or result.arc_projection_digest:
            raise ValueError("disabled simulation result has impossible state")
    elif result.mode == "off":
        raise ValueError("evaluated simulation result cannot use off mode")
    elif is_realtime_voice_surface(request.surface):
        expected_action = "observe_async"
        expected_path = "async_observation"
    elif result.mode == "shadow":
        expected_action = "observe_only"
        expected_path = "shadow_observation"
    else:
        expected_action = expected_would
        expected_path = "pre_send_advisory"
    if result.would_recommend != expected_would \
            or result.recommended_action != expected_action \
            or result.evaluation_path != expected_path:
        raise ValueError("simulation result state matrix is inconsistent")
    person, revision = _attested_recipient(request.recipient)
    risks = tuple(
        f"{risk.severity}:{risk.code}" for risk in result.risks)
    repairs = tuple(
        f"{repair.priority}:{repair.code}" for repair in result.repairs)
    return RecipientAuditEventV1(
        event_id=event_id,
        idempotency_key=idempotency_key,
        event_kind="evaluation",
        outbound_item_ref=outbound_item_ref,
        recipient_person_id=person,
        scope_revision=revision,
        high_salience=request.high_salience,
        occurred_at=_iso(evaluated_at, field="evaluated_at"),
        simulation_ref=request.simulation_id,
        request_digest=request.audit_digest,
        result_digest=result.audit_digest,
        draft_digest=content_digest(request.draft_text),
        surface=request.surface,
        risk_class=request.risk_class,
        evaluated=result.evaluated,
        would_action=result.would_recommend,
        effective_action=result.recommended_action,
        evaluation_path=result.evaluation_path,
        risk_codes=risks,
        repair_codes=repairs,
    )


@dataclass(frozen=True, slots=True)
class RecipientAuditAppendV1:
    event: RecipientAuditEventV1
    sequence: int
    appended: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class RecipientAuditProjectionV1:
    events: tuple[RecipientAuditEventV1, ...]
    viewer_attested: bool
    corrupt_count: int
    truncated: bool
    viewer_digest: str
    audit_digest: str
    schema_version: int = SCHEMA_VERSION

    def public(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "events": [event.public() for event in self.events],
            "viewer_attested": self.viewer_attested,
            "corrupt_count": self.corrupt_count,
            "truncated": self.truncated,
            "viewer_digest": self.viewer_digest,
            "audit_digest": self.audit_digest,
        }


@dataclass(frozen=True, slots=True)
class RecipientAuditCoverageV1:
    sampled_high_salience: int
    evaluated_high_salience: int
    unevaluated_high_salience: int
    missing_item_refs: tuple[str, ...]
    missing_refs_truncated: bool
    corrupt_count: int
    scan_truncated: bool
    status: str
    coverage_complete: bool
    viewer_digest: str
    audit_digest: str
    schema_version: int = SCHEMA_VERSION

    def public(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sampled_high_salience": self.sampled_high_salience,
            "evaluated_high_salience": self.evaluated_high_salience,
            "unevaluated_high_salience": self.unevaluated_high_salience,
            "missing_item_refs": list(self.missing_item_refs),
            "missing_refs_truncated": self.missing_refs_truncated,
            "corrupt_count": self.corrupt_count,
            "scan_truncated": self.scan_truncated,
            "status": self.status,
            "coverage_complete": self.coverage_complete,
            "viewer_digest": self.viewer_digest,
            "audit_digest": self.audit_digest,
        }


class RecipientSimulationAuditStore:
    """Append-only recipient-simulation evidence and coverage ledger."""

    def __init__(self, db_path: str | os.PathLike[str] = ":memory:") -> None:
        self.path = str(db_path)
        if self.path != ":memory:":
            path = Path(self.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.touch(mode=0o600)
            else:
                os.chmod(path, 0o600)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA recursive_triggers=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS recipient_simulation_audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                idempotency_key TEXT NOT NULL UNIQUE,
                event_kind TEXT NOT NULL CHECK (
                    event_kind IN ('sample','evaluation')),
                outbound_item_ref TEXT NOT NULL,
                recipient_person_id TEXT NOT NULL,
                scope_revision TEXT NOT NULL,
                high_salience INTEGER NOT NULL CHECK (high_salience IN (0,1)),
                occurred_at TEXT NOT NULL,
                simulation_ref TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                result_digest TEXT NOT NULL,
                draft_digest TEXT NOT NULL,
                surface TEXT NOT NULL,
                risk_class TEXT NOT NULL,
                evaluated INTEGER NOT NULL CHECK (evaluated IN (0,1)),
                would_action TEXT NOT NULL,
                effective_action TEXT NOT NULL,
                evaluation_path TEXT NOT NULL,
                risk_codes_json TEXT NOT NULL,
                repair_codes_json TEXT NOT NULL,
                event_digest TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                stored_at TEXT NOT NULL,
                UNIQUE(event_kind,outbound_item_ref,simulation_ref)
            );
            CREATE INDEX IF NOT EXISTS idx_recipient_audit_viewer_time
                ON recipient_simulation_audit(
                    recipient_person_id,scope_revision,occurred_at,seq);
            CREATE INDEX IF NOT EXISTS idx_recipient_audit_coverage
                ON recipient_simulation_audit(
                    event_kind,high_salience,recipient_person_id,outbound_item_ref);
            CREATE INDEX IF NOT EXISTS idx_recipient_audit_outbound
                ON recipient_simulation_audit(outbound_item_ref,event_kind);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_recipient_audit_simulation_once
                ON recipient_simulation_audit(simulation_ref)
                WHERE event_kind='evaluation';
            CREATE TRIGGER IF NOT EXISTS recipient_audit_no_update
                BEFORE UPDATE ON recipient_simulation_audit BEGIN
                    SELECT RAISE(ABORT, 'recipient audit is append-only');
                END;
            CREATE TRIGGER IF NOT EXISTS recipient_audit_no_delete
                BEFORE DELETE ON recipient_simulation_audit BEGIN
                    SELECT RAISE(ABORT, 'recipient audit is append-only');
                END;
            CREATE TRIGGER IF NOT EXISTS recipient_audit_no_replace
                BEFORE INSERT ON recipient_simulation_audit
                WHEN EXISTS (
                    SELECT 1 FROM recipient_simulation_audit
                    WHERE seq=NEW.seq
                       OR event_id=NEW.event_id
                       OR idempotency_key=NEW.idempotency_key
                       OR event_digest=NEW.event_digest
                       OR (
                           event_kind=NEW.event_kind
                           AND outbound_item_ref=NEW.outbound_item_ref
                           AND simulation_ref=NEW.simulation_ref
                       )
                       OR (
                           NEW.event_kind='evaluation'
                           AND event_kind='evaluation'
                           AND simulation_ref=NEW.simulation_ref
                       )
                ) BEGIN
                    SELECT RAISE(ABORT, 'recipient audit is append-only');
                END;
            """
        )
        self._conn.commit()
        if self.path != ":memory:":
            os.chmod(self.path, 0o600)

    @staticmethod
    def _decode(row: sqlite3.Row) -> RecipientAuditEventV1:
        try:
            payload = json.loads(row["payload_json"])
            event = RecipientAuditEventV1(**payload)
            risk_codes = json.loads(row["risk_codes_json"])
            repair_codes = json.loads(row["repair_codes_json"])
        except (TypeError, ValueError) as exc:
            raise ValueError("recipient audit payload is unreadable") from exc
        indexed = {
            "event_id": str(row["event_id"]),
            "idempotency_key": str(row["idempotency_key"]),
            "event_kind": str(row["event_kind"]),
            "outbound_item_ref": str(row["outbound_item_ref"]),
            "recipient_person_id": str(row["recipient_person_id"]),
            "scope_revision": str(row["scope_revision"]),
            "high_salience": bool(row["high_salience"]),
            "occurred_at": str(row["occurred_at"]),
            "simulation_ref": str(row["simulation_ref"]),
            "request_digest": str(row["request_digest"]),
            "result_digest": str(row["result_digest"]),
            "draft_digest": str(row["draft_digest"]),
            "surface": str(row["surface"]),
            "risk_class": str(row["risk_class"]),
            "evaluated": bool(row["evaluated"]),
            "would_action": str(row["would_action"]),
            "effective_action": str(row["effective_action"]),
            "evaluation_path": str(row["evaluation_path"]),
            "risk_codes": tuple(risk_codes),
            "repair_codes": tuple(repair_codes),
            "event_digest": str(row["event_digest"]),
        }
        expected = {
            "event_id": event.event_id,
            "idempotency_key": event.idempotency_key,
            "event_kind": event.event_kind,
            "outbound_item_ref": event.outbound_item_ref,
            "recipient_person_id": event.recipient_person_id,
            "scope_revision": event.scope_revision,
            "high_salience": event.high_salience,
            "occurred_at": event.occurred_at,
            "simulation_ref": event.simulation_ref,
            "request_digest": event.request_digest,
            "result_digest": event.result_digest,
            "draft_digest": event.draft_digest,
            "surface": event.surface,
            "risk_class": event.risk_class,
            "evaluated": event.evaluated,
            "would_action": event.would_action,
            "effective_action": event.effective_action,
            "evaluation_path": event.evaluation_path,
            "risk_codes": event.risk_codes,
            "repair_codes": event.repair_codes,
            "event_digest": event.audit_digest,
        }
        if indexed != expected:
            raise ValueError("recipient audit digest/index mismatch")
        return event

    @staticmethod
    def _same_outbound_authority(
        first: RecipientAuditEventV1,
        second: RecipientAuditEventV1,
    ) -> bool:
        return (
            first.outbound_item_ref == second.outbound_item_ref
            and first.recipient_person_id == second.recipient_person_id
            and first.scope_revision == second.scope_revision
            and first.high_salience == second.high_salience
            and first.draft_digest == second.draft_digest
        )

    def append(self, event: RecipientAuditEventV1) -> RecipientAuditAppendV1:
        if not isinstance(event, RecipientAuditEventV1):
            raise ValueError("audit store accepts RecipientAuditEventV1 only")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT * FROM recipient_simulation_audit "
                    "WHERE event_id=? OR idempotency_key=? "
                    "OR (event_kind=? AND outbound_item_ref=? "
                    "AND simulation_ref=?) "
                    "OR (event_kind='evaluation' AND ?='evaluation' "
                    "AND simulation_ref=?) ORDER BY seq",
                    (
                        event.event_id,
                        event.idempotency_key,
                        event.event_kind,
                        event.outbound_item_ref,
                        event.simulation_ref,
                        event.event_kind,
                        event.simulation_ref,
                    ),
                ).fetchall()
                if rows:
                    for row in rows:
                        saved = self._decode(row)
                        if (
                            saved.event_id == event.event_id
                            and saved.idempotency_key == event.idempotency_key
                            and saved.event_kind == event.event_kind
                            and saved.outbound_item_ref == event.outbound_item_ref
                            and saved.audit_digest == event.audit_digest
                        ):
                            self._conn.commit()
                            return RecipientAuditAppendV1(
                                event=saved,
                                sequence=int(row["seq"]),
                                appended=False,
                                replayed=True,
                            )
                    raise RecipientAuditConflictError(
                        "recipient audit identity changed immutable content")

                counterpart_kind = (
                    "evaluation" if event.event_kind == "sample" else "sample")
                counterpart_rows = self._conn.execute(
                    "SELECT * FROM recipient_simulation_audit "
                    "WHERE event_kind=? AND outbound_item_ref=? ORDER BY seq",
                    (counterpart_kind, event.outbound_item_ref),
                ).fetchall()
                if event.event_kind == "evaluation" and not counterpart_rows:
                    raise RecipientAuditConflictError(
                        "evaluation requires an immutable sample first")
                for counterpart_row in counterpart_rows:
                    counterpart = self._decode(counterpart_row)
                    if not self._same_outbound_authority(event, counterpart):
                        raise RecipientAuditConflictError(
                            "evaluation does not match its immutable sample authority")
                    sample = event if event.event_kind == "sample" else counterpart
                    evaluation = (
                        event if event.event_kind == "evaluation" else counterpart)
                    if evaluation.occurred_at < sample.occurred_at:
                        raise RecipientAuditConflictError(
                            "evaluation predates its immutable sample")

                cursor = self._conn.execute(
                    "INSERT INTO recipient_simulation_audit "
                    "(event_id,idempotency_key,event_kind,outbound_item_ref,"
                    "recipient_person_id,scope_revision,high_salience,occurred_at,"
                    "simulation_ref,request_digest,result_digest,draft_digest,"
                    "surface,risk_class,evaluated,would_action,effective_action,"
                    "evaluation_path,risk_codes_json,repair_codes_json,"
                    "event_digest,payload_json,stored_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_id,
                        event.idempotency_key,
                        event.event_kind,
                        event.outbound_item_ref,
                        event.recipient_person_id,
                        event.scope_revision,
                        int(event.high_salience),
                        event.occurred_at,
                        event.simulation_ref,
                        event.request_digest,
                        event.result_digest,
                        event.draft_digest,
                        event.surface,
                        event.risk_class,
                        int(event.evaluated),
                        event.would_action,
                        event.effective_action,
                        event.evaluation_path,
                        _canonical(event.risk_codes),
                        _canonical(event.repair_codes),
                        event.audit_digest,
                        _canonical(event.public()),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                self._conn.commit()
                return RecipientAuditAppendV1(
                    event=event,
                    sequence=int(cursor.lastrowid),
                    appended=True,
                    replayed=False,
                )
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _authorized_where(
        viewer: ViewerContextV1,
        *,
        alias: str = "",
    ) -> tuple[str, tuple[Any, ...]]:
        if viewer.viewer_person_id == viewer.owner_person_id:
            return "", ()
        prefix = f"{alias}." if alias else ""
        return (
            f" AND {prefix}recipient_person_id=? "
            f"AND {prefix}scope_revision=?",
            (viewer.viewer_person_id, viewer.scope_revision),
        )

    def project(
        self,
        viewer: ViewerContextV1,
        *,
        max_events: int = 64,
    ) -> RecipientAuditProjectionV1:
        if not isinstance(viewer, ViewerContextV1):
            raise ValueError("viewer must be ViewerContextV1")
        if not 1 <= int(max_events) <= MAX_PROJECTED_EVENTS:
            raise ValueError("max_events is outside bounded projection range")
        if not viewer.attested or not viewer.viewer_person_id:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "viewer_digest": viewer.audit_digest,
                "viewer_attested": False,
                "event_digests": (),
                "corrupt_count": 0,
                "truncated": False,
                "max_events": int(max_events),
            }
            return RecipientAuditProjectionV1(
                events=(),
                viewer_attested=False,
                corrupt_count=0,
                truncated=False,
                viewer_digest=viewer.audit_digest,
                audit_digest=_digest(payload),
            )
        where, params = self._authorized_where(viewer)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM recipient_simulation_audit WHERE 1=1"
                + where + " ORDER BY seq DESC LIMIT ?",
                (*params, int(max_events) + 1),
            ).fetchall()
        truncated = len(rows) > int(max_events)
        events: list[RecipientAuditEventV1] = []
        corrupt_count = 0
        for row in rows[:int(max_events)]:
            try:
                events.append(self._decode(row))
            except Exception:
                corrupt_count += 1
        payload = {
            "schema_version": SCHEMA_VERSION,
            "viewer_digest": viewer.audit_digest,
            "viewer_attested": True,
            "event_digests": tuple(event.audit_digest for event in events),
            "corrupt_count": corrupt_count,
            "truncated": truncated,
            "max_events": int(max_events),
        }
        return RecipientAuditProjectionV1(
            events=tuple(events),
            viewer_attested=True,
            corrupt_count=corrupt_count,
            truncated=truncated,
            viewer_digest=viewer.audit_digest,
            audit_digest=_digest(payload),
        )

    def coverage(
        self,
        viewer: ViewerContextV1,
        *,
        max_missing_refs: int = 32,
    ) -> RecipientAuditCoverageV1:
        """Account for all authorized sampled high-salience outbound items."""

        if not isinstance(viewer, ViewerContextV1):
            raise ValueError("viewer must be ViewerContextV1")
        if not 1 <= int(max_missing_refs) <= MAX_MISSING_REFS:
            raise ValueError("max_missing_refs is outside bounded coverage range")
        if not viewer.attested or not viewer.viewer_person_id:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "viewer_digest": viewer.audit_digest,
                "status": "viewer_unattested",
                "sampled": 0,
                "evaluated": 0,
                "missing": (),
                "missing_refs_truncated": False,
            }
            return RecipientAuditCoverageV1(
                sampled_high_salience=0,
                evaluated_high_salience=0,
                unevaluated_high_salience=0,
                missing_item_refs=(),
                missing_refs_truncated=False,
                corrupt_count=0,
                scan_truncated=False,
                status="viewer_unattested",
                coverage_complete=False,
                viewer_digest=viewer.audit_digest,
                audit_digest=_digest(payload),
            )

        where, params = self._authorized_where(viewer)
        samples: dict[str, RecipientAuditEventV1] = {}
        evaluated_items: set[str] = set()
        corrupt_count = 0
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM recipient_simulation_audit "
                "WHERE event_kind IN ('sample','evaluation')" + where
                + " ORDER BY seq",
                params,
            )
            while True:
                rows = cursor.fetchmany(COVERAGE_FETCH_SIZE)
                if not rows:
                    break
                for row in rows:
                    try:
                        event = self._decode(row)
                    except Exception:
                        corrupt_count += 1
                        continue
                    if event.event_kind == "sample" and event.high_salience:
                        samples[event.outbound_item_ref] = event
                    elif event.event_kind == "evaluation" and event.evaluated:
                        evaluated_items.add(event.outbound_item_ref)
        scan_truncated = False
        sampled = len(samples)
        evaluated = sum(
            outbound in evaluated_items for outbound in samples)
        unevaluated = sampled - evaluated
        all_missing = tuple(sorted(
            outbound for outbound in samples if outbound not in evaluated_items))
        missing_truncated = len(all_missing) > int(max_missing_refs)
        missing = all_missing[:int(max_missing_refs)]
        if corrupt_count:
            status = "indeterminate"
            complete = False
        elif sampled == 0:
            status = "no_samples"
            complete = False
        elif unevaluated:
            status = "incomplete"
            complete = False
        else:
            status = "complete"
            complete = True
        payload = {
            "schema_version": SCHEMA_VERSION,
            "viewer_digest": viewer.audit_digest,
            "status": status,
            "sampled": sampled,
            "evaluated": evaluated,
            "missing": missing,
            "missing_refs_truncated": missing_truncated,
            "corrupt_count": corrupt_count,
            "scan_truncated": scan_truncated,
        }
        return RecipientAuditCoverageV1(
            sampled_high_salience=sampled,
            evaluated_high_salience=evaluated,
            unevaluated_high_salience=unevaluated,
            missing_item_refs=missing,
            missing_refs_truncated=missing_truncated,
            corrupt_count=corrupt_count,
            scan_truncated=scan_truncated,
            status=status,
            coverage_complete=complete,
            viewer_digest=viewer.audit_digest,
            audit_digest=_digest(payload),
        )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


def open_recipient_simulation_audit_store(
    db_path: str | os.PathLike[str],
    *,
    mode: Optional[str] = None,
) -> Optional[RecipientSimulationAuditStore]:
    """Open only for explicit shadow/live mode; off/unknown creates no state."""

    selected = recipient_simulator_mode() if mode is None else str(mode).strip().lower()
    if selected not in {"shadow", "live"}:
        return None
    return RecipientSimulationAuditStore(db_path)


__all__ = [
    "RecipientAuditAppendV1",
    "RecipientAuditConflictError",
    "RecipientAuditCoverageV1",
    "RecipientAuditEventV1",
    "RecipientAuditProjectionV1",
    "RecipientSimulationAuditStore",
    "evaluation_event_from_result",
    "open_recipient_simulation_audit_store",
    "sample_event",
]
