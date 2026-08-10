"""Receipt-bound cognition evidence reduction.

This reducer closes the durable outcome-to-learning seam without trusting a
worker, model, or external producer to declare its own success.  Project and
WorkOrder events are rejoined to Colony's local immutable project ledger.
External cognition events remain visible as reported/unverified observations
unless a future server-owned resolver joins them to an exact local receipt.

The host event journal is the only input cursor.  Downstream writes use stable
event identities so a crash after one sink but before cursor commit replays
without duplicating competence or expectation evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
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
from colony_sidecar.projects.store import ProjectStore


_CONSUMER_ID = "cognition-evidence-v1"
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,255}$")
_EVIDENCE_REF = re.compile(r"^[a-z][a-z0-9+.-]{1,31}:[^\s]{1,480}$")
_AUTHORITY_STATES = frozenset({
    "verified", "unverified", "neutral", "reported_unverified",
})
_OUTCOMES = frozenset({"success", "failure", "timeout", "neutral"})


def cognition_evidence_mode() -> str:
    value = os.environ.get("COLONY_COGNITION_EVIDENCE", "off").strip().lower()
    return value if value in {"off", "shadow", "live"} else "off"


def cognition_evidence_enabled() -> bool:
    return cognition_evidence_mode() in {"shadow", "live"}


def _bootstrap_mode() -> str:
    value = os.environ.get(
        "COLONY_COGNITION_EVIDENCE_BOOTSTRAP", "beginning",
    ).strip().lower()
    return value if value in {"beginning", "tail"} else "beginning"


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _ledger_digest(
    *,
    event_seq: int,
    event_id: str,
    event_type: str,
    raw_digest: str,
    projection_digest: Optional[str],
    disposition: str,
    applied_sinks: Sequence[str],
    recorded_at: float,
) -> str:
    """Bind every authoritative evidence-ledger envelope field."""

    return _digest({
        "schema": "CognitionEvidenceLedgerEntryV1",
        "version": 1,
        "event_seq": int(event_seq),
        "event_id": str(event_id),
        "event_type": str(event_type),
        "raw_digest": str(raw_digest),
        "projection_digest": str(projection_digest or ""),
        "disposition": str(disposition),
        "applied_sinks": list(applied_sinks),
        "recorded_at": float(recorded_at),
    })


def _hex_digest(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not re.fullmatch(r"[a-f0-9]{64}", result):
        raise ValueError(f"{field} is not a lowercase SHA-256 digest")
    return result


def _ref(value: Any, field: str, *, allow_empty: bool = False) -> str:
    result = str(value or "").strip()
    if allow_empty and not result:
        return ""
    if not _SAFE_REF.fullmatch(result):
        raise ValueError(f"{field} is not a bounded reference")
    return result


def _bounded_text(
    value: Any, field: str, *, maximum: int, allow_empty: bool = False,
) -> str:
    result = str(value or "").strip()
    if allow_empty and not result:
        return ""
    if not result or len(result) > maximum:
        raise ValueError(f"{field} is not bounded text")
    return result


def _refs(values: Iterable[Any]) -> tuple[str, ...]:
    result = tuple(sorted(dict.fromkeys(
        _bounded_text(value, "evidence_ref", maximum=512)
        for value in (values or ())
    )))
    if any(not _EVIDENCE_REF.fullmatch(value) for value in result):
        raise ValueError("evidence refs require a bounded URI scheme")
    if len(result) > 512:
        raise ValueError("evidence refs exceed 512 entries")
    return result


def _event_time(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} is not a timestamp")
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError(f"{field} requires a timezone")
        result = parsed.astimezone(timezone.utc).timestamp()
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{field} is outside the supported range")
    return result


def _scope(data: Mapping[str, Any]) -> tuple[str, str, str]:
    subject = _ref(data.get("subject_person_id") or "owner", "subject_person_id")
    viewer = _ref(data.get("viewer_scope") or "owner", "viewer_scope")
    sharing = str(data.get("shareability") or "owner_private").strip().lower()
    if sharing not in {"owner_private", "subject_private", "shared", "public"}:
        raise ValueError("shareability is invalid")
    if sharing == "owner_private" and viewer != "owner":
        raise ValueError("owner-private evidence requires owner viewer scope")
    if sharing == "subject_private" and viewer != f"person:{subject}":
        raise ValueError("subject-private evidence requires subject viewer scope")
    if sharing == "public" and viewer != "public":
        raise ValueError("public evidence requires public viewer scope")
    if sharing == "shared" and not (
        viewer == "shared" or viewer.startswith("shared:")
    ):
        raise ValueError("shared evidence requires shared viewer scope")
    return subject, viewer, sharing


@dataclass(frozen=True)
class EvidenceProjectionV1:
    event_seq: int
    event_id: str
    event_type: str
    occurred_at: float
    authority_state: str
    outcome: str
    domain: str
    project_id: str
    step_id: str
    work_order_id: str
    result_ref: str
    subject_person_id: str
    viewer_scope: str
    shareability: str
    evidence_refs: tuple[str, ...]
    latency_seconds: Optional[float]
    stated_confidence: Optional[float]
    disposition: str
    local_evidence: Mapping[str, Any]
    schema: str = "EvidenceProjectionV1"
    version: int = 1

    def __post_init__(self) -> None:
        if self.event_seq < 1:
            raise ValueError("evidence projection requires a positive sequence")
        _ref(self.event_id, "event_id")
        _bounded_text(
            self.project_id, "project_id", maximum=128, allow_empty=True,
        )
        _bounded_text(
            self.step_id, "step_id", maximum=128, allow_empty=True,
        )
        _ref(self.work_order_id, "work_order_id", allow_empty=True)
        _ref(self.result_ref, "result_ref", allow_empty=True)
        if self.authority_state not in _AUTHORITY_STATES:
            raise ValueError("evidence authority state is invalid")
        if self.outcome not in _OUTCOMES:
            raise ValueError("evidence outcome is invalid")
        if _scope({
            "subject_person_id": self.subject_person_id,
            "viewer_scope": self.viewer_scope,
            "shareability": self.shareability,
        }) != (
            self.subject_person_id, self.viewer_scope, self.shareability,
        ):
            raise ValueError("evidence scope is not canonical")
        if _refs(self.evidence_refs) != self.evidence_refs:
            raise ValueError("evidence refs are not canonical")
        if self.latency_seconds is not None and (
            not math.isfinite(self.latency_seconds) or self.latency_seconds < 0.0
        ):
            raise ValueError("evidence latency is invalid")
        if self.stated_confidence is not None and not (
            0.0 <= self.stated_confidence <= 1.0
        ):
            raise ValueError("stated confidence is invalid")

    @property
    def projection_digest(self) -> str:
        return _digest(self.payload())

    def payload(self) -> Dict[str, Any]:
        result = asdict(self)
        result["evidence_refs"] = list(self.evidence_refs)
        result["local_evidence"] = dict(self.local_evidence)
        return result


def _event_identity(
    raw: Mapping[str, Any],
) -> tuple[int, str, str, float, Mapping[str, Any], str]:
    sequence = int(raw.get("seq") or 0)
    event_id = _ref(
        raw.get("ulid") or f"journal-seq-{sequence}", "event_id",
    )
    event_type = str(raw.get("type") or "").strip().lower()
    if sequence < 1 or not re.fullmatch(
        r"[a-z0-9][a-z0-9_.:\-]{0,127}", event_type,
    ):
        raise ValueError("journal event identity is invalid")
    occurred = _event_time(
        raw.get("occurredAt") or raw.get("recordedAt"), "occurred_at",
    )
    data = raw.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("journal event data is not an object")
    raw_digest = _digest({
        "event_type": event_type,
        "occurred_at": occurred,
        "data": data,
    })
    return sequence, event_id, event_type, occurred, data, raw_digest


def _validate_self_digest(
    data: Mapping[str, Any], field: str,
) -> Dict[str, Any]:
    value = dict(data)
    expected = str(value.pop(field, ""))
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or _digest(value) != expected:
        raise ValueError(f"{field} does not bind the event payload")
    return value


def _mode_at_stage(data: Mapping[str, Any]) -> str:
    mode = str(data.get("cognition_mode_at_stage") or "").strip().lower()
    if mode not in {"off", "shadow", "live"}:
        raise ValueError("project evidence stage mode is invalid")
    return mode


def _require_project_outbox_binding(
    project_store: ProjectStore,
    *,
    event_key: str,
    event_seq: int,
    raw_event_seq: Any,
    event_id: str,
    raw_event_id: Any,
    event_type: str,
    raw_event_type: str,
    occurred_at: float,
    occurred_at_text: str,
    recorded_at_text: str,
    payload: Mapping[str, Any],
) -> None:
    """Rejoin a journal event to its exact server-owned outbox projection."""

    row = project_store.project_event(event_key)
    if row is None:
        raise ValueError("project evidence has no local outbox projection")
    projected_payload = row.get("payload")
    if row.get("payload_error") or not isinstance(projected_payload, dict):
        raise ValueError("project evidence outbox payload is invalid")
    if (
        row.get("state") != "projected"
        or type(raw_event_seq) is not int
        or raw_event_seq != event_seq
        or type(raw_event_id) is not str
        or raw_event_id != event_id
        or int(row.get("journal_seq") or 0) != event_seq
        or str(row.get("journal_event_id") or "") != event_id
        or str(row.get("event_type") or "") != event_type
        or str(row.get("event_type") or "") != raw_event_type
        or str(row.get("occurred_at") or "") != occurred_at_text
        or str(row.get("journal_recorded_at") or "") != recorded_at_text
        or _event_time(row.get("occurred_at"), "outbox_occurred_at")
        != occurred_at
        or _canonical(projected_payload) != _canonical(dict(payload))
    ):
        raise ValueError("project evidence differs from its outbox projection")
    expected_digest = ProjectStore.project_event_envelope_digest(
        event_key=event_key,
        event_type=event_type,
        occurred_at=str(row.get("occurred_at") or ""),
        payload=projected_payload,
    )
    if str(row.get("event_digest") or "") != expected_digest:
        raise ValueError("project evidence outbox envelope digest mismatch")


def _execution_projection(
    raw: Mapping[str, Any], project_store: ProjectStore,
) -> EvidenceProjectionV1:
    sequence, event_id, event_type, occurred, data, _ = _event_identity(raw)
    if data.get("schema") != "ProjectExecutionEvidenceV2" \
            or data.get("version") != 2:
        raise ValueError("execution evidence schema is invalid")
    _validate_self_digest(data, "evidence_digest")
    project_id = _bounded_text(
        data.get("project_id"), "project_id", maximum=128,
    )
    step_id = _bounded_text(data.get("step_id"), "step_id", maximum=128)
    work_order_id = _ref(data.get("work_order_id"), "work_order_id")
    result_ref = _ref(data.get("result_ref"), "result_ref")
    run_id = _bounded_text(data.get("run_id"), "run_id", maximum=256)
    attempt_number = int(data.get("attempt_number") or 0)
    if attempt_number < 1:
        raise ValueError("execution evidence attempt number is invalid")
    mode_at_stage = _mode_at_stage(data)
    identity = _digest({
        "work_order_id": work_order_id,
        "run_id": run_id,
    })
    _require_project_outbox_binding(
        project_store,
        event_key=f"project-execution:{identity}",
        event_seq=sequence,
        raw_event_seq=raw.get("seq"),
        event_id=event_id,
        raw_event_id=raw.get("ulid"),
        event_type=event_type,
        raw_event_type=str(raw.get("type") or ""),
        occurred_at=occurred,
        occurred_at_text=str(raw.get("occurredAt") or ""),
        recorded_at_text=str(raw.get("recordedAt") or ""),
        payload=data,
    )

    work_order = project_store.get_work_order(work_order_id)
    if work_order is None:
        raise ValueError("execution evidence has no local WorkOrder")
    order_payload = work_order["payload"]
    max_attempts = int(order_payload.get("max_attempts") or 0)
    if (
        max_attempts < 1
        or attempt_number > max_attempts
        or
        work_order["project_id"] != project_id
        or work_order["step_id"] != step_id
        or work_order["work_order_digest"] != data.get("work_order_digest")
        or order_payload.get("work_order_id") != work_order_id
        or order_payload.get("work_order_digest") != data.get("work_order_digest")
    ):
        raise ValueError("execution evidence does not match local WorkOrder authority")

    attempts = [
        item for item in project_store.execution_attempts_for(work_order_id)
        if item["run_id"] == run_id
        and int(item["attempt_number"]) == attempt_number
    ]
    if len(attempts) != 1:
        raise ValueError("execution evidence has no exact local attempt")
    attempt = attempts[0]
    attempt_ref = _ref(attempt.get("attempt_ref"), "attempt_ref")
    result = attempt["result"]
    if (
        result.get("result_ref") != result_ref
        or result.get("work_order_id") != work_order_id
        or result.get("work_order_digest") != data.get("work_order_digest")
        or result.get("run_id") != run_id
        or int(result.get("attempt_number") or 0) != attempt_number
        or _digest(result) != data.get("result_digest")
        or attempt.get("transport_status") != data.get("transport_status")
    ):
        raise ValueError("execution evidence does not match local attempt content")
    for field in (
        "terminal_outcome", "verification_result", "effect_class",
    ):
        if result.get(field) != data.get(field):
            raise ValueError(f"execution evidence {field} mismatch")
    if abs(_event_time(result.get("ended_at"), "ended_at") - occurred) > 0.001:
        raise ValueError("execution event time does not match the local result")

    project = project_store.get_project(project_id)
    if project is None:
        raise ValueError("execution evidence project is unavailable")
    matching_steps = [
        item for item in project_store.steps_for(project_id) if item.id == step_id
    ]
    if len(matching_steps) != 1:
        raise ValueError("execution evidence step is unavailable")
    step = matching_steps[0]
    if (
        step.work_order_ref != work_order["work_order_ref"]
        or step.work_order_digest != data.get("work_order_digest")
        or step.result_ref != result_ref
    ):
        raise ValueError("execution evidence is not bound to the local step")
    subject, viewer, sharing = _scope(data)
    if (
        subject != (project.subject_person_id or "owner")
        or viewer != (project.viewer_scope or "owner")
        or sharing != (project.shareability or "owner_private")
    ):
        raise ValueError("execution evidence scope differs from local project scope")

    receipts = _refs(result.get("receipt_refs") or ())
    if receipts != _refs(data.get("receipt_refs") or ()):
        raise ValueError("execution evidence receipt refs mismatch")
    terminal = str(result.get("terminal_outcome") or "")
    verification = str(result.get("verification_result") or "")
    if terminal == "succeeded" and verification == "verified" and receipts:
        authority_state, outcome, disposition, status = (
            "verified", "success", "verified_execution_success", "verified",
        )
    elif terminal == "failed":
        error = str(result.get("error") or "").lower()
        outcome = "timeout" if "timeout" in error else "failure"
        authority_state, disposition, status = (
            "verified", "verified_execution_failure", "failed",
        )
    elif terminal in {"cancelled", "skipped"}:
        authority_state, outcome, disposition, status = (
            "neutral", "neutral", f"neutral_execution_{terminal}", terminal,
        )
    elif terminal == "succeeded":
        authority_state, outcome, disposition, status = (
            "unverified", "neutral", "unverified_execution_success", "unverified",
        )
    else:
        raise ValueError("execution evidence terminal outcome is invalid")
    if data.get("status") != status:
        raise ValueError("execution evidence status projection mismatch")

    started = _event_time(result.get("started_at"), "started_at")
    ended = _event_time(result.get("ended_at"), "ended_at")
    if ended < started:
        raise ValueError("execution result ends before it starts")
    refs = _refs((attempt_ref, result_ref, *receipts))
    return EvidenceProjectionV1(
        event_seq=sequence,
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred,
        authority_state=authority_state,
        outcome=outcome,
        domain="project",
        project_id=project_id,
        step_id=step_id,
        work_order_id=work_order_id,
        result_ref=result_ref,
        subject_person_id=subject,
        viewer_scope=viewer,
        shareability=sharing,
        evidence_refs=refs,
        latency_seconds=ended - started,
        stated_confidence=float(step.confidence),
        disposition=disposition,
        local_evidence={
            "work_order_digest": str(data["work_order_digest"]),
            "result_digest": str(data["result_digest"]),
            "attempt_ref": attempt_ref,
            "run_id": run_id,
            "attempt_number": attempt_number,
            "max_attempts": max_attempts,
            "effect_class": str(data["effect_class"]),
            "transport_status": str(data["transport_status"]),
            "verifier_identity": str(data.get("verifier_identity") or ""),
            "cognition_mode_at_stage": mode_at_stage,
        },
    )


def _terminal_projection(
    raw: Mapping[str, Any], project_store: ProjectStore,
) -> EvidenceProjectionV1:
    sequence, event_id, event_type, occurred, data, _ = _event_identity(raw)
    if data.get("schema") != "ProjectTerminalEvidenceV2" \
            or data.get("version") != 2:
        raise ValueError("project terminal evidence schema is invalid")
    _validate_self_digest(data, "project_digest")
    project_id = _bounded_text(
        data.get("project_id"), "project_id", maximum=128,
    )
    mode_at_stage = _mode_at_stage(data)
    identity = _digest({
        "project_id": project_id,
        "lifecycle_status": data.get("lifecycle_status"),
        "outcome": data.get("outcome"),
    })
    _require_project_outbox_binding(
        project_store,
        event_key=f"project-terminal:{identity}",
        event_seq=sequence,
        raw_event_seq=raw.get("seq"),
        event_id=event_id,
        raw_event_id=raw.get("ulid"),
        event_type=event_type,
        raw_event_type=str(raw.get("type") or ""),
        occurred_at=occurred,
        occurred_at_text=str(raw.get("occurredAt") or ""),
        recorded_at_text=str(raw.get("recordedAt") or ""),
        payload=data,
    )
    project = project_store.get_project(project_id)
    if project is None:
        raise ValueError("project terminal evidence has no local project")
    if (
        project.status != data.get("lifecycle_status")
        or project.outcome != data.get("outcome")
        or str(project.reason or "")[:128] != data.get("reason_code")
        or str(project.source or "")[:64] != data.get("source")
    ):
        raise ValueError("project terminal evidence differs from local project")
    subject, viewer, sharing = _scope(data)
    if (
        subject != (project.subject_person_id or "owner")
        or viewer != (project.viewer_scope or "owner")
        or sharing != (project.shareability or "owner_private")
    ):
        raise ValueError("project terminal scope differs from local project scope")

    expected_results: list[Dict[str, str]] = []
    evidence_refs: list[str] = []
    verified = bool(project_store.steps_for(project_id)) \
        and project.outcome == "succeeded"
    for step in project_store.steps_for(project_id):
        if step.status != "done" or not step.result_ref:
            verified = False
            continue
        result = project_store.get_execution_result(step.result_ref)
        if result is None:
            verified = False
            continue
        payload = result["payload"]
        work_order = project_store.get_work_order(result["work_order_id"])
        receipts = _refs(payload.get("receipt_refs") or ())
        if (
            work_order is None
            or work_order.get("project_id") != project_id
            or work_order.get("step_id") != step.id
            or work_order.get("work_order_digest")
            != result.get("work_order_digest")
            or payload.get("work_order_id") != result.get("work_order_id")
            or payload.get("work_order_digest")
            != result.get("work_order_digest")
            or
            result.get("terminal_outcome") != "succeeded"
            or result.get("verification_result") != "verified"
            or not receipts
        ):
            verified = False
        expected_results.append({
            "step_id": step.id,
            "result_ref": step.result_ref,
            "result_digest": _digest(payload),
        })
        evidence_refs.extend((step.result_ref, *receipts))
    if list(data.get("result_refs") or ()) != expected_results:
        raise ValueError("project terminal result set differs from local ledger")
    expected_state = "verified" if verified else "unverified"
    if data.get("evidence_status") != expected_state:
        raise ValueError("project terminal evidence status is not locally derived")
    expected_status = (
        "verified" if verified else
        "failed" if project.status == "abandoned" else
        "unverified"
    )
    if data.get("status") != expected_status:
        raise ValueError("project terminal status is not locally derived")
    terminal_ref = f"project-terminal:{str(data['project_digest'])}"
    return EvidenceProjectionV1(
        event_seq=sequence,
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred,
        authority_state=expected_state,
        outcome="success" if verified else "neutral",
        domain="project",
        project_id=project_id,
        step_id="",
        work_order_id="",
        result_ref="",
        subject_person_id=subject,
        viewer_scope=viewer,
        shareability=sharing,
        evidence_refs=_refs((terminal_ref, *evidence_refs)),
        latency_seconds=None,
        stated_confidence=None,
        disposition=f"{expected_state}_project_terminal",
        local_evidence={
            "project_digest": str(data["project_digest"]),
            "terminal_ref": terminal_ref,
            "lifecycle_status": str(data["lifecycle_status"]),
            "project_outcome": str(data["outcome"]),
            "concern_id": str(data.get("concern_id") or ""),
            "goal_proposal_id": str(data.get("goal_proposal_id") or ""),
            "cognition_mode_at_stage": mode_at_stage,
        },
    )


def _external_projection(raw: Mapping[str, Any]) -> EvidenceProjectionV1:
    if type(raw.get("seq")) is not int or raw.get("seq") < 1:
        raise ValueError("external cognition journal sequence is not canonical")
    from colony_sidecar.cognition.external_events import (
        validate_external_journal_event_id,
        validate_external_journal_projection,
    )

    validate_external_journal_event_id(raw.get("ulid"))
    sequence, event_id, event_type, occurred, data, _ = _event_identity(raw)
    if type(raw.get("ulid")) is not str or raw.get("ulid") != event_id:
        raise ValueError("external cognition journal ID is not canonical")
    if type(raw.get("type")) is not str or raw.get("type") != event_type:
        raise ValueError("external cognition journal type is not canonical")

    projected = validate_external_journal_projection(event_type, data)
    projected_occurred = projected["external_occurred_at"]
    if (
        type(raw.get("occurredAt")) is not str
        or raw.get("occurredAt") != projected_occurred
        or _event_time(projected_occurred, "external_occurred_at") != occurred
    ):
        raise ValueError(
            "external cognition host time differs from its server projection"
        )
    external_id = projected["external_event_id"]
    external_digest = projected["external_event_digest"]
    producer = projected["producer_principal_id"]
    revision = projected["producer_revision"]
    subject = projected["subject_person_id"]
    viewer = projected["viewer_scope"]
    sharing = projected["shareability"]
    return EvidenceProjectionV1(
        event_seq=sequence,
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred,
        authority_state="reported_unverified",
        outcome="neutral",
        domain="external_cognition",
        project_id="",
        step_id="",
        work_order_id="",
        result_ref="",
        subject_person_id=subject,
        viewer_scope=viewer,
        shareability=sharing,
        evidence_refs=(),
        latency_seconds=None,
        stated_confidence=None,
        disposition="reported_external_observation",
        local_evidence={
            "external_event_id": external_id,
            "external_event_digest": external_digest,
            "producer_principal_id": producer,
            "producer_revision": revision,
        },
    )


def project_evidence_event(
    raw: Mapping[str, Any], project_store: ProjectStore,
) -> tuple[Optional[EvidenceProjectionV1], str, str]:
    _, _, event_type, _, _, raw_digest = _event_identity(raw)
    if event_type == "work_order.result":
        return _execution_projection(raw, project_store), "", raw_digest
    if event_type in {"project.completed", "project.abandoned"}:
        return _terminal_projection(raw, project_store), "", raw_digest
    if event_type.startswith("cognition.external."):
        return _external_projection(raw), "", raw_digest
    return None, "unmapped_event_type", raw_digest


class CognitionEvidenceStore:
    """Append-only evidence ledger plus one durable host-journal cursor."""

    def __init__(self, db_path: str) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cognition_evidence_cursors (
                    consumer_id TEXT PRIMARY KEY,
                    last_seq INTEGER NOT NULL,
                    bootstrap_mode TEXT NOT NULL,
                    last_error TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cognition_evidence_events (
                    event_seq INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    raw_digest TEXT NOT NULL,
                    projection_digest TEXT,
                    disposition TEXT NOT NULL,
                    authority_state TEXT,
                    outcome TEXT,
                    domain TEXT,
                    project_id TEXT,
                    step_id TEXT,
                    work_order_id TEXT,
                    result_ref TEXT,
                    subject_person_id TEXT,
                    viewer_scope TEXT,
                    shareability TEXT,
                    occurred_at REAL,
                    evidence_refs_json TEXT,
                    projection_json TEXT,
                    applied_sinks_json TEXT NOT NULL,
                    ledger_digest TEXT,
                    recorded_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cognition_evidence_project_idx
                    ON cognition_evidence_events(project_id,event_seq);
                CREATE INDEX IF NOT EXISTS cognition_evidence_work_idx
                    ON cognition_evidence_events(work_order_id,event_seq);
                CREATE TABLE IF NOT EXISTS cognition_evidence_gaps (
                    gap_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    consumer_id TEXT NOT NULL,
                    prior_cursor INTEGER NOT NULL,
                    resume_after INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    acknowledged_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cognition_evidence_passthrough_ranges (
                    range_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    consumer_id TEXT NOT NULL,
                    prior_cursor INTEGER NOT NULL,
                    resume_after INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                );
                """
            )
            event_columns = {
                row[1] for row in self._conn.execute(
                    "PRAGMA table_info(cognition_evidence_events)"
                ).fetchall()
            }
            if "ledger_digest" not in event_columns:
                # Old pre-release rows deliberately remain unverifiable.  Do
                # not bless possibly modified content during an upgrade by
                # manufacturing a digest from the current mutable row.
                self._conn.execute(
                    "ALTER TABLE cognition_evidence_events "
                    "ADD COLUMN ledger_digest TEXT"
                )
            self._conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS cognition_evidence_events_no_update
                BEFORE UPDATE ON cognition_evidence_events
                BEGIN
                    SELECT RAISE(ABORT, 'cognition evidence ledger is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cognition_evidence_events_no_delete
                BEFORE DELETE ON cognition_evidence_events
                BEGIN
                    SELECT RAISE(ABORT, 'cognition evidence ledger is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cognition_evidence_gaps_no_update
                BEFORE UPDATE ON cognition_evidence_gaps
                BEGIN
                    SELECT RAISE(ABORT, 'cognition evidence gaps are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cognition_evidence_gaps_no_delete
                BEFORE DELETE ON cognition_evidence_gaps
                BEGIN
                    SELECT RAISE(ABORT, 'cognition evidence gaps are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cognition_evidence_passthrough_no_update
                BEFORE UPDATE ON cognition_evidence_passthrough_ranges
                BEGIN
                    SELECT RAISE(ABORT, 'cognition evidence passthrough is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cognition_evidence_passthrough_no_delete
                BEFORE DELETE ON cognition_evidence_passthrough_ranges
                BEGIN
                    SELECT RAISE(ABORT, 'cognition evidence passthrough is append-only');
                END;
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def cursor(self, consumer_id: str) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_seq FROM cognition_evidence_cursors "
                "WHERE consumer_id=?", (consumer_id,),
            ).fetchone()
        return int(row["last_seq"]) if row is not None else None

    def initialize_cursor(
        self, consumer_id: str, sequence: int, *, bootstrap_mode: str,
    ) -> int:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO cognition_evidence_cursors
                   (consumer_id,last_seq,bootstrap_mode,last_error,updated_at)
                   VALUES (?,?,?,NULL,?)""",
                (consumer_id, int(sequence), bootstrap_mode, time.time()),
            )
            self._conn.commit()
        value = self.cursor(consumer_id)
        return int(value if value is not None else sequence)

    def set_error(self, consumer_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE cognition_evidence_cursors SET last_error=?,updated_at=?
                   WHERE consumer_id=?""",
                (str(error or "evidence_reducer_failed")[:500], time.time(), consumer_id),
            )
            self._conn.commit()

    def acknowledge_gap(
        self, consumer_id: str, *, prior_cursor: int, resume_after: int,
        reason: str,
    ) -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._conn.execute(
                    "SELECT last_seq FROM cognition_evidence_cursors "
                    "WHERE consumer_id=?", (consumer_id,),
                ).fetchone()
                if current is None or int(current["last_seq"]) != int(prior_cursor):
                    raise ValueError("evidence gap cursor changed")
                self._conn.execute(
                    """INSERT INTO cognition_evidence_gaps
                       (consumer_id,prior_cursor,resume_after,reason,acknowledged_at)
                       VALUES (?,?,?,?,?)""",
                    (
                        consumer_id, int(prior_cursor), int(resume_after),
                        str(reason)[:500], time.time(),
                    ),
                )
                self._conn.execute(
                    """UPDATE cognition_evidence_cursors SET last_seq=?,
                       last_error=NULL,updated_at=? WHERE consumer_id=?""",
                    (int(resume_after), time.time(), consumer_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return int(resume_after)

    def checkpoint_passthrough(
        self,
        consumer_id: str,
        *,
        prior_cursor: int,
        resume_after: int,
        mode: str = "off",
        reason: str = "evidence_mode_off_passthrough",
    ) -> int:
        prior = int(prior_cursor)
        resume = int(resume_after)
        if resume <= prior:
            raise ValueError("evidence passthrough cursor must advance")
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode != "off":
            raise ValueError("only off mode may checkpoint passthrough evidence")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._conn.execute(
                    "SELECT last_seq FROM cognition_evidence_cursors "
                    "WHERE consumer_id=?", (consumer_id,),
                ).fetchone()
                if current is None or int(current["last_seq"]) != prior:
                    raise ValueError("evidence passthrough cursor changed")
                now = time.time()
                self._conn.execute(
                    """INSERT INTO cognition_evidence_passthrough_ranges
                       (consumer_id,prior_cursor,resume_after,mode,reason,recorded_at)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        consumer_id, prior, resume, normalized_mode,
                        str(reason)[:500], now,
                    ),
                )
                self._conn.execute(
                    """UPDATE cognition_evidence_cursors SET last_seq=?,
                       last_error=NULL,updated_at=? WHERE consumer_id=?""",
                    (resume, now, consumer_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return resume

    def apply(
        self,
        *,
        consumer_id: str,
        event_seq: int,
        event_id: str,
        event_type: str,
        raw_digest: str,
        projection: Optional[EvidenceProjectionV1],
        disposition: str,
        applied_sinks: Sequence[str] = (),
    ) -> str:
        sinks = tuple(sorted(dict.fromkeys(str(item) for item in applied_sinks)))
        projection_payload = projection.payload() if projection is not None else None
        projection_digest = (
            projection.projection_digest if projection is not None else None
        )
        recorded_at = time.time()
        entry_digest = _ledger_digest(
            event_seq=event_seq,
            event_id=event_id,
            event_type=event_type,
            raw_digest=raw_digest,
            projection_digest=projection_digest,
            disposition=disposition,
            applied_sinks=sinks,
            recorded_at=recorded_at,
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    "SELECT last_seq FROM cognition_evidence_cursors "
                    "WHERE consumer_id=?", (consumer_id,),
                ).fetchone()
                if cursor is None:
                    raise ValueError("evidence cursor is not initialized")
                current = int(cursor["last_seq"])
                existing = self._conn.execute(
                    "SELECT * FROM cognition_evidence_events WHERE event_seq=?",
                    (int(event_seq),),
                ).fetchone()
                if existing is not None:
                    existing_sinks = json.loads(existing["applied_sinks_json"])
                    existing_digest = _ledger_digest(
                        event_seq=int(existing["event_seq"]),
                        event_id=str(existing["event_id"]),
                        event_type=str(existing["event_type"]),
                        raw_digest=str(existing["raw_digest"]),
                        projection_digest=existing["projection_digest"],
                        disposition=str(existing["disposition"]),
                        applied_sinks=existing_sinks,
                        recorded_at=float(existing["recorded_at"]),
                    )
                    if (
                        existing["event_id"] != event_id
                        or existing["event_type"] != event_type
                        or existing["raw_digest"] != raw_digest
                        or existing["projection_digest"] != projection_digest
                        or existing["disposition"] != disposition
                        or existing["applied_sinks_json"] != _canonical(list(sinks))
                        or existing["ledger_digest"] != existing_digest
                    ):
                        raise ValueError("immutable cognition evidence replay mismatch")
                    result = "duplicate"
                else:
                    if int(event_seq) != current + 1:
                        raise ValueError(
                            "cognition evidence event is not cursor-contiguous"
                        )
                    self._conn.execute(
                        """INSERT INTO cognition_evidence_events
                           (event_seq,event_id,event_type,raw_digest,
                            projection_digest,disposition,authority_state,outcome,
                            domain,project_id,step_id,work_order_id,result_ref,
                            subject_person_id,viewer_scope,shareability,occurred_at,
                            evidence_refs_json,projection_json,applied_sinks_json,
                            ledger_digest,recorded_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            int(event_seq), event_id, event_type, raw_digest,
                            projection_digest, disposition,
                            projection.authority_state if projection else None,
                            projection.outcome if projection else None,
                            projection.domain if projection else None,
                            projection.project_id if projection else None,
                            projection.step_id if projection else None,
                            projection.work_order_id if projection else None,
                            projection.result_ref if projection else None,
                            projection.subject_person_id if projection else None,
                            projection.viewer_scope if projection else None,
                            projection.shareability if projection else None,
                            projection.occurred_at if projection else None,
                            _canonical(list(projection.evidence_refs))
                            if projection else None,
                            _canonical(projection_payload)
                            if projection_payload is not None else None,
                            _canonical(list(sinks)), entry_digest, recorded_at,
                        ),
                    )
                    result = "recorded"
                if int(event_seq) > current:
                    self._conn.execute(
                        """UPDATE cognition_evidence_cursors SET last_seq=?,
                           last_error=NULL,updated_at=? WHERE consumer_id=?""",
                        (int(event_seq), time.time(), consumer_id),
                    )
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def status(self, consumer_id: str) -> Dict[str, Any]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM cognition_evidence_cursors WHERE consumer_id=?",
                (consumer_id,),
            ).fetchone()
            counts = self._conn.execute(
                "SELECT disposition,COUNT(*) AS n FROM cognition_evidence_events "
                "GROUP BY disposition"
            ).fetchall()
            gap_count = int(self._conn.execute(
                "SELECT COUNT(*) AS n FROM cognition_evidence_gaps "
                "WHERE consumer_id=?", (consumer_id,),
            ).fetchone()["n"])
            latest_gap = self._conn.execute(
                "SELECT prior_cursor,resume_after,reason,acknowledged_at "
                "FROM cognition_evidence_gaps WHERE consumer_id=? "
                "ORDER BY gap_id DESC LIMIT 1", (consumer_id,),
            ).fetchone()
            passthrough_count = int(self._conn.execute(
                "SELECT COUNT(*) AS n FROM cognition_evidence_passthrough_ranges "
                "WHERE consumer_id=?", (consumer_id,),
            ).fetchone()["n"])
            latest_passthrough = self._conn.execute(
                "SELECT prior_cursor,resume_after,mode,reason,recorded_at "
                "FROM cognition_evidence_passthrough_ranges WHERE consumer_id=? "
                "ORDER BY range_id DESC LIMIT 1", (consumer_id,),
            ).fetchone()
        return {
            "initialized": cursor is not None,
            "cursor": int(cursor["last_seq"]) if cursor is not None else None,
            "bootstrap_mode": cursor["bootstrap_mode"] if cursor else None,
            "last_error": str(cursor["last_error"] or "") if cursor else "",
            "dispositions": {
                row["disposition"]: int(row["n"]) for row in counts
            },
            "gaps": {
                "count": gap_count,
                "latest": dict(latest_gap) if latest_gap is not None else None,
            },
            "passthrough": {
                "count": passthrough_count,
                "latest": (
                    dict(latest_passthrough)
                    if latest_passthrough is not None else None
                ),
            },
        }

    @staticmethod
    def _verified_trace_row(raw_row: Mapping[str, Any]) -> Dict[str, Any]:
        """Decode one ledger row only after every stored binding verifies."""

        row = dict(raw_row)
        try:
            sinks_raw = json.loads(str(row["applied_sinks_json"]))
            if not isinstance(sinks_raw, list) or any(
                not isinstance(item, str) for item in sinks_raw
            ):
                raise ValueError("applied sinks are not a string list")
            sinks = tuple(sorted(dict.fromkeys(sinks_raw)))
            if sinks_raw != list(sinks):
                raise ValueError("applied sinks are not canonical")
            expected_ledger = _ledger_digest(
                event_seq=int(row["event_seq"]),
                event_id=str(row["event_id"]),
                event_type=str(row["event_type"]),
                raw_digest=str(row["raw_digest"]),
                projection_digest=row.get("projection_digest"),
                disposition=str(row["disposition"]),
                applied_sinks=sinks,
                recorded_at=float(row["recorded_at"]),
            )
            if row.get("ledger_digest") != expected_ledger:
                raise ValueError("ledger envelope digest mismatch")

            projection_raw = json.loads(str(row["projection_json"]))
            if not isinstance(projection_raw, dict):
                raise ValueError("projection is not an object")
            projection = EvidenceProjectionV1(**{
                **projection_raw,
                "evidence_refs": tuple(projection_raw.get("evidence_refs") or ()),
            })
            if projection.projection_digest != row.get("projection_digest"):
                raise ValueError("projection digest mismatch")
            expected_columns = {
                "event_seq": projection.event_seq,
                "event_id": projection.event_id,
                "event_type": projection.event_type,
                "disposition": projection.disposition,
                "authority_state": projection.authority_state,
                "outcome": projection.outcome,
                "domain": projection.domain,
                "project_id": projection.project_id,
                "step_id": projection.step_id,
                "work_order_id": projection.work_order_id,
                "result_ref": projection.result_ref,
                "subject_person_id": projection.subject_person_id,
                "viewer_scope": projection.viewer_scope,
                "shareability": projection.shareability,
                "occurred_at": projection.occurred_at,
            }
            if any(row.get(key) != value for key, value in expected_columns.items()):
                raise ValueError("projection columns differ from signed content")
            evidence_refs = json.loads(str(row["evidence_refs_json"]))
            if evidence_refs != list(projection.evidence_refs):
                raise ValueError("projection evidence refs differ from signed content")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            sequence = row.get("event_seq", "unknown")
            raise ValueError(
                f"cognition evidence ledger integrity failure at {sequence}: {exc}"
            ) from exc

        row["projection"] = projection.payload()
        row["evidence_refs"] = evidence_refs
        row["applied_sinks"] = list(sinks)
        row.pop("projection_json", None)
        row.pop("evidence_refs_json", None)
        row.pop("applied_sinks_json", None)
        return row

    def trace(
        self,
        *,
        project_id: str = "",
        subject_person_id: str = "",
        viewer_scope: str = "owner",
        limit: int = 100,
    ) -> list[Dict[str, Any]]:
        bound = max(1, min(500, int(limit)))
        query = (
            "SELECT * FROM cognition_evidence_events "
            "WHERE viewer_scope=? AND projection_json IS NOT NULL"
        )
        params: list[Any] = [str(viewer_scope)]
        if project_id:
            query += " AND project_id=?"
            params.append(str(project_id))
        if subject_person_id:
            query += " AND subject_person_id=?"
            params.append(str(subject_person_id))
        query += " ORDER BY event_seq DESC LIMIT ?"
        params.append(bound)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._verified_trace_row(row) for row in rows]


class CognitionEvidenceReducer:
    """Reduce durable journal evidence into trusted, idempotent learning sinks."""

    def __init__(
        self,
        store: CognitionEvidenceStore,
        *,
        project_store: ProjectStore,
        self_model: Any = None,
        expectations: Any = None,
        project_event_projector: Any = None,
        consumer_id: str = _CONSUMER_ID,
        replay_fn: Callable[..., Mapping[str, Any]] = replay_events,
        current_sequence_fn: Callable[[], int] = current_sequence,
    ) -> None:
        self.store = store
        self.project_store = project_store
        self.self_model = self_model
        self.expectations = expectations
        self.project_event_projector = project_event_projector
        self.consumer_id = consumer_id
        self._replay = replay_fn
        self._current_sequence = current_sequence_fn

    @property
    def mode(self) -> str:
        return cognition_evidence_mode()

    @staticmethod
    def _replay_integrity_error(snapshot: Mapping[str, Any]) -> str:
        if snapshot.get("replayError"):
            return "event_journal_unavailable"
        try:
            corrupt = int(snapshot.get("corruptCount") or 0)
            first = int(snapshot.get("firstAvailableSeq") or 0)
            high = int(snapshot.get("journalLastSeq") or 0)
        except (TypeError, ValueError):
            return "event_journal_metadata_invalid"
        if corrupt < 0 or first < 0 or high < 0 or (first and first > high):
            return "event_journal_metadata_invalid"
        if corrupt:
            return f"journal_corruption_detected:{corrupt}"
        events = snapshot.get("events") or ()
        if not isinstance(events, (list, tuple)):
            return "event_journal_metadata_invalid"
        return ""

    @staticmethod
    def _gap_policy() -> str:
        return os.environ.get(
            "COLONY_COGNITION_EVIDENCE_GAP_POLICY", "stop",
        ).strip().lower()

    def _stop(self, error: str, *, outbox: Any, processed: int = 0,
              last_seq: Optional[int] = None) -> Dict[str, Any]:
        self.store.set_error(self.consumer_id, error)
        result: Dict[str, Any] = {
            "enabled": True,
            "mode": self.mode,
            "processed": int(processed),
            "error": error,
            "project_outbox": outbox,
        }
        if last_seq is not None:
            result["last_seq"] = int(last_seq)
        return result

    def _initialize(self) -> Dict[str, Any]:
        snapshot = self._replay(after_seq=0, limit=1)
        bootstrap = _bootstrap_mode()
        error = self._replay_integrity_error(snapshot)
        if error:
            initial = 0
        else:
            first = int(snapshot.get("firstAvailableSeq") or 0)
            high = int(snapshot.get("journalLastSeq") or 0)
            initial = high if bootstrap == "tail" else max(0, first - 1)
        cursor = self.store.initialize_cursor(
            self.consumer_id, initial, bootstrap_mode=bootstrap,
        )
        result = {
            "bootstrapped": True,
            "bootstrap_mode": bootstrap,
            "cursor": cursor,
        }
        if error:
            self.store.set_error(self.consumer_id, error)
            result["error"] = error
        return result

    def _apply_competence(self, projection: EvidenceProjectionV1) -> list[str]:
        if self.mode != "live" \
                or projection.local_evidence.get(
                    "cognition_mode_at_stage"
                ) != "live" \
                or projection.event_type != "work_order.result" \
                or projection.authority_state != "verified" \
                or projection.outcome == "neutral":
            return []
        if self.self_model is None:
            raise RuntimeError("verified cognition evidence has no self-model sink")
        event_key = f"journal:{projection.event_seq}:{projection.event_id}"
        source = "cognition_evidence_v1"
        recorded = self.self_model.record(
            projection.domain,
            projection.outcome,
            latency_secs=projection.latency_seconds,
            stated_confidence=projection.stated_confidence,
            source=source,
            source_ref=str(projection.local_evidence["attempt_ref"]),
            event_key=event_key,
            evidence_status="verified",
            outcome_contract="ExecutionResultV1:1",
            evidence={
                "event_ref": event_key,
                "project_id": projection.project_id,
                "step_id": projection.step_id,
                "work_order_id": projection.work_order_id,
                "result_ref": projection.result_ref,
                "evidence_refs": list(projection.evidence_refs),
                **dict(projection.local_evidence),
            },
        )
        competence_store = getattr(self.self_model, "store", None)
        durable = bool(
            competence_store is not None
            and callable(getattr(competence_store, "has_event_key", None))
            and competence_store.has_event_key(source, event_key)
        )
        if not recorded and not durable:
            raise RuntimeError("verified competence evidence was not made durable")
        return ["competence:project"]

    def _apply_expectations(self, projection: EvidenceProjectionV1) -> list[str]:
        if self.mode != "live" \
                or projection.local_evidence.get(
                    "cognition_mode_at_stage"
                ) != "live" \
                or projection.authority_state != "verified" \
                or projection.outcome == "neutral" \
                or self.expectations is None:
            return []
        if projection.event_type == "work_order.result":
            # A transient failed attempt is real competence evidence, but it
            # does not settle the logical task while its bounded retry budget
            # remains open.
            if projection.outcome in {"failure", "timeout"} and int(
                projection.local_evidence.get("attempt_number") or 0
            ) < int(projection.local_evidence.get("max_attempts") or 0):
                return []
            subjects = {
                f"task:{projection.step_id}",
                f"task:{projection.work_order_id}",
            }
            evidence_ref = str(projection.local_evidence["attempt_ref"])
        elif projection.event_type in {"project.completed", "project.abandoned"}:
            subjects = {f"project:{projection.project_id}"}
            evidence_ref = str(projection.local_evidence["terminal_ref"])
        else:
            return []
        predictions = self.expectations.store.for_subjects(subjects, limit=500)
        sinks: list[str] = []
        from colony_sidecar.self_model.expectations import OutcomeObservationV1

        for prediction in predictions:
            if prediction.schema_version != 2:
                continue
            if (
                prediction.subject_person_id != projection.subject_person_id
                or prediction.viewer_scope != projection.viewer_scope
                or prediction.shareability != projection.shareability
            ):
                continue
            identity = _digest({
                "event_id": projection.event_id,
                "prediction_id": prediction.prediction_id,
            })
            observation_id = f"eo-{identity[:24]}"
            # Replaying a journal event must not resolve a prediction that did
            # not exist when the outcome happened. Already-resolved rows are
            # retained only when this exact observation owns the resolution;
            # another attempt or resolver must not turn first-writer sealing
            # into a permanent evidence-pipeline failure.
            if prediction.created_at > projection.occurred_at:
                continue
            if prediction.outcome != "pending" and (
                prediction.outcome_observation_id != observation_id
            ):
                continue
            observation = OutcomeObservationV1.create(
                observation_id=observation_id,
                prediction_id=prediction.prediction_id,
                value=projection.outcome == "success",
                observed_at=projection.occurred_at,
                # The immutable attempt, not the mutable logical-result head,
                # is the canonical join. It retains the independently checked
                # result digest/run identity even after a later retry becomes
                # the WorkOrder's current logical result.
                evidence_refs=(evidence_ref,),
                source_kind="work_receipt",
                subject_person_id=projection.subject_person_id,
                viewer_scope=projection.viewer_scope,
                shareability=projection.shareability,
            )
            result = self.expectations.ingest_outcome(observation)
            if result.get("disposition") not in {"resolved", "duplicate"}:
                raise RuntimeError("expectation evidence did not resolve durably")
            sinks.append(f"expectation:{prediction.prediction_id}")
        return sinks

    def status(self) -> Dict[str, Any]:
        state = self.store.status(self.consumer_id)
        outbox = (
            self.project_event_projector.status()
            if self.project_event_projector is not None else None
        )
        high_water = int(self._current_sequence())
        cursor = state.get("cursor")
        state.update({
            "enabled": self.mode != "off",
            "mode": self.mode,
            "healthy": not bool(state.get("last_error")) and not bool(
                (outbox or {}).get("last_error")
            ),
            "journal_high_water": high_water,
            "journal_lag": (
                max(0, high_water - int(cursor)) if cursor is not None else None
            ),
            "project_outbox": outbox,
        })
        return state

    def run_once(self, *, limit: int = 100) -> Dict[str, Any]:
        outbox = (
            self.project_event_projector.run_once(limit=limit)
            if self.project_event_projector is not None else None
        )
        if self.mode == "off":
            # Off restores the legacy direct writer, so journal outcomes from
            # this interval must never be replayed as new receipt-derived
            # learning after live is re-enabled.  Keep only a durable cursor
            # range acknowledgement; do not validate, trace, or train events.
            high = int(self._current_sequence())
            cursor = self.store.cursor(self.consumer_id)
            skipped = 0
            if cursor is None:
                cursor = self.store.initialize_cursor(
                    self.consumer_id, 0, bootstrap_mode="off_passthrough",
                )
            if high < cursor:
                error = f"event_journal_rewind:{cursor}:{high}"
                self.store.set_error(self.consumer_id, error)
                return {
                    "enabled": False, "mode": "off", "processed": 0,
                    "cursor": cursor, "error": error,
                    "project_outbox": outbox,
                }
            elif high > cursor:
                prior = cursor
                cursor = self.store.checkpoint_passthrough(
                    self.consumer_id,
                    prior_cursor=prior,
                    resume_after=high,
                    reason=f"evidence_mode_off_passthrough:{prior}:{high}",
                )
                skipped = high - prior
            return {
                "enabled": False, "mode": "off", "processed": 0,
                "cursor": cursor, "passthrough_events": skipped,
                "project_outbox": outbox,
            }
        cursor = self.store.cursor(self.consumer_id)
        initialized: Dict[str, Any] = {}
        if cursor is None:
            initialized = self._initialize()
            cursor = int(initialized["cursor"])
            if initialized.get("error"):
                return self._stop(
                    str(initialized["error"]), outbox=outbox,
                    last_seq=cursor,
                )
            if initialized["bootstrap_mode"] == "tail":
                return {
                    **initialized, "enabled": True, "processed": 0,
                    "project_outbox": outbox,
                }
        batch = self._replay(
            after_seq=cursor, limit=max(1, min(500, int(limit))),
        )
        integrity_error = self._replay_integrity_error(batch)
        if integrity_error:
            return self._stop(
                integrity_error, outbox=outbox, last_seq=cursor,
            )
        journal_high = int(batch.get("journalLastSeq") or 0)
        if cursor > journal_high:
            return self._stop(
                f"event_journal_rewind:{cursor}:{journal_high}",
                outbox=outbox,
                last_seq=cursor,
            )
        first = int(batch.get("firstAvailableSeq") or 0)
        if first and cursor < first - 1:
            message = f"journal_retention_gap:{cursor}:{first}"
            if self._gap_policy() != "acknowledge":
                return self._stop(message, outbox=outbox, last_seq=cursor)
            cursor = self.store.acknowledge_gap(
                self.consumer_id,
                prior_cursor=cursor,
                resume_after=first - 1,
                reason=message,
            )
            batch = self._replay(
                after_seq=cursor, limit=max(1, min(500, int(limit))),
            )
            integrity_error = self._replay_integrity_error(batch)
            if integrity_error:
                return self._stop(
                    integrity_error, outbox=outbox, last_seq=cursor,
                )
            journal_high = int(batch.get("journalLastSeq") or 0)
            if cursor > journal_high:
                return self._stop(
                    f"event_journal_rewind:{cursor}:{journal_high}",
                    outbox=outbox,
                    last_seq=cursor,
                )

        counts: Dict[str, int] = {}
        last_seq = cursor
        for raw in batch.get("events") or []:
            try:
                if not isinstance(raw, Mapping):
                    raise ValueError("journal event is not an object")
                sequence = int(raw.get("seq") or 0)
                if sequence <= last_seq:
                    raise ValueError("journal event sequence is not increasing")
                if sequence != last_seq + 1:
                    message = f"journal_sequence_gap:{last_seq}:{sequence}"
                    if self._gap_policy() != "acknowledge":
                        return self._stop(
                            message,
                            outbox=outbox,
                            processed=sum(counts.values()),
                            last_seq=last_seq,
                        )
                    last_seq = self.store.acknowledge_gap(
                        self.consumer_id,
                        prior_cursor=last_seq,
                        resume_after=sequence - 1,
                        reason=message,
                    )
                sequence, event_id, event_type, _, _, raw_digest = _event_identity(raw)
                projection, skip_reason, _ = project_evidence_event(
                    raw, self.project_store,
                )
                sinks: list[str] = []
                disposition = skip_reason
                if projection is not None:
                    sinks.extend(self._apply_competence(projection))
                    sinks.extend(self._apply_expectations(projection))
                    disposition = projection.disposition
                applied = self.store.apply(
                    consumer_id=self.consumer_id,
                    event_seq=sequence,
                    event_id=event_id,
                    event_type=event_type,
                    raw_digest=raw_digest,
                    projection=projection,
                    disposition=disposition,
                    applied_sinks=sinks,
                )
            except Exception as exc:
                sequence = int(raw.get("seq") or 0) if isinstance(raw, Mapping) else 0
                if sequence < 1:
                    error = "malformed_event_without_sequence"
                else:
                    error = (
                        f"evidence_validation_failed:{sequence}:"
                        f"{type(exc).__name__}:{str(exc)[:200]}"
                    )
                self.store.set_error(self.consumer_id, error)
                return {
                    "enabled": True,
                    "mode": self.mode,
                    "processed": sum(counts.values()),
                    "last_seq": last_seq,
                    "error": error,
                    "project_outbox": outbox,
                }
            counts[disposition] = counts.get(disposition, 0) + 1
            if applied == "duplicate":
                counts["duplicates"] = counts.get("duplicates", 0) + 1
            last_seq = sequence
        journal_high = int(batch.get("journalLastSeq") or 0)
        if not bool(batch.get("hasMore")) and journal_high > last_seq:
            message = f"journal_sequence_gap:{last_seq}:{journal_high + 1}"
            if self._gap_policy() != "acknowledge":
                return self._stop(
                    message,
                    outbox=outbox,
                    processed=sum(counts.values()),
                    last_seq=last_seq,
                )
            last_seq = self.store.acknowledge_gap(
                self.consumer_id,
                prior_cursor=last_seq,
                resume_after=journal_high,
                reason=message,
            )
        return {
            **initialized,
            "enabled": True,
            "mode": self.mode,
            "processed": sum(
                value for key, value in counts.items() if key != "duplicates"
            ),
            "last_seq": last_seq,
            "dispositions": counts,
            "project_outbox": outbox,
        }


__all__ = [
    "CognitionEvidenceReducer",
    "CognitionEvidenceStore",
    "EvidenceProjectionV1",
    "cognition_evidence_enabled",
    "cognition_evidence_mode",
    "project_evidence_event",
]
