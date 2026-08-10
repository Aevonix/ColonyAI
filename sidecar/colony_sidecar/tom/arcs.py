"""P8 append-only conversational arcs.

An arc is not a mutable summary row.  It is a deterministic projection of an
append-only event ledger linking source turns, people, commitments,
expectations, and projects.  Deadlines can make an arc overdue, but only an
explicit terminal event with evidence can close it.

This core has no model calls and no startup or delivery integration.  Future
producers must supply stable event/idempotency identities and server-derived
scope; future readers must project through an attested ``ViewerContextV1``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterable, Optional, Sequence

from colony_sidecar.tom.visibility import (
    ViewerContextV1,
    validate_visibility_scope,
    visibility_scope_decision,
)


SCHEMA_VERSION = 1
ARC_TYPES = frozenset({
    "promise",
    "open_question",
    "stress_topic",
    "decision",
    "shared_plan",
    "follow_up",
    "unresolved_social_moment",
})
EVENT_KINDS = frozenset({
    "open", "link", "hold", "resume", "close", "cancel", "reopen",
})
ACTIVE_STATES = frozenset({"open", "held"})
TERMINAL_STATES = frozenset({"closed", "cancelled"})

MAX_PEOPLE = 16
MAX_REFS_PER_KIND = 64
MAX_TOPIC_CHARS = 500
MAX_CLOSURE_REASON_CHARS = 500
MAX_EVENTS_PER_ARC = 1_024
MAX_PROJECTED_ARCS = 64
MAX_PROJECTED_TOPIC_CHARS = 12_000
MAX_ARCS_SCANNED = 2_000

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,191}$")


class ArcConflictError(ValueError):
    """An event replay changed content or reused another idempotency key."""


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
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


def _refs(values: Iterable[Any], *, field: str,
          maximum: int = MAX_REFS_PER_KIND) -> tuple[str, ...]:
    normalized = tuple(sorted(dict.fromkeys(
        _ref(value, field=field) for value in values
    )))
    if len(normalized) > maximum:
        raise ValueError(f"{field} exceeds bounded reference limit")
    return normalized


def _as_utc(value: datetime | str, *, field: str,
            allow_empty: bool = False) -> Optional[datetime]:
    if allow_empty and not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | str, *, field: str,
         allow_empty: bool = False) -> str:
    parsed = _as_utc(value, field=field, allow_empty=allow_empty)
    return parsed.isoformat() if parsed is not None else ""


def _topic(value: Any) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized or len(normalized) > MAX_TOPIC_CHARS:
        raise ValueError("arc topic is required and bounded")
    return normalized


@dataclass(frozen=True, slots=True)
class ArcEventV1:
    event_id: str
    idempotency_key: str
    arc_id: str
    event_kind: str
    source_ref: str
    occurred_at: str
    subject_person_id: str
    arc_type: str = ""
    topic: str = ""
    people: tuple[str, ...] = ()
    turn_refs: tuple[str, ...] = ()
    commitment_refs: tuple[str, ...] = ()
    expectation_refs: tuple[str, ...] = ()
    project_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    viewer_scope: str = ""
    shareability: str = ""
    due_at: str = ""
    next_check_at: str = ""
    closure_reason: str = ""
    sequence: int = 0
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported arc event version")
        event_id = _ref(self.event_id, field="event_id")
        idempotency = _ref(self.idempotency_key, field="idempotency_key")
        arc_id = _ref(self.arc_id, field="arc_id")
        source_ref = _ref(self.source_ref, field="source_ref")
        kind = str(self.event_kind or "").strip().lower()
        if kind not in EVENT_KINDS:
            raise ValueError("unknown arc event kind")
        occurred_at = _iso(self.occurred_at, field="occurred_at")
        people = _refs(self.people, field="person", maximum=MAX_PEOPLE)
        turn_refs = _refs(self.turn_refs, field="turn_ref")
        commitment_refs = _refs(
            self.commitment_refs, field="commitment_ref")
        expectation_refs = _refs(
            self.expectation_refs, field="expectation_ref")
        project_refs = _refs(self.project_refs, field="project_ref")
        evidence_refs = _refs(self.evidence_refs, field="evidence_ref")
        due_at = _iso(self.due_at, field="due_at", allow_empty=True)
        next_check_at = _iso(
            self.next_check_at, field="next_check_at", allow_empty=True)
        sequence = int(self.sequence)
        if sequence < 0:
            raise ValueError("arc event sequence cannot be negative")

        arc_type = str(self.arc_type or "").strip().lower()
        topic = ""
        viewer_scope, shareability, subject = validate_visibility_scope(
            viewer_scope=str(self.viewer_scope or "").strip(),
            shareability=str(self.shareability or "").strip().lower(),
            subject_person_id=self.subject_person_id,
        )
        closure_reason = " ".join(str(self.closure_reason or "").split())

        if kind == "open":
            if arc_type not in ARC_TYPES:
                raise ValueError("unknown conversational arc type")
            topic = _topic(self.topic)
            if not people:
                raise ValueError("open arc requires at least one person")
            if not turn_refs:
                raise ValueError("open arc requires a source turn")
            if not evidence_refs:
                raise ValueError("open arc requires source evidence")
            if subject not in people:
                raise ValueError("arc subject must be one of its people")
            if closure_reason:
                raise ValueError("open arc cannot carry a closure reason")
        else:
            if arc_type or self.topic:
                raise ValueError(
                    "non-open arc events cannot replace arc identity fields")
            if kind == "link" and not any((
                people, turn_refs, commitment_refs, expectation_refs,
                project_refs, evidence_refs, due_at, next_check_at,
            )):
                raise ValueError("link event must add bounded evidence or links")
            if kind in {"hold", "resume", "reopen"} and not evidence_refs:
                raise ValueError(f"{kind} event requires evidence")
            if kind in {"close", "cancel"}:
                if not evidence_refs:
                    raise ValueError("arc closure evidence is required")
                if not closure_reason \
                        or len(closure_reason) > MAX_CLOSURE_REASON_CHARS:
                    raise ValueError("arc closure reason is required and bounded")
            elif closure_reason:
                raise ValueError("only terminal events carry closure reason")

        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "idempotency_key", idempotency)
        object.__setattr__(self, "arc_id", arc_id)
        object.__setattr__(self, "event_kind", kind)
        object.__setattr__(self, "source_ref", source_ref)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "subject_person_id", subject)
        object.__setattr__(self, "arc_type", arc_type)
        object.__setattr__(self, "topic", topic)
        object.__setattr__(self, "people", people)
        object.__setattr__(self, "turn_refs", turn_refs)
        object.__setattr__(self, "commitment_refs", commitment_refs)
        object.__setattr__(self, "expectation_refs", expectation_refs)
        object.__setattr__(self, "project_refs", project_refs)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "viewer_scope", viewer_scope)
        object.__setattr__(self, "shareability", shareability)
        object.__setattr__(self, "due_at", due_at)
        object.__setattr__(self, "next_check_at", next_check_at)
        object.__setattr__(self, "closure_reason", closure_reason)
        object.__setattr__(self, "sequence", sequence)

    @classmethod
    def open(
        cls,
        *,
        event_id: str,
        idempotency_key: str,
        arc_id: str,
        arc_type: str,
        topic: str,
        people: Sequence[str],
        source_turn_ref: str,
        source_ref: str,
        viewer_scope: str,
        shareability: str,
        subject_person_id: str,
        evidence_refs: Sequence[str],
        occurred_at: str,
        commitment_refs: Sequence[str] = (),
        expectation_refs: Sequence[str] = (),
        project_refs: Sequence[str] = (),
        due_at: str = "",
        next_check_at: str = "",
    ) -> "ArcEventV1":
        return cls(
            event_id=event_id,
            idempotency_key=idempotency_key,
            arc_id=arc_id,
            event_kind="open",
            source_ref=source_ref,
            occurred_at=occurred_at,
            subject_person_id=subject_person_id,
            arc_type=arc_type,
            topic=topic,
            people=tuple(people),
            turn_refs=(source_turn_ref,),
            commitment_refs=tuple(commitment_refs),
            expectation_refs=tuple(expectation_refs),
            project_refs=tuple(project_refs),
            evidence_refs=tuple(evidence_refs),
            viewer_scope=viewer_scope,
            shareability=shareability,
            due_at=due_at,
            next_check_at=next_check_at,
        )

    @classmethod
    def link(
        cls,
        *,
        event_id: str,
        idempotency_key: str,
        arc_id: str,
        source_ref: str,
        subject_person_id: str,
        viewer_scope: str,
        shareability: str,
        occurred_at: str,
        people: Sequence[str] = (),
        turn_refs: Sequence[str] = (),
        commitment_refs: Sequence[str] = (),
        expectation_refs: Sequence[str] = (),
        project_refs: Sequence[str] = (),
        evidence_refs: Sequence[str] = (),
        due_at: str = "",
        next_check_at: str = "",
    ) -> "ArcEventV1":
        return cls(
            event_id=event_id,
            idempotency_key=idempotency_key,
            arc_id=arc_id,
            event_kind="link",
            source_ref=source_ref,
            occurred_at=occurred_at,
            subject_person_id=subject_person_id,
            people=tuple(people),
            turn_refs=tuple(turn_refs),
            commitment_refs=tuple(commitment_refs),
            expectation_refs=tuple(expectation_refs),
            project_refs=tuple(project_refs),
            evidence_refs=tuple(evidence_refs),
            due_at=due_at,
            next_check_at=next_check_at,
            viewer_scope=viewer_scope,
            shareability=shareability,
        )

    @classmethod
    def _transition(
        cls,
        kind: str,
        *,
        event_id: str,
        idempotency_key: str,
        arc_id: str,
        source_ref: str,
        subject_person_id: str,
        viewer_scope: str,
        shareability: str,
        evidence_refs: Sequence[str],
        occurred_at: str,
        closure_reason: str = "",
    ) -> "ArcEventV1":
        return cls(
            event_id=event_id,
            idempotency_key=idempotency_key,
            arc_id=arc_id,
            event_kind=kind,
            source_ref=source_ref,
            occurred_at=occurred_at,
            subject_person_id=subject_person_id,
            evidence_refs=tuple(evidence_refs),
            viewer_scope=viewer_scope,
            shareability=shareability,
            closure_reason=closure_reason,
        )

    @classmethod
    def close(cls, **kwargs: Any) -> "ArcEventV1":
        return cls._transition("close", **kwargs)

    @classmethod
    def cancel(cls, **kwargs: Any) -> "ArcEventV1":
        return cls._transition("cancel", **kwargs)

    @classmethod
    def hold(cls, **kwargs: Any) -> "ArcEventV1":
        return cls._transition("hold", **kwargs)

    @classmethod
    def resume(cls, **kwargs: Any) -> "ArcEventV1":
        return cls._transition("resume", **kwargs)

    @classmethod
    def reopen(cls, **kwargs: Any) -> "ArcEventV1":
        return cls._transition("reopen", **kwargs)

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "idempotency_key": self.idempotency_key,
            "arc_id": self.arc_id,
            "event_kind": self.event_kind,
            "source_ref": self.source_ref,
            "occurred_at": self.occurred_at,
            "subject_person_id": self.subject_person_id,
            "arc_type": self.arc_type,
            "topic": self.topic,
            "people": list(self.people),
            "turn_refs": list(self.turn_refs),
            "commitment_refs": list(self.commitment_refs),
            "expectation_refs": list(self.expectation_refs),
            "project_refs": list(self.project_refs),
            "evidence_refs": list(self.evidence_refs),
            "viewer_scope": self.viewer_scope,
            "shareability": self.shareability,
            "due_at": self.due_at,
            "next_check_at": self.next_check_at,
            "closure_reason": self.closure_reason,
        }

    @property
    def event_digest(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True, slots=True)
class Arc:
    arc_id: str
    arc_type: str
    topic: str
    subject_person_id: str
    people: tuple[str, ...]
    viewer_scope: str
    shareability: str
    state: str
    turn_refs: tuple[str, ...]
    commitment_refs: tuple[str, ...]
    expectation_refs: tuple[str, ...]
    project_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    closure_evidence_refs: tuple[str, ...]
    due_at: str
    next_check_at: str
    opened_at: str
    updated_at: str
    closed_at: str
    closure_reason: str
    event_count: int
    revision: int
    history_digest: str
    overdue: bool
    needs_check: bool
    schema_version: int = SCHEMA_VERSION

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    def visible_to(self, viewer: ViewerContextV1) -> bool:
        allowed, _ = visibility_scope_decision(
            viewer_scope=self.viewer_scope,
            shareability=self.shareability,
            subject_person_id=self.subject_person_id,
            viewer=viewer,
        )
        return allowed

    def public(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "arc_id": self.arc_id,
            "arc_type": self.arc_type,
            "topic": self.topic,
            "subject_person_id": self.subject_person_id,
            "people": list(self.people),
            "viewer_scope": self.viewer_scope,
            "shareability": self.shareability,
            "state": self.state,
            "turn_refs": list(self.turn_refs),
            "commitment_refs": list(self.commitment_refs),
            "expectation_refs": list(self.expectation_refs),
            "project_refs": list(self.project_refs),
            "evidence_refs": list(self.evidence_refs),
            "closure_evidence_refs": list(self.closure_evidence_refs),
            "due_at": self.due_at,
            "next_check_at": self.next_check_at,
            "opened_at": self.opened_at,
            "updated_at": self.updated_at,
            "closed_at": self.closed_at,
            "closure_reason": self.closure_reason,
            "event_count": self.event_count,
            "revision": self.revision,
            "history_digest": self.history_digest,
            "overdue": self.overdue,
            "needs_check": self.needs_check,
        }


class ArcReducer:
    """Strict deterministic event reducer; invalid history never projects."""

    @staticmethod
    def reduce(
        events: Sequence[ArcEventV1],
        *,
        now: datetime | str | None = None,
    ) -> Arc:
        if not events:
            raise ValueError("arc reducer requires an open event")
        if len(events) > MAX_EVENTS_PER_ARC:
            raise ValueError("arc event history exceeds bounded limit")
        ordered = list(events)
        if any(event.sequence for event in ordered):
            if not all(event.sequence for event in ordered):
                raise ValueError("arc history mixes sequenced and unsequenced events")
            ordered.sort(key=lambda event: event.sequence)
        if ordered[0].event_kind != "open":
            raise ValueError("arc history must begin with an open event")
        if sum(1 for event in ordered if event.event_kind == "open") != 1:
            raise ValueError("arc history must contain exactly one open event")
        arc_ids = {event.arc_id for event in ordered}
        if len(arc_ids) != 1:
            raise ValueError("arc history cannot mix arc identities")

        opened = ordered[0]
        state = "open"
        people = set(opened.people)
        turn_refs = set(opened.turn_refs)
        commitment_refs = set(opened.commitment_refs)
        expectation_refs = set(opened.expectation_refs)
        project_refs = set(opened.project_refs)
        evidence_refs = set(opened.evidence_refs)
        closure_evidence: set[str] = set()
        due_at = opened.due_at
        next_check_at = opened.next_check_at
        closed_at = ""
        closure_reason = ""
        updated_at = opened.occurred_at

        for event in ordered[1:]:
            if (
                event.viewer_scope != opened.viewer_scope
                or event.shareability != opened.shareability
            ):
                raise ValueError(
                    "arc event visibility must exactly match its open event")
            if state in TERMINAL_STATES and event.event_kind != "reopen":
                raise ValueError("terminal arc cannot accept non-reopen events")
            kind = event.event_kind
            if kind == "link":
                people.update(event.people)
                if len(people) > MAX_PEOPLE:
                    raise ValueError("arc people exceed bounded limit")
                turn_refs.update(event.turn_refs)
                commitment_refs.update(event.commitment_refs)
                expectation_refs.update(event.expectation_refs)
                project_refs.update(event.project_refs)
                evidence_refs.update(event.evidence_refs)
                due_at = event.due_at or due_at
                next_check_at = event.next_check_at or next_check_at
            elif kind == "hold":
                if state != "open":
                    raise ValueError("only an open arc can be held")
                state = "held"
                evidence_refs.update(event.evidence_refs)
            elif kind == "resume":
                if state != "held":
                    raise ValueError("only a held arc can resume")
                state = "open"
                evidence_refs.update(event.evidence_refs)
            elif kind in {"close", "cancel"}:
                if state not in ACTIVE_STATES:
                    raise ValueError("only an active arc can close")
                state = "closed" if kind == "close" else "cancelled"
                closure_evidence.update(event.evidence_refs)
                evidence_refs.update(event.evidence_refs)
                closure_reason = event.closure_reason
                closed_at = event.occurred_at
            elif kind == "reopen":
                if state not in TERMINAL_STATES:
                    raise ValueError("only a terminal arc can reopen")
                state = "open"
                evidence_refs.update(event.evidence_refs)
                closed_at = ""
                closure_reason = ""
            else:
                raise ValueError(f"unhandled arc event kind {kind}")
            if event.subject_person_id not in people:
                raise ValueError(
                    "arc event subject must be one of the linked people")
            for field, references in (
                ("turn_refs", turn_refs),
                ("commitment_refs", commitment_refs),
                ("expectation_refs", expectation_refs),
                ("project_refs", project_refs),
                ("evidence_refs", evidence_refs),
                ("closure_evidence_refs", closure_evidence),
            ):
                if len(references) > MAX_REFS_PER_KIND:
                    raise ValueError(f"arc {field} exceeds bounded limit")
            updated_at = event.occurred_at

        observed = _as_utc(
            now or datetime.now(timezone.utc), field="now")
        assert observed is not None
        due = _as_utc(due_at, field="due_at", allow_empty=True)
        check = _as_utc(
            next_check_at, field="next_check_at", allow_empty=True)
        active = state in ACTIVE_STATES
        return Arc(
            arc_id=opened.arc_id,
            arc_type=opened.arc_type,
            topic=opened.topic,
            subject_person_id=opened.subject_person_id,
            people=tuple(sorted(people)),
            viewer_scope=opened.viewer_scope,
            shareability=opened.shareability,
            state=state,
            turn_refs=tuple(sorted(turn_refs)),
            commitment_refs=tuple(sorted(commitment_refs)),
            expectation_refs=tuple(sorted(expectation_refs)),
            project_refs=tuple(sorted(project_refs)),
            evidence_refs=tuple(sorted(evidence_refs)),
            closure_evidence_refs=tuple(sorted(closure_evidence)),
            due_at=due_at,
            next_check_at=next_check_at,
            opened_at=opened.occurred_at,
            updated_at=updated_at,
            closed_at=closed_at,
            closure_reason=closure_reason,
            event_count=len(ordered),
            revision=max((event.sequence for event in ordered), default=0)
            or len(ordered),
            history_digest=_digest([
                event.event_digest for event in ordered]),
            overdue=bool(active and due is not None and observed >= due),
            needs_check=bool(active and check is not None and observed >= check),
        )


@dataclass(frozen=True, slots=True)
class ArcAppendResultV1:
    event: ArcEventV1
    appended: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class ArcProjectionBatchV1:
    arcs: tuple[Arc, ...]
    denied_count: int
    corrupt_count: int
    truncated: bool
    viewer_digest: str
    audit_digest: str

    def public(self) -> dict[str, Any]:
        return {
            "arcs": [arc.public() for arc in self.arcs],
            "denied_count": self.denied_count,
            "corrupt_count": self.corrupt_count,
            "truncated": self.truncated,
            "viewer_digest": self.viewer_digest,
            "audit_digest": self.audit_digest,
        }


class ArcStore:
    """SQLite append-only event ledger with transactional idempotency."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.path = db_path
        if db_path != ":memory:":
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.touch(mode=0o600)
            else:
                os.chmod(path, 0o600)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS arc_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                idempotency_key TEXT NOT NULL UNIQUE,
                arc_id TEXT NOT NULL,
                event_kind TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                event_digest TEXT NOT NULL,
                stored_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_arc_events_arc
                ON arc_events(arc_id,seq);
            CREATE TRIGGER IF NOT EXISTS arc_events_no_update
                BEFORE UPDATE ON arc_events BEGIN
                    SELECT RAISE(ABORT, 'arc events are append-only');
                END;
            CREATE TRIGGER IF NOT EXISTS arc_events_no_delete
                BEFORE DELETE ON arc_events BEGIN
                    SELECT RAISE(ABORT, 'arc events are append-only');
                END;
            """
        )
        self._conn.commit()
        if db_path != ":memory:":
            os.chmod(db_path, 0o600)

    @staticmethod
    def _decode(row: sqlite3.Row) -> ArcEventV1:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError) as exc:
            raise ValueError("arc event payload is unreadable") from exc
        event = ArcEventV1(
            **payload,
            sequence=int(row["seq"]),
        )
        if event.event_digest != row["event_digest"]:
            raise ValueError("arc event digest mismatch")
        return event

    def _events_locked(self, arc_id: str) -> list[ArcEventV1]:
        rows = self._conn.execute(
            "SELECT * FROM arc_events WHERE arc_id=? ORDER BY seq",
            (arc_id,),
        ).fetchall()
        return [self._decode(row) for row in rows]

    def append(self, event: ArcEventV1) -> ArcAppendResultV1:
        if not isinstance(event, ArcEventV1):
            raise ValueError("ArcStore accepts ArcEventV1 only")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT * FROM arc_events WHERE event_id=? "
                    "OR idempotency_key=? ORDER BY seq",
                    (event.event_id, event.idempotency_key),
                ).fetchall()
                if rows:
                    for row in rows:
                        saved = self._decode(row)
                        if (
                            saved.event_id == event.event_id
                            and saved.idempotency_key == event.idempotency_key
                            and saved.event_digest == event.event_digest
                        ):
                            self._conn.commit()
                            return ArcAppendResultV1(
                                event=saved, appended=False, replayed=True)
                    raise ArcConflictError(
                        "arc event idempotency identity changed immutable content")

                # Reject an event that would make the ledger unreducible before
                # it is persisted.  BEGIN IMMEDIATE reserves the next sequence,
                # so validation sees the same fully-sequenced history that a
                # subsequent reader will reduce.
                existing = self._events_locked(event.arc_id)
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq),0) + 1 AS next_seq "
                    "FROM arc_events"
                ).fetchone()
                next_sequence = int(row["next_seq"] if row else 1)
                ArcReducer.reduce(
                    [*existing, replace(event, sequence=next_sequence)],
                    now=event.occurred_at,
                )
                cursor = self._conn.execute(
                    "INSERT INTO arc_events "
                    "(event_id,idempotency_key,arc_id,event_kind,source_ref,"
                    "occurred_at,payload_json,event_digest,stored_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_id,
                        event.idempotency_key,
                        event.arc_id,
                        event.event_kind,
                        event.source_ref,
                        event.occurred_at,
                        _canonical(event.payload()),
                        event.event_digest,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                sequence = int(cursor.lastrowid)
                if sequence != next_sequence:
                    raise RuntimeError("arc ledger sequence allocation changed")
                self._conn.commit()
                return ArcAppendResultV1(
                    event=replace(event, sequence=sequence),
                    appended=True,
                    replayed=False,
                )
            except Exception:
                self._conn.rollback()
                raise

    def events(self, arc_id: str) -> tuple[ArcEventV1, ...]:
        normalized = _ref(arc_id, field="arc_id")
        with self._lock:
            return tuple(self._events_locked(normalized))

    def event_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM arc_events").fetchone()
        return int(row["n"] if row else 0)

    def get_arc(
        self,
        arc_id: str,
        *,
        now: datetime | str | None = None,
    ) -> Optional[Arc]:
        events = self.events(arc_id)
        return ArcReducer.reduce(events, now=now) if events else None

    def project_active(
        self,
        viewer: ViewerContextV1,
        *,
        now: datetime | str,
        max_arcs: int = 24,
        max_topic_chars: int = 8_000,
    ) -> ArcProjectionBatchV1:
        if not 1 <= int(max_arcs) <= MAX_PROJECTED_ARCS:
            raise ValueError("max_arcs is outside bounded projection range")
        if not 1 <= int(max_topic_chars) <= MAX_PROJECTED_TOPIC_CHARS:
            raise ValueError("max_topic_chars is outside bounded projection range")
        with self._lock:
            rows = self._conn.execute(
                "SELECT arc_id,MAX(seq) AS last_seq FROM arc_events "
                "GROUP BY arc_id ORDER BY last_seq DESC LIMIT ?",
                (MAX_ARCS_SCANNED + 1,),
            ).fetchall()
            arc_ids = [str(row["arc_id"]) for row in rows]
        scan_truncated = len(arc_ids) > MAX_ARCS_SCANNED
        arc_ids = arc_ids[:MAX_ARCS_SCANNED]
        eligible: list[Arc] = []
        denied_count = 0
        corrupt_count = 0
        for arc_id in arc_ids:
            try:
                arc = self.get_arc(arc_id, now=now)
            except Exception:
                corrupt_count += 1
                continue
            if arc is None or not arc.active:
                continue
            if not arc.visible_to(viewer):
                denied_count += 1
                continue
            eligible.append(arc)
        eligible.sort(key=lambda arc: (arc.updated_at, arc.arc_id), reverse=True)

        projected: list[Arc] = []
        chars = 0
        truncated = scan_truncated
        for arc in eligible:
            if len(projected) >= int(max_arcs) \
                    or chars + len(arc.topic) > int(max_topic_chars):
                truncated = True
                continue
            projected.append(arc)
            chars += len(arc.topic)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "viewer_digest": viewer.audit_digest,
            "arc_digests": [arc.history_digest for arc in projected],
            "denied_count": denied_count,
            "corrupt_count": corrupt_count,
            "truncated": truncated,
            "now": _iso(now, field="now"),
            "max_arcs": int(max_arcs),
            "max_topic_chars": int(max_topic_chars),
        }
        return ArcProjectionBatchV1(
            arcs=tuple(projected),
            denied_count=denied_count,
            corrupt_count=corrupt_count,
            truncated=truncated,
            viewer_digest=viewer.audit_digest,
            audit_digest=_digest(payload),
        )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


__all__ = [
    "ACTIVE_STATES",
    "ARC_TYPES",
    "Arc",
    "ArcAppendResultV1",
    "ArcConflictError",
    "ArcEventV1",
    "ArcProjectionBatchV1",
    "ArcReducer",
    "ArcStore",
]
