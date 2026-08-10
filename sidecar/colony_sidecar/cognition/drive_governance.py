"""P7 owner-visible drives and immutable charter governance.

This module is a side-effect-free priority layer around P3's goal spine.  It
does not create projects, execute actions, or mint authority.  A ranking is
considered eligible only after the server resolves all five persisted P3
policy decisions, and every result explicitly declares that it has no
authorization effect.

Charter changes reuse Colony's transport-neutral ``ApprovalAuthorityStore``.
Models may propose immutable revisions, but activation and revocation require
an exact, unexpired approval request plus the server-derived scoped owner
``RequestAuthority``.  Lifecycle state is append-only and every proposal and
ratification operation is replay fenced.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

from colony_sidecar.initiatives.approval_authority import (
    ActionBinding,
    ApprovalAuthorityStore,
    ApprovalSubjectBinding,
    PRESENTATION_SCHEMA,
)
from colony_sidecar.scope_bounds import VIEWER_SCOPE_MAX_CHARS


DRIVE_GOVERNANCE_SCHEMA_VERSION = 7
_MODES = frozenset({"off", "shadow", "bootstrap", "live"})
_SHAREABILITY = frozenset({
    "owner_private", "subject_private", "shared", "public",
})
_DRIVE_STATES = frozenset({"enabled", "disabled"})
_SIGNAL_STATES = frozenset({"active", "disabled", "unknown"})
_TRANSITIONS = frozenset({"activate", "revoke"})
CHARTER_TRANSITION_APPROVAL_PROJECTION_SCHEMA = (
    "ColonyCharterTransitionApprovalProjectionV1"
)
_REQUIRED_P3_STAGES = (
    "charter", "boundary", "situation", "duplicate", "authority",
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,255}$")
_SAFE_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|bearer|credential|password|secret|token)"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)


class DriveGovernanceError(ValueError):
    """Stable, public-safe governance failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def drive_governance_mode() -> str:
    """Return the explicit migration mode; unset/invalid always fails off."""

    value = os.environ.get("COLONY_DRIVE_GOVERNANCE_MODE", "off").strip().lower()
    return value if value in _MODES else "off"


def _normalized_mode(value: Optional[str]) -> str:
    if value is None:
        return drive_governance_mode()
    normalized = str(value).strip().lower()
    return normalized if normalized in _MODES else "off"


def _canonical(value: Any) -> str:
    def default(item: Any) -> str:
        if isinstance(item, datetime):
            return _iso(item)
        return str(item)

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=default,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _as_utc(value: Optional[datetime]) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_time(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise DriveGovernanceError(
            "invalid_time", f"{field} must be an RFC3339 timestamp",
        ) from exc
    if parsed.tzinfo is None:
        raise DriveGovernanceError(
            "invalid_time", f"{field} must include a timezone",
        )
    return parsed.astimezone(timezone.utc)


def _text(value: Any, field: str, maximum: int, *, required: bool = True) -> str:
    result = " ".join(str(value or "").split()).strip()
    if required and not result:
        raise DriveGovernanceError("invalid_text", f"{field} is required")
    if len(result) > maximum:
        raise DriveGovernanceError(
            "invalid_text", f"{field} exceeds {maximum} characters",
        )
    return _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[REDACTED]", result,
    )


def _identifier(value: Any, field: str, *, maximum: int = 256) -> str:
    result = str(value or "").strip()
    if len(result) > maximum or not _SAFE_ID.fullmatch(result):
        raise DriveGovernanceError("invalid_identifier", f"{field} is invalid")
    return result


def _operation_id(value: Any) -> str:
    result = _identifier(value, "operation_id")
    if len(result) < 8:
        raise DriveGovernanceError(
            "invalid_operation_id", "operation_id must be at least 8 characters",
        )
    return result


def _refs(
    values: Iterable[Any],
    *,
    field: str = "evidence_refs",
    maximum: int = 40,
    required: bool = False,
) -> tuple[str, ...]:
    result: list[str] = []
    for raw in values or ():
        value = str(raw or "").strip()
        if not _SAFE_ID.fullmatch(value):
            raise DriveGovernanceError(
                "invalid_evidence", f"{field} contains an invalid reference",
            )
        if value not in result:
            result.append(value)
        if len(result) > maximum:
            raise DriveGovernanceError(
                "evidence_budget_exceeded",
                f"{field} exceeds {maximum} references",
            )
    if required and not result:
        raise DriveGovernanceError(
            "evidence_required", f"{field} requires evidence",
        )
    return tuple(result)


def _bounded_float(
    value: Any,
    field: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool):
        raise DriveGovernanceError("invalid_number", f"{field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise DriveGovernanceError(
            "invalid_number", f"{field} must be numeric",
        ) from exc
    if not minimum <= result <= maximum:
        raise DriveGovernanceError(
            "invalid_number", f"{field} must be between {minimum} and {maximum}",
        )
    return round(result, 8)


def _bounded_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise DriveGovernanceError("invalid_number", f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise DriveGovernanceError(
            "invalid_number", f"{field} must be an integer",
        ) from exc
    if result != value and not isinstance(value, str):
        raise DriveGovernanceError("invalid_number", f"{field} must be an integer")
    if not minimum <= result <= maximum:
        raise DriveGovernanceError(
            "invalid_number", f"{field} must be between {minimum} and {maximum}",
        )
    return result


@dataclass(frozen=True)
class ScopeV1:
    """Scope carried from evidence through signals and ranking projections."""

    subject_person_id: str
    viewer_scope: str
    shareability: str
    schema: str = "ScopeV1"
    version: int = 1

    def __post_init__(self) -> None:
        _identifier(self.subject_person_id, "subject_person_id", maximum=128)
        _identifier(
            self.viewer_scope, "viewer_scope",
            maximum=VIEWER_SCOPE_MAX_CHARS,
        )
        if self.shareability not in _SHAREABILITY:
            raise DriveGovernanceError(
                "invalid_scope", "shareability is not recognized",
            )
        expected = {
            "owner_private": "owner",
            "subject_private": f"person:{self.subject_person_id}",
            "shared": "shared",
            "public": "public",
        }[self.shareability]
        if self.viewer_scope != expected:
            raise DriveGovernanceError(
                "invalid_scope",
                "viewer_scope must match the declared shareability lane",
            )

    def payload(self) -> Dict[str, Any]:
        return asdict(self)

    def visible_to(
        self,
        *,
        viewer_person_id: str,
        owner_person_id: str,
        audiences: set[str] | frozenset[str] = frozenset(),
    ) -> bool:
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
            return viewer == self.subject_person_id
        return False

    def permits_child(self, child: "ScopeV1") -> bool:
        """Return whether ``child`` is no broader than this scope."""

        if self.shareability == "public":
            return True
        if self.shareability == "shared":
            return child.shareability in {
                "shared", "owner_private",
            }
        if self.shareability == "owner_private":
            return child.shareability == "owner_private"
        return (
            child.shareability == "owner_private"
            or (
                child.shareability == "subject_private"
                and child.subject_person_id == self.subject_person_id
            )
        )


def _narrower_scope(first: ScopeV1, second: ScopeV1) -> ScopeV1:
    if first.permits_child(second):
        return second
    if second.permits_child(first):
        return first
    # Incomparable private lanes can only be combined into an owner-visible
    # projection; this prevents a score from leaking that a private signal
    # exists to either subject.
    subject = first.subject_person_id or second.subject_person_id
    return ScopeV1(subject, "owner", "owner_private")


@dataclass(frozen=True)
class RankingBudgetV1:
    max_goals: int = 50
    max_signals_per_drive: int = 5
    max_total_signals: int = 250
    max_evidence_refs_per_goal: int = 20
    schema: str = "RankingBudgetV1"
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "max_goals",
            _bounded_int(self.max_goals, "max_goals", minimum=1, maximum=200),
        )
        object.__setattr__(
            self, "max_signals_per_drive",
            _bounded_int(
                self.max_signals_per_drive,
                "max_signals_per_drive", minimum=1, maximum=20,
            ),
        )
        object.__setattr__(
            self, "max_total_signals",
            _bounded_int(
                self.max_total_signals,
                "max_total_signals", minimum=1, maximum=2000,
            ),
        )
        object.__setattr__(
            self, "max_evidence_refs_per_goal",
            _bounded_int(
                self.max_evidence_refs_per_goal,
                "max_evidence_refs_per_goal", minimum=1, maximum=100,
            ),
        )

    def payload(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DriveV1:
    drive_id: str
    definition_digest: str
    key: str
    version_label: str
    title: str
    definition_summary: str
    max_abs_contribution: float
    max_signals_per_goal: int
    state: str
    scope: ScopeV1
    evidence_refs: tuple[str, ...]
    created_at: str
    expires_at: Optional[str]
    schema: str = "DriveV1"
    version: int = 1

    @classmethod
    def create(
        cls,
        *,
        key: str,
        version: str,
        title: str,
        definition_summary: str,
        max_abs_contribution: float,
        max_signals_per_goal: int,
        state: str,
        scope: ScopeV1,
        evidence_refs: Iterable[str],
        created_at: Optional[datetime] = None,
        expires_at: Optional[datetime] = None,
    ) -> "DriveV1":
        key = str(key or "").strip().lower()
        if not _SAFE_KEY.fullmatch(key):
            raise DriveGovernanceError("invalid_drive_key", "drive key is invalid")
        version_label = _identifier(version, "drive version", maximum=32)
        normalized_state = str(state or "").strip().lower()
        if normalized_state not in _DRIVE_STATES:
            raise DriveGovernanceError("invalid_drive_state", "drive state is invalid")
        created = _as_utc(created_at)
        expiry = _as_utc(expires_at) if expires_at is not None else None
        if expiry is not None and expiry <= created:
            raise DriveGovernanceError(
                "invalid_expiry", "drive expiry must follow creation",
            )
        authority = {
            "schema": "DriveV1", "version": 1,
            "key": key,
            "version_label": version_label,
            "title": _text(title, "title", 120),
            "definition_summary": _text(
                definition_summary, "definition_summary", 600,
            ),
            "max_abs_contribution": _bounded_float(
                max_abs_contribution, "max_abs_contribution",
                minimum=0.0, maximum=1.0,
            ),
            "max_signals_per_goal": _bounded_int(
                max_signals_per_goal, "max_signals_per_goal",
                minimum=1, maximum=20,
            ),
            "state": normalized_state,
            "scope": scope.payload(),
            "evidence_refs": list(_refs(
                evidence_refs, required=True, maximum=20,
            )),
            "created_at": _iso(created),
            "expires_at": _iso(expiry) if expiry else None,
        }
        digest = _digest(authority)
        return cls(
            drive_id=f"drive:{key}:{digest[:20]}",
            definition_digest=digest,
            key=key,
            version_label=version_label,
            title=authority["title"],
            definition_summary=authority["definition_summary"],
            max_abs_contribution=authority["max_abs_contribution"],
            max_signals_per_goal=authority["max_signals_per_goal"],
            state=normalized_state,
            scope=scope,
            evidence_refs=tuple(authority["evidence_refs"]),
            created_at=authority["created_at"],
            expires_at=authority["expires_at"],
        )

    def authority_payload(self) -> Dict[str, Any]:
        return {
            "schema": self.schema, "version": self.version,
            "key": self.key,
            "version_label": self.version_label,
            "title": self.title,
            "definition_summary": self.definition_summary,
            "max_abs_contribution": self.max_abs_contribution,
            "max_signals_per_goal": self.max_signals_per_goal,
            "state": self.state,
            "scope": self.scope.payload(),
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    def payload(self) -> Dict[str, Any]:
        return {
            **self.authority_payload(),
            "drive_id": self.drive_id,
            "definition_digest": self.definition_digest,
        }

    def validate_integrity(self) -> None:
        if self.schema != "DriveV1" or self.version != 1:
            raise DriveGovernanceError(
                "unsupported_schema", "drive definition schema is unsupported",
            )
        if not _SAFE_KEY.fullmatch(str(self.key or "")):
            raise DriveGovernanceError("invalid_drive_key", "drive key is invalid")
        _identifier(self.version_label, "drive version", maximum=32)
        if _text(self.title, "title", 120) != self.title or _text(
            self.definition_summary, "definition_summary", 600,
        ) != self.definition_summary:
            raise DriveGovernanceError(
                "invalid_text", "drive text is not normalized and bounded",
            )
        if self.state not in _DRIVE_STATES:
            raise DriveGovernanceError("invalid_drive_state", "drive state is invalid")
        _bounded_float(
            self.max_abs_contribution, "max_abs_contribution",
            minimum=0.0, maximum=1.0,
        )
        _bounded_int(
            self.max_signals_per_goal, "max_signals_per_goal",
            minimum=1, maximum=20,
        )
        if _refs(
            self.evidence_refs, required=True, maximum=20,
        ) != self.evidence_refs:
            raise DriveGovernanceError(
                "invalid_evidence", "drive evidence references are not canonical",
            )
        created = _parse_time(self.created_at, field="drive created_at")
        if self.expires_at and _parse_time(
            self.expires_at, field="drive expires_at",
        ) <= created:
            raise DriveGovernanceError(
                "invalid_expiry", "drive expiry must follow creation",
            )
        digest = _digest(self.authority_payload())
        if digest != self.definition_digest \
                or self.drive_id != f"drive:{self.key}:{digest[:20]}":
            raise DriveGovernanceError(
                "immutable_object_tampered", "drive definition integrity mismatch",
            )

    def state_at(self, now: Optional[datetime] = None) -> str:
        if self.state == "disabled":
            return "disabled"
        if self.expires_at and _parse_time(
            self.expires_at, field="drive expires_at",
        ) <= _as_utc(now):
            return "expired"
        return "enabled"

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DriveV1":
        result = cls(
            drive_id=str(payload["drive_id"]),
            definition_digest=str(payload["definition_digest"]),
            key=str(payload["key"]),
            version_label=str(payload["version_label"]),
            title=str(payload["title"]),
            definition_summary=str(payload["definition_summary"]),
            max_abs_contribution=float(payload["max_abs_contribution"]),
            max_signals_per_goal=int(payload["max_signals_per_goal"]),
            state=str(payload["state"]),
            scope=ScopeV1(**dict(payload["scope"])),
            evidence_refs=tuple(payload.get("evidence_refs") or ()),
            created_at=str(payload["created_at"]),
            expires_at=(
                str(payload["expires_at"]) if payload.get("expires_at") else None
            ),
        )
        result.validate_integrity()
        return result


@dataclass(frozen=True)
class DriveSignalV1:
    signal_id: str
    signal_digest: str
    drive_id: str
    goal_fingerprint: str
    normalized_value: float
    confidence: float
    state: str
    rationale_summary: str
    evidence_refs: tuple[str, ...]
    observed_at: str
    expires_at: str
    scope: ScopeV1
    schema: str = "DriveSignalV1"
    version: int = 1

    @classmethod
    def derive(
        cls,
        *,
        drive: DriveV1,
        goal_fingerprint: str,
        normalized_value: float,
        confidence: float,
        rationale_summary: str,
        evidence_refs: Iterable[str],
        observed_at: datetime,
        expires_at: datetime,
        scope: ScopeV1,
        state: str = "active",
    ) -> "DriveSignalV1":
        drive.validate_integrity()
        fingerprint = _identifier(
            goal_fingerprint, "goal_fingerprint", maximum=192,
        )
        normalized_state = str(state or "").strip().lower()
        if normalized_state not in _SIGNAL_STATES:
            raise DriveGovernanceError(
                "invalid_signal_state", "signal state is invalid",
            )
        if not drive.scope.permits_child(scope):
            raise DriveGovernanceError(
                "scope_broadening", "signal scope cannot broaden its drive scope",
            )
        observed = _as_utc(observed_at)
        expiry = _as_utc(expires_at)
        if expiry <= observed:
            raise DriveGovernanceError(
                "invalid_expiry", "signal expiry must follow observation",
            )
        if expiry - observed > timedelta(days=90):
            raise DriveGovernanceError(
                "invalid_expiry", "signal lifetime cannot exceed 90 days",
            )
        value = _bounded_float(
            normalized_value, "normalized_value", minimum=-1.0, maximum=1.0,
        )
        if normalized_state != "active" and value != 0.0:
            # Disabled/unknown evidence is explicit state, not a hidden score.
            value = 0.0
        authority = {
            "schema": "DriveSignalV1", "version": 1,
            "drive_id": drive.drive_id,
            "goal_fingerprint": fingerprint,
            "normalized_value": value,
            "confidence": _bounded_float(
                confidence, "confidence", minimum=0.0, maximum=1.0,
            ),
            "state": normalized_state,
            "rationale_summary": _text(
                rationale_summary, "rationale_summary", 500,
            ),
            "evidence_refs": list(_refs(
                evidence_refs, required=True, maximum=20,
            )),
            "observed_at": _iso(observed),
            "expires_at": _iso(expiry),
            "scope": scope.payload(),
        }
        digest = _digest(authority)
        return cls(
            signal_id=f"drive-signal:{digest[:24]}",
            signal_digest=digest,
            drive_id=drive.drive_id,
            goal_fingerprint=fingerprint,
            normalized_value=value,
            confidence=authority["confidence"],
            state=normalized_state,
            rationale_summary=authority["rationale_summary"],
            evidence_refs=tuple(authority["evidence_refs"]),
            observed_at=authority["observed_at"],
            expires_at=authority["expires_at"],
            scope=scope,
        )

    def authority_payload(self) -> Dict[str, Any]:
        return {
            "schema": self.schema, "version": self.version,
            "drive_id": self.drive_id,
            "goal_fingerprint": self.goal_fingerprint,
            "normalized_value": self.normalized_value,
            "confidence": self.confidence,
            "state": self.state,
            "rationale_summary": self.rationale_summary,
            "evidence_refs": list(self.evidence_refs),
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "scope": self.scope.payload(),
        }

    def payload(self) -> Dict[str, Any]:
        return {
            **self.authority_payload(),
            "signal_id": self.signal_id,
            "signal_digest": self.signal_digest,
        }

    def validate_integrity(self) -> None:
        if self.schema != "DriveSignalV1" or self.version != 1:
            raise DriveGovernanceError(
                "unsupported_schema", "drive signal schema is unsupported",
            )
        _identifier(self.drive_id, "drive_id")
        _identifier(self.goal_fingerprint, "goal_fingerprint", maximum=192)
        if self.state not in _SIGNAL_STATES:
            raise DriveGovernanceError(
                "invalid_signal_state", "signal state is invalid",
            )
        value = _bounded_float(
            self.normalized_value, "normalized_value",
            minimum=-1.0, maximum=1.0,
        )
        _bounded_float(self.confidence, "confidence", minimum=0.0, maximum=1.0)
        if self.state != "active" and value != 0.0:
            raise DriveGovernanceError(
                "invalid_signal_state",
                "disabled or unknown signals cannot carry a score",
            )
        if _text(
            self.rationale_summary, "rationale_summary", 500,
        ) != self.rationale_summary:
            raise DriveGovernanceError(
                "invalid_text", "signal rationale is not normalized and bounded",
            )
        if _refs(
            self.evidence_refs, required=True, maximum=20,
        ) != self.evidence_refs:
            raise DriveGovernanceError(
                "invalid_evidence", "signal evidence references are not canonical",
            )
        observed = _parse_time(self.observed_at, field="signal observed_at")
        expiry = _parse_time(self.expires_at, field="signal expires_at")
        if expiry <= observed or expiry - observed > timedelta(days=90):
            raise DriveGovernanceError(
                "invalid_expiry", "signal expiry is outside its bounded lifetime",
            )
        digest = _digest(self.authority_payload())
        if digest != self.signal_digest \
                or self.signal_id != f"drive-signal:{digest[:24]}":
            raise DriveGovernanceError(
                "immutable_object_tampered", "drive signal integrity mismatch",
            )

    def state_at(self, now: Optional[datetime] = None) -> str:
        if _parse_time(self.expires_at, field="signal expires_at") <= _as_utc(now):
            return "expired"
        return self.state

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DriveSignalV1":
        result = cls(
            signal_id=str(payload["signal_id"]),
            signal_digest=str(payload["signal_digest"]),
            drive_id=str(payload["drive_id"]),
            goal_fingerprint=str(payload["goal_fingerprint"]),
            normalized_value=float(payload["normalized_value"]),
            confidence=float(payload["confidence"]),
            state=str(payload["state"]),
            rationale_summary=str(payload["rationale_summary"]),
            evidence_refs=tuple(payload.get("evidence_refs") or ()),
            observed_at=str(payload["observed_at"]),
            expires_at=str(payload["expires_at"]),
            scope=ScopeV1(**dict(payload["scope"])),
        )
        result.validate_integrity()
        return result


@dataclass(frozen=True)
class CharterAdmissionConstraintsV1:
    """Deterministic upper bounds for P3 admission under one charter."""

    objective_allow_terms: tuple[str, ...] = ()
    objective_deny_terms: tuple[str, ...] = (
        "destroy", "drop", "format", "overwrite", "wipe",
    )
    capability_ceiling: tuple[str, ...] = (
        "concerns:read", "directives:read", "memory:read",
        "projects:read", "reasoning", "situation:read", "web:read",
        "world_model:read",
    )
    capability_deny: tuple[str, ...] = ("messaging:send", "root:shell")
    required_boundary_refs: tuple[str, ...] = ()
    allowed_shareability: tuple[str, ...] = ("owner_private",)
    allowed_recipient_ids: tuple[str, ...] = ()
    allow_destructive: bool = False
    allow_root_shell: bool = False
    allow_messaging: bool = False
    schema: str = "CharterAdmissionConstraintsV1"
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != "CharterAdmissionConstraintsV1" or self.version != 1:
            raise DriveGovernanceError(
                "unsupported_schema", "charter admission constraints are unsupported",
            )
        for field_name in ("objective_allow_terms", "objective_deny_terms"):
            values = tuple(getattr(self, field_name))
            if len(values) > 30:
                raise DriveGovernanceError(
                    "invalid_admission_constraints", f"{field_name} exceeds 30 items",
                )
            canonical = tuple(sorted({
                _text(item, field_name, 80).casefold() for item in values
            }))
            if values != canonical:
                raise DriveGovernanceError(
                    "invalid_admission_constraints",
                    f"{field_name} must be unique, sorted, and lowercase",
                )
        for field_name in ("capability_ceiling", "capability_deny"):
            values = tuple(getattr(self, field_name))
            if len(values) > 30:
                raise DriveGovernanceError(
                    "invalid_admission_constraints", f"{field_name} exceeds 30 items",
                )
            canonical = tuple(sorted({
                _identifier(item, field_name) for item in values
            }))
            if values != canonical:
                raise DriveGovernanceError(
                    "invalid_admission_constraints",
                    f"{field_name} must be unique and sorted",
                )
        boundaries = _refs(
            self.required_boundary_refs, required=False, maximum=30,
        )
        if boundaries != self.required_boundary_refs:
            raise DriveGovernanceError(
                "invalid_admission_constraints",
                "required boundary references are not canonical",
            )
        shareability = tuple(self.allowed_shareability)
        if (
            not shareability
            or len(shareability) > len(_SHAREABILITY)
            or tuple(sorted(set(shareability))) != shareability
            or any(item not in _SHAREABILITY for item in shareability)
        ):
            raise DriveGovernanceError(
                "invalid_admission_constraints",
                "allowed shareability lanes are invalid",
            )
        recipients = tuple(self.allowed_recipient_ids)
        if len(recipients) > 30 or tuple(sorted(set(recipients))) != recipients:
            raise DriveGovernanceError(
                "invalid_admission_constraints", "recipient IDs are not canonical",
            )
        for recipient in recipients:
            _identifier(recipient, "allowed_recipient_id", maximum=128)
        if any(not isinstance(value, bool) for value in (
            self.allow_destructive, self.allow_root_shell, self.allow_messaging,
        )):
            raise DriveGovernanceError(
                "invalid_admission_constraints", "admission flags must be boolean",
            )

    def payload(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "objective_allow_terms": list(self.objective_allow_terms),
            "objective_deny_terms": list(self.objective_deny_terms),
            "capability_ceiling": list(self.capability_ceiling),
            "capability_deny": list(self.capability_deny),
            "required_boundary_refs": list(self.required_boundary_refs),
            "allowed_shareability": list(self.allowed_shareability),
            "allowed_recipient_ids": list(self.allowed_recipient_ids),
            "allow_destructive": self.allow_destructive,
            "allow_root_shell": self.allow_root_shell,
            "allow_messaging": self.allow_messaging,
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any],
    ) -> "CharterAdmissionConstraintsV1":
        return cls(
            objective_allow_terms=tuple(payload.get("objective_allow_terms") or ()),
            objective_deny_terms=tuple(payload.get("objective_deny_terms") or ()),
            capability_ceiling=tuple(payload.get("capability_ceiling") or ()),
            capability_deny=tuple(payload.get("capability_deny") or ()),
            required_boundary_refs=tuple(
                payload.get("required_boundary_refs") or ()),
            allowed_shareability=tuple(payload.get("allowed_shareability") or ()),
            allowed_recipient_ids=tuple(
                payload.get("allowed_recipient_ids") or ()),
            allow_destructive=payload.get("allow_destructive") is True,
            allow_root_shell=payload.get("allow_root_shell") is True,
            allow_messaging=payload.get("allow_messaging") is True,
            schema=str(payload.get("schema") or ""),
            version=int(payload.get("version") or 0),
        )


@dataclass(frozen=True)
class CharterRevisionV1:
    revision_id: str
    content_digest: str
    charter_key: str
    revision_label: str
    parent_revision_id: Optional[str]
    title: str
    purpose_summary: str
    principles: tuple[str, ...]
    drive_weights: tuple[tuple[str, float], ...]
    ranking_budget: RankingBudgetV1
    scope: ScopeV1
    evidence_refs: tuple[str, ...]
    proposed_by: str
    proposed_at: str
    expires_at: str
    admission_constraints: Optional[CharterAdmissionConstraintsV1] = None
    schema: str = "CharterRevisionV1"
    version: int = 1

    @classmethod
    def create(
        cls,
        *,
        charter_key: str,
        revision_label: str,
        parent_revision_id: Optional[str],
        title: str,
        purpose_summary: str,
        principles: Iterable[str],
        drive_weights: Mapping[str, float],
        ranking_budget: RankingBudgetV1,
        scope: ScopeV1,
        evidence_refs: Iterable[str],
        proposed_by: str,
        proposed_at: datetime,
        expires_at: datetime,
        admission_constraints: Optional[CharterAdmissionConstraintsV1] = None,
    ) -> "CharterRevisionV1":
        key = str(charter_key or "").strip().lower()
        if not _SAFE_KEY.fullmatch(key):
            raise DriveGovernanceError(
                "invalid_charter_key", "charter key is invalid",
            )
        label = _identifier(revision_label, "revision_label", maximum=64)
        parent = (
            _identifier(parent_revision_id, "parent_revision_id")
            if parent_revision_id else None
        )
        normalized_principles = tuple(
            _text(item, "principle", 300) for item in principles or ()
        )
        if not normalized_principles or len(normalized_principles) > 20:
            raise DriveGovernanceError(
                "invalid_principles", "charter requires 1 to 20 principles",
            )
        if len(set(normalized_principles)) != len(normalized_principles):
            raise DriveGovernanceError(
                "invalid_principles", "charter principles must be unique",
            )
        weights: list[tuple[str, float]] = []
        for raw_drive_id, raw_weight in (drive_weights or {}).items():
            drive_id = _identifier(raw_drive_id, "drive_id")
            weight = _bounded_float(
                raw_weight, f"drive weight {drive_id}", minimum=0.000001,
                maximum=1.0,
            )
            weights.append((drive_id, weight))
        weights.sort()
        if not weights or len(weights) > 20:
            raise DriveGovernanceError(
                "invalid_drive_weights", "charter requires 1 to 20 drive weights",
            )
        if sum(weight for _, weight in weights) > 1.00000001:
            raise DriveGovernanceError(
                "drive_budget_exceeded", "drive weights must sum to at most 1.0",
            )
        proposed = _as_utc(proposed_at)
        expiry = _as_utc(expires_at)
        if expiry - proposed < timedelta(hours=1):
            raise DriveGovernanceError(
                "invalid_expiry", "charter lifetime must be at least one hour",
            )
        if expiry - proposed > timedelta(days=366):
            raise DriveGovernanceError(
                "invalid_expiry", "charter lifetime cannot exceed 366 days",
            )
        constraints = admission_constraints or CharterAdmissionConstraintsV1()
        authority = {
            "schema": "CharterRevisionV1", "version": 1,
            "charter_key": key,
            "revision_label": label,
            "parent_revision_id": parent,
            "title": _text(title, "title", 120),
            "purpose_summary": _text(
                purpose_summary, "purpose_summary", 600,
            ),
            "principles": list(normalized_principles),
            "drive_weights": [[drive_id, weight] for drive_id, weight in weights],
            "ranking_budget": ranking_budget.payload(),
            "scope": scope.payload(),
            "evidence_refs": list(_refs(
                evidence_refs, required=True, maximum=30,
            )),
            "proposed_by": _identifier(
                proposed_by, "proposed_by", maximum=128,
            ),
            "proposed_at": _iso(proposed),
            "expires_at": _iso(expiry),
            "admission_constraints": constraints.payload(),
        }
        digest = _digest(authority)
        return cls(
            revision_id=f"charter:{key}:{digest[:24]}",
            content_digest=digest,
            charter_key=key,
            revision_label=label,
            parent_revision_id=parent,
            title=authority["title"],
            purpose_summary=authority["purpose_summary"],
            principles=normalized_principles,
            drive_weights=tuple(weights),
            ranking_budget=ranking_budget,
            scope=scope,
            evidence_refs=tuple(authority["evidence_refs"]),
            proposed_by=authority["proposed_by"],
            proposed_at=authority["proposed_at"],
            expires_at=authority["expires_at"],
            admission_constraints=constraints,
        )

    def authority_payload(self) -> Dict[str, Any]:
        payload = {
            "schema": self.schema, "version": self.version,
            "charter_key": self.charter_key,
            "revision_label": self.revision_label,
            "parent_revision_id": self.parent_revision_id,
            "title": self.title,
            "purpose_summary": self.purpose_summary,
            "principles": list(self.principles),
            "drive_weights": [list(item) for item in self.drive_weights],
            "ranking_budget": self.ranking_budget.payload(),
            "scope": self.scope.payload(),
            "evidence_refs": list(self.evidence_refs),
            "proposed_by": self.proposed_by,
            "proposed_at": self.proposed_at,
            "expires_at": self.expires_at,
        }
        if self.admission_constraints is not None:
            payload["admission_constraints"] = self.admission_constraints.payload()
        return payload

    def payload(self) -> Dict[str, Any]:
        return {
            **self.authority_payload(),
            "revision_id": self.revision_id,
            "content_digest": self.content_digest,
        }

    def validate_integrity(self) -> None:
        if self.schema != "CharterRevisionV1" or self.version != 1:
            raise DriveGovernanceError(
                "unsupported_schema", "charter revision schema is unsupported",
            )
        if not _SAFE_KEY.fullmatch(str(self.charter_key or "")):
            raise DriveGovernanceError(
                "invalid_charter_key", "charter key is invalid",
            )
        _identifier(self.revision_label, "revision_label", maximum=64)
        if self.parent_revision_id:
            _identifier(self.parent_revision_id, "parent_revision_id")
        if _text(self.title, "title", 120) != self.title or _text(
            self.purpose_summary, "purpose_summary", 600,
        ) != self.purpose_summary:
            raise DriveGovernanceError(
                "invalid_text", "charter text is not normalized and bounded",
            )
        if not 1 <= len(self.principles) <= 20 or len(
            set(self.principles)
        ) != len(self.principles):
            raise DriveGovernanceError(
                "invalid_principles", "charter principles are invalid",
            )
        if any(_text(item, "principle", 300) != item for item in self.principles):
            raise DriveGovernanceError(
                "invalid_principles", "charter principles are not canonical",
            )
        if not 1 <= len(self.drive_weights) <= 20:
            raise DriveGovernanceError(
                "invalid_drive_weights", "charter drive weights are invalid",
            )
        drive_ids = [item[0] for item in self.drive_weights]
        if drive_ids != sorted(drive_ids) or len(set(drive_ids)) != len(drive_ids):
            raise DriveGovernanceError(
                "invalid_drive_weights", "charter drive weights are not canonical",
            )
        for drive_id, weight in self.drive_weights:
            _identifier(drive_id, "drive_id")
            _bounded_float(
                weight, f"drive weight {drive_id}", minimum=0.000001, maximum=1.0,
            )
        if sum(weight for _, weight in self.drive_weights) > 1.00000001:
            raise DriveGovernanceError(
                "drive_budget_exceeded", "drive weights exceed the charter budget",
            )
        if _refs(
            self.evidence_refs, required=True, maximum=30,
        ) != self.evidence_refs:
            raise DriveGovernanceError(
                "invalid_evidence", "charter evidence references are not canonical",
            )
        if self.admission_constraints is not None:
            self.admission_constraints.__post_init__()
        _identifier(self.proposed_by, "proposed_by", maximum=128)
        proposed = _parse_time(self.proposed_at, field="charter proposed_at")
        expiry = _parse_time(self.expires_at, field="charter expires_at")
        if not timedelta(hours=1) <= expiry - proposed <= timedelta(days=366):
            raise DriveGovernanceError(
                "invalid_expiry", "charter expiry is outside its bounded lifetime",
            )
        digest = _digest(self.authority_payload())
        if digest != self.content_digest or self.revision_id != (
            f"charter:{self.charter_key}:{digest[:24]}"
        ):
            raise DriveGovernanceError(
                "immutable_object_tampered", "charter revision integrity mismatch",
            )

    def weight_map(self) -> Dict[str, float]:
        return dict(self.drive_weights)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CharterRevisionV1":
        result = cls(
            revision_id=str(payload["revision_id"]),
            content_digest=str(payload["content_digest"]),
            charter_key=str(payload["charter_key"]),
            revision_label=str(payload["revision_label"]),
            parent_revision_id=(
                str(payload["parent_revision_id"])
                if payload.get("parent_revision_id") else None
            ),
            title=str(payload["title"]),
            purpose_summary=str(payload["purpose_summary"]),
            principles=tuple(payload.get("principles") or ()),
            drive_weights=tuple(
                (str(item[0]), float(item[1]))
                for item in payload.get("drive_weights") or ()
            ),
            ranking_budget=RankingBudgetV1(**dict(payload["ranking_budget"])),
            scope=ScopeV1(**dict(payload["scope"])),
            evidence_refs=tuple(payload.get("evidence_refs") or ()),
            proposed_by=str(payload["proposed_by"]),
            proposed_at=str(payload["proposed_at"]),
            expires_at=str(payload["expires_at"]),
            admission_constraints=(
                CharterAdmissionConstraintsV1.from_payload(
                    dict(payload["admission_constraints"]),
                )
                if payload.get("admission_constraints") else None
            ),
        )
        result.validate_integrity()
        return result


@dataclass(frozen=True)
class GoalRankInputV1:
    """Server-built projection of a P3-created project eligible for ordering."""

    goal_id: str
    proposal_id: str
    goal_fingerprint: str
    title: str
    objective_summary: str
    rationale_summary: str
    evidence_refs: tuple[str, ...]
    policy_decision_refs: tuple[str, ...]
    scope: ScopeV1
    schema: str = "GoalRankInputV1"
    version: int = 1

    def __post_init__(self) -> None:
        _identifier(self.goal_id, "goal_id")
        _identifier(self.proposal_id, "proposal_id")
        _identifier(self.goal_fingerprint, "goal_fingerprint")
        _text(self.title, "title", 160)
        _text(self.objective_summary, "objective_summary", 600)
        _text(self.rationale_summary, "rationale_summary", 500)
        _refs(self.evidence_refs, maximum=30)
        refs = _refs(
            self.policy_decision_refs,
            field="policy_decision_refs",
            maximum=20,
        )
        if len(refs) != len(self.policy_decision_refs):
            raise DriveGovernanceError(
                "duplicate_policy_reference", "policy decision references repeat",
            )

    def payload(self) -> Dict[str, Any]:
        return {
            "schema": self.schema, "version": self.version,
            "goal_id": self.goal_id,
            "proposal_id": self.proposal_id,
            "goal_fingerprint": self.goal_fingerprint,
            "title": self.title,
            "objective_summary": self.objective_summary,
            "rationale_summary": self.rationale_summary,
            "evidence_refs": list(self.evidence_refs),
            "policy_decision_refs": list(self.policy_decision_refs),
            "scope": self.scope.payload(),
        }


@dataclass(frozen=True)
class DriveContributionV1:
    drive_id: str
    state: str
    weight: float
    raw_signal_value: float
    mean_confidence: float
    weighted_value: float
    abs_budget: float
    signal_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    rationale_summaries: tuple[str, ...]
    schema: str = "DriveContributionV1"
    version: int = 1

    def payload(self) -> Dict[str, Any]:
        return {
            "schema": self.schema, "version": self.version,
            "drive_id": self.drive_id,
            "state": self.state,
            "weight": self.weight,
            "raw_signal_value": self.raw_signal_value,
            "mean_confidence": self.mean_confidence,
            "weighted_value": self.weighted_value,
            "abs_budget": self.abs_budget,
            "signal_refs": list(self.signal_refs),
            "evidence_refs": list(self.evidence_refs),
            "rationale_summaries": list(self.rationale_summaries),
        }


@dataclass(frozen=True)
class GoalRankResultV1:
    goal_id: str
    proposal_id: str
    state: str
    eligible: bool
    total_score: Optional[float]
    contributions: tuple[DriveContributionV1, ...]
    drive_states: Dict[str, str]
    evidence_refs: tuple[str, ...]
    policy_decision_refs: tuple[str, ...]
    rationale_summary: str
    scope: ScopeV1
    authorization_effect: str = "none"
    schema: str = "GoalRankResultV1"
    version: int = 1

    def payload(self) -> Dict[str, Any]:
        return {
            "schema": self.schema, "version": self.version,
            "goal_id": self.goal_id,
            "proposal_id": self.proposal_id,
            "state": self.state,
            "eligible": self.eligible,
            "total_score": self.total_score,
            "contributions": [item.payload() for item in self.contributions],
            "drive_states": dict(sorted(self.drive_states.items())),
            "evidence_refs": list(self.evidence_refs),
            "policy_decision_refs": list(self.policy_decision_refs),
            "rationale_summary": self.rationale_summary,
            "scope": self.scope.payload(),
            "authorization_effect": self.authorization_effect,
            "required_p3_stages": list(_REQUIRED_P3_STAGES),
        }


@dataclass(frozen=True)
class RankingBatchV1:
    status: str
    mode: str
    ranking_applied: bool
    charter_revision_id: Optional[str]
    input_order: tuple[str, ...]
    suggested_order: tuple[str, ...]
    effective_order: tuple[str, ...]
    held_goal_ids: tuple[str, ...]
    results: tuple[GoalRankResultV1, ...]
    generated_at: str
    authorization_effect: str = "none"
    schema: str = "GoalRankingBatchV1"
    version: int = 1

    def payload(self) -> Dict[str, Any]:
        return {
            "schema": self.schema, "version": self.version,
            "status": self.status,
            "mode": self.mode,
            "ranking_applied": self.ranking_applied,
            "charter_revision_id": self.charter_revision_id,
            "input_order": list(self.input_order),
            "suggested_order": list(self.suggested_order),
            "effective_order": list(self.effective_order),
            "held_goal_ids": list(self.held_goal_ids),
            "results": [item.payload() for item in self.results],
            "generated_at": self.generated_at,
            "authorization_effect": self.authorization_effect,
        }

    def observer_projection(
        self,
        *,
        viewer_person_id: str,
        owner_person_id: str,
        audiences: set[str] | frozenset[str] = frozenset(),
    ) -> Dict[str, Any]:
        visible = tuple(
            item for item in self.results
            if item.scope.visible_to(
                viewer_person_id=viewer_person_id,
                owner_person_id=owner_person_id,
                audiences=audiences,
            )
        )
        ids = {item.goal_id for item in visible}
        payload = self.payload()
        payload["results"] = [item.payload() for item in visible]
        for key in ("input_order", "suggested_order", "effective_order"):
            payload[key] = [goal_id for goal_id in payload[key] if goal_id in ids]
        payload["held_goal_ids"] = [
            goal_id for goal_id in payload["held_goal_ids"] if goal_id in ids
        ]
        return payload


def charter_transition_binding(
    revision: CharterRevisionV1,
    *,
    transition: str,
    expected_active_revision_id: Optional[str],
) -> ActionBinding:
    """Build one exact, privacy-bounded ApprovalAuthority action binding."""

    normalized = str(transition or "").strip().lower()
    if normalized not in _TRANSITIONS:
        raise DriveGovernanceError(
            "invalid_transition", "transition must be activate or revoke",
        )
    revision.validate_integrity()
    constraints = {
        "charter_key_digest": _digest(revision.charter_key),
        "revision_id_digest": _digest(revision.revision_id),
        "content_digest": revision.content_digest,
        "expected_active_revision_digest": _digest(
            expected_active_revision_id,
        ),
    }
    scope = {
        "version": 1,
        "job_type": "charter_governance",
        "action_name": f"charter_revision_{normalized}",
        "risk": "authority_mutation",
        "constraints": constraints,
    }
    snapshot = {
        "version": 1,
        "transition": normalized,
        "revision_id": revision.revision_id,
        "content_digest": revision.content_digest,
        "expected_active_revision_id": expected_active_revision_id,
        "scope": scope,
    }
    return ActionBinding(
        action_digest=_digest(snapshot),
        scope=scope,
        scope_digest=_digest(scope),
    )


def charter_transition_subject(
    revision: CharterRevisionV1,
    *,
    transition: str,
) -> ApprovalSubjectBinding:
    normalized = str(transition or "").strip().lower()
    if normalized not in _TRANSITIONS:
        raise DriveGovernanceError(
            "invalid_transition", "transition must be activate or revoke",
        )
    revision.validate_integrity()
    return ApprovalSubjectBinding(
        kind="charter_transition",
        subject_id=revision.revision_id,
        revision=revision.content_digest,
        action=normalized,
    )


def charter_transition_presentation(
    revision: CharterRevisionV1,
    *,
    transition: str,
    binding: ActionBinding,
) -> Dict[str, Any]:
    """Build the bounded owner view without charter principles or evidence."""

    normalized = str(transition or "").strip().lower()
    if normalized not in _TRANSITIONS:
        raise DriveGovernanceError(
            "invalid_transition", "transition must be activate or revoke",
        )
    verb = "Activate" if normalized == "activate" else "Revoke"
    return {
        "schema": PRESENTATION_SCHEMA,
        "version": 1,
        "summary": (
            f"{verb} charter {revision.title} "
            f"({revision.revision_label})"
        ),
        "action_name": f"charter_revision_{normalized}",
        "risk": "authority_mutation",
        "effect": "mutation",
        "target": f"charter:{revision.charter_key}",
        "capabilities": [],
        "deadline": "bounded by approval request expiry",
        "reversibility": (
            "revocable by a new exact owner decision"
            if normalized == "activate"
            else "requires a new charter activation decision"
        ),
        "constraints": dict(binding.scope["constraints"]),
    }


class DriveGovernanceStore:
    """Append-only SQLite ledger for drives, signals, and charter lifecycle."""

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch(mode=0o600)
        else:
            os.chmod(self.path, 0o600)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._closed = False
        self._initialize()

    def close(self) -> None:
        """Release the feature-owned SQLite handle; exact retries are safe."""

        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def _initialize(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS governance_operations (
                    operation_id TEXT PRIMARY KEY,
                    object_kind TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS drive_definitions (
                    drive_id TEXT PRIMARY KEY,
                    definition_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS drive_signals (
                    signal_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id TEXT NOT NULL UNIQUE,
                    signal_digest TEXT NOT NULL,
                    drive_id TEXT NOT NULL,
                    goal_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(drive_id) REFERENCES drive_definitions(drive_id)
                );
                CREATE INDEX IF NOT EXISTS drive_signals_goal_idx
                    ON drive_signals(goal_fingerprint, observed_at DESC, signal_seq);
                CREATE INDEX IF NOT EXISTS drive_signals_drive_idx
                    ON drive_signals(drive_id, expires_at);

                CREATE TABLE IF NOT EXISTS charter_revisions (
                    revision_id TEXT PRIMARY KEY,
                    charter_key TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    parent_revision_id TEXT,
                    proposed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS charter_revisions_key_idx
                    ON charter_revisions(charter_key, proposed_at DESC);

                CREATE TABLE IF NOT EXISTS charter_lifecycle_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    charter_key TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    transition TEXT NOT NULL,
                    from_revision_id TEXT,
                    action_digest TEXT NOT NULL,
                    approval_request_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    authority_evidence TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(revision_id) REFERENCES charter_revisions(revision_id)
                );
                CREATE INDEX IF NOT EXISTS charter_events_key_idx
                    ON charter_lifecycle_events(charter_key, event_seq);
                CREATE INDEX IF NOT EXISTS charter_events_revision_idx
                    ON charter_lifecycle_events(revision_id, event_seq);

                CREATE TABLE IF NOT EXISTS charter_transition_operations (
                    operation_id TEXT PRIMARY KEY,
                    transition_digest TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    transition TEXT NOT NULL,
                    approval_request_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS charter_authority_uses (
                    approval_request_id TEXT PRIMARY KEY,
                    action_digest TEXT NOT NULL,
                    operation_id TEXT NOT NULL UNIQUE,
                    event_id TEXT NOT NULL,
                    used_at TEXT NOT NULL
                );
                """
            )
            current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if current < DRIVE_GOVERNANCE_SCHEMA_VERSION:
                self._conn.execute(
                    f"PRAGMA user_version={DRIVE_GOVERNANCE_SCHEMA_VERSION}"
                )
        os.chmod(self.path, 0o600)

    def _operation(
        self,
        conn: sqlite3.Connection,
        *,
        operation_id: str,
        object_kind: str,
        object_id: str,
        content_digest: str,
        now: datetime,
    ) -> bool:
        operation = _operation_id(operation_id)
        row = conn.execute(
            "SELECT * FROM governance_operations WHERE operation_id=?",
            (operation,),
        ).fetchone()
        if row is not None:
            if (
                row["object_kind"] != object_kind
                or row["object_id"] != object_id
                or row["content_digest"] != content_digest
            ):
                raise DriveGovernanceError(
                    "operation_replay_conflict",
                    "operation replay does not match its immutable first use",
                )
            return True
        conn.execute(
            "INSERT INTO governance_operations "
            "(operation_id,object_kind,object_id,content_digest,created_at) "
            "VALUES (?,?,?,?,?)",
            (operation, object_kind, object_id, content_digest, _iso(now)),
        )
        return False

    def register_drive(
        self,
        drive: DriveV1,
        *,
        operation_id: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        observed = _as_utc(now)
        actual_digest = _digest(drive.authority_payload())
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            replayed = self._operation(
                self._conn,
                operation_id=operation_id,
                object_kind="drive",
                object_id=drive.drive_id,
                content_digest=actual_digest,
                now=observed,
            )
            if replayed:
                existing = self._conn.execute(
                    "SELECT definition_digest,payload_json FROM drive_definitions "
                    "WHERE drive_id=?", (drive.drive_id,),
                ).fetchone()
                if existing is None or existing["definition_digest"] != actual_digest:
                    raise DriveGovernanceError(
                        "ledger_integrity_error", "replayed drive is missing or changed",
                    )
                return {
                    "enabled": True, "status": "drive_replayed",
                    "drive_id": drive.drive_id, "replayed": True,
                }
            drive.validate_integrity()
            encoded = _canonical(drive.payload())
            existing = self._conn.execute(
                "SELECT * FROM drive_definitions WHERE drive_id=?",
                (drive.drive_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["definition_digest"] != drive.definition_digest
                    or existing["payload_json"] != encoded
                ):
                    raise DriveGovernanceError(
                        "immutable_drive_conflict", "drive definition is immutable",
                    )
                status = "drive_replayed"
            else:
                self._conn.execute(
                    "INSERT INTO drive_definitions "
                    "(drive_id,definition_digest,payload_json,created_at) "
                    "VALUES (?,?,?,?)",
                    (
                        drive.drive_id, drive.definition_digest, encoded,
                        _iso(observed),
                    ),
                )
                status = "drive_registered"
        return {
            "enabled": True, "status": status,
            "drive_id": drive.drive_id,
            "replayed": status == "drive_replayed",
        }

    def get_drive(self, drive_id: str) -> Optional[DriveV1]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM drive_definitions WHERE drive_id=?",
                (str(drive_id),),
            ).fetchone()
        return DriveV1.from_payload(json.loads(row["payload_json"])) if row else None

    def list_drives(self) -> list[DriveV1]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM drive_definitions ORDER BY created_at,drive_id"
            ).fetchall()
        return [DriveV1.from_payload(json.loads(row["payload_json"])) for row in rows]

    def record_signal(
        self,
        signal: DriveSignalV1,
        *,
        operation_id: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        observed = _as_utc(now)
        actual_digest = _digest(signal.authority_payload())
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            replayed = self._operation(
                self._conn,
                operation_id=operation_id,
                object_kind="signal",
                object_id=signal.signal_id,
                content_digest=actual_digest,
                now=observed,
            )
            if replayed:
                existing = self._conn.execute(
                    "SELECT signal_digest FROM drive_signals WHERE signal_id=?",
                    (signal.signal_id,),
                ).fetchone()
                if existing is None or existing["signal_digest"] != actual_digest:
                    raise DriveGovernanceError(
                        "ledger_integrity_error", "replayed signal is missing or changed",
                    )
                return {
                    "enabled": True, "status": "signal_replayed",
                    "signal_id": signal.signal_id, "replayed": True,
                }
            signal.validate_integrity()
            drive = self.get_drive(signal.drive_id)
            if drive is None:
                raise DriveGovernanceError("drive_unknown", "signal drive is unknown")
            if not drive.scope.permits_child(signal.scope):
                raise DriveGovernanceError(
                    "scope_broadening", "signal scope broadens its registered drive",
                )
            encoded = _canonical(signal.payload())
            existing = self._conn.execute(
                "SELECT * FROM drive_signals WHERE signal_id=?",
                (signal.signal_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["signal_digest"] != signal.signal_digest
                    or existing["payload_json"] != encoded
                ):
                    raise DriveGovernanceError(
                        "immutable_signal_conflict", "drive signal is immutable",
                    )
                status = "signal_replayed"
            else:
                self._conn.execute(
                    "INSERT INTO drive_signals "
                    "(signal_id,signal_digest,drive_id,goal_fingerprint,state,"
                    "observed_at,expires_at,payload_json,recorded_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        signal.signal_id, signal.signal_digest, signal.drive_id,
                        signal.goal_fingerprint, signal.state, signal.observed_at,
                        signal.expires_at, encoded, _iso(observed),
                    ),
                )
                status = "signal_recorded"
        return {
            "enabled": True, "status": status,
            "signal_id": signal.signal_id,
            "replayed": status == "signal_replayed",
        }

    def signals_for_goal(
        self,
        goal_fingerprint: str,
        *,
        now: Optional[datetime] = None,
        include_expired: bool = False,
    ) -> list[DriveSignalV1]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM drive_signals "
                "WHERE goal_fingerprint=? "
                "ORDER BY observed_at DESC,signal_seq ASC",
                (str(goal_fingerprint),),
            ).fetchall()
        signals = [
            DriveSignalV1.from_payload(json.loads(row["payload_json"]))
            for row in rows
        ]
        if include_expired:
            return signals
        return [item for item in signals if item.state_at(now) != "expired"]

    def propose_revision(
        self,
        revision: CharterRevisionV1,
        *,
        operation_id: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        observed = _as_utc(now)
        actual_digest = _digest(revision.authority_payload())
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            replayed = self._operation(
                self._conn,
                operation_id=operation_id,
                object_kind="charter_revision",
                object_id=revision.revision_id,
                content_digest=actual_digest,
                now=observed,
            )
            if replayed:
                existing = self._conn.execute(
                    "SELECT content_digest FROM charter_revisions WHERE revision_id=?",
                    (revision.revision_id,),
                ).fetchone()
                if existing is None or existing["content_digest"] != actual_digest:
                    raise DriveGovernanceError(
                        "ledger_integrity_error",
                        "replayed charter revision is missing or changed",
                    )
                projection = self._revision_projection_conn(
                    self._conn, revision.revision_id, observed,
                )
                projection.update({"enabled": True, "replayed": True})
                return projection
            revision.validate_integrity()
            if revision.parent_revision_id == revision.revision_id:
                raise DriveGovernanceError(
                    "invalid_parent", "charter cannot be its own parent",
                )
            encoded = _canonical(revision.payload())
            existing = self._conn.execute(
                "SELECT * FROM charter_revisions WHERE revision_id=?",
                (revision.revision_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["content_digest"] != revision.content_digest
                    or existing["payload_json"] != encoded
                ):
                    raise DriveGovernanceError(
                        "immutable_revision_conflict",
                        "charter revision is immutable",
                    )
            else:
                self._conn.execute(
                    "INSERT INTO charter_revisions "
                    "(revision_id,charter_key,content_digest,parent_revision_id,"
                    "proposed_at,expires_at,payload_json) VALUES (?,?,?,?,?,?,?)",
                    (
                        revision.revision_id, revision.charter_key,
                        revision.content_digest, revision.parent_revision_id,
                        revision.proposed_at, revision.expires_at, encoded,
                    ),
                )
            projection = self._revision_projection_conn(
                self._conn, revision.revision_id, observed,
            )
            projection.update({"enabled": True, "replayed": existing is not None})
            return projection

    def get_revision(self, revision_id: str) -> Optional[CharterRevisionV1]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM charter_revisions WHERE revision_id=?",
                (str(revision_id),),
            ).fetchone()
        return (
            CharterRevisionV1.from_payload(json.loads(row["payload_json"]))
            if row else None
        )

    def _revision_projection_conn(
        self,
        conn: sqlite3.Connection,
        revision_id: str,
        now: datetime,
    ) -> Dict[str, Any]:
        row = conn.execute(
            "SELECT payload_json FROM charter_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise DriveGovernanceError(
                "revision_unknown", "charter revision is unknown",
            )
        revision = CharterRevisionV1.from_payload(json.loads(row["payload_json"]))
        events = conn.execute(
            "SELECT * FROM charter_lifecycle_events "
            "WHERE revision_id=? ORDER BY event_seq",
            (revision_id,),
        ).fetchall()
        for event in events:
            self._validate_lifecycle_event(event)
        transitions = [event["transition"] for event in events]
        if "revoke" in transitions:
            status = "revoked"
        elif "supersede" in transitions:
            status = "superseded"
        elif "activate" in transitions:
            status = (
                "expired"
                if _parse_time(revision.expires_at, field="revision expires_at") <= now
                else "active"
            )
        else:
            status = (
                "expired"
                if _parse_time(revision.expires_at, field="revision expires_at") <= now
                else "proposed"
            )
        result = revision.payload()
        result["lifecycle_status"] = status
        result["lifecycle_event_refs"] = [event["event_id"] for event in events]
        return result

    @staticmethod
    def _validate_lifecycle_event(row: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DriveGovernanceError(
                "lifecycle_integrity_error", "lifecycle event payload is invalid",
            ) from exc
        fields = (
            "charter_key", "revision_id", "transition", "from_revision_id",
            "action_digest", "approval_request_id", "decision_id",
            "principal_id", "authority_evidence", "occurred_at",
        )
        if any(payload.get(field) != row[field] for field in fields):
            raise DriveGovernanceError(
                "lifecycle_integrity_error",
                "lifecycle event columns do not match immutable payload",
            )
        if row["event_id"] != f"charter-event:{_digest(payload)[:24]}":
            raise DriveGovernanceError(
                "lifecycle_integrity_error", "lifecycle event digest mismatch",
            )
        return payload

    def revision_projection(
        self, revision_id: str, *, now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            return self._revision_projection_conn(
                self._conn, revision_id, _as_utc(now),
            )

    def list_revision_projections(
        self,
        charter_key: Optional[str] = None,
        *,
        now: Optional[datetime] = None,
    ) -> list[Dict[str, Any]]:
        observed = _as_utc(now)
        with self._lock:
            if charter_key:
                rows = self._conn.execute(
                    "SELECT revision_id FROM charter_revisions WHERE charter_key=? "
                    "ORDER BY proposed_at DESC,revision_id",
                    (str(charter_key),),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT revision_id FROM charter_revisions "
                    "ORDER BY proposed_at DESC,revision_id"
                ).fetchall()
            return [
                self._revision_projection_conn(
                    self._conn, row["revision_id"], observed,
                )
                for row in rows
            ]

    def _active_revision_conn(
        self,
        conn: sqlite3.Connection,
        charter_key: str,
        now: datetime,
    ) -> Optional[CharterRevisionV1]:
        rows = conn.execute(
            "SELECT revision_id,payload_json FROM charter_revisions "
            "WHERE charter_key=? ORDER BY proposed_at DESC,revision_id",
            (charter_key,),
        ).fetchall()
        active: list[CharterRevisionV1] = []
        for row in rows:
            projection = self._revision_projection_conn(
                conn, row["revision_id"], now,
            )
            if projection["lifecycle_status"] == "active":
                active.append(CharterRevisionV1.from_payload(
                    json.loads(row["payload_json"]),
                ))
        if len(active) > 1:
            raise DriveGovernanceError(
                "lifecycle_integrity_error", "multiple active charter revisions",
            )
        return active[0] if active else None

    def active_revision(
        self, charter_key: str = "default", *, now: Optional[datetime] = None,
    ) -> Optional[CharterRevisionV1]:
        with self._lock:
            return self._active_revision_conn(
                self._conn, str(charter_key), _as_utc(now),
            )

    def _active_revisions_conn(
        self, conn: sqlite3.Connection, now: datetime,
    ) -> tuple[CharterRevisionV1, ...]:
        rows = conn.execute(
            "SELECT DISTINCT charter_key FROM charter_revisions "
            "ORDER BY charter_key"
        ).fetchall()
        return tuple(
            active
            for row in rows
            for active in (
                self._active_revision_conn(conn, row["charter_key"], now),
            )
            if active is not None
        )

    def active_revisions(
        self, *, now: Optional[datetime] = None,
    ) -> tuple[CharterRevisionV1, ...]:
        """Return every active charter across keys in deterministic order."""

        with self._lock:
            return self._active_revisions_conn(self._conn, _as_utc(now))

    def _validate_transition_conn(
        self,
        conn: sqlite3.Connection,
        revision: CharterRevisionV1,
        transition: str,
        now: datetime,
    ) -> Optional[CharterRevisionV1]:
        current = self._active_revision_conn(conn, revision.charter_key, now)
        projection = self._revision_projection_conn(
            conn, revision.revision_id, now,
        )
        status = projection["lifecycle_status"]
        if transition == "activate":
            if status == "expired":
                raise DriveGovernanceError(
                    "revision_expired", "expired charter revision cannot activate",
                )
            if status == "active":
                raise DriveGovernanceError(
                    "revision_already_active", "charter revision is already active",
                )
            if status in {"superseded", "revoked"}:
                raise DriveGovernanceError(
                    "revision_terminal", "terminal charter revision cannot reactivate",
                )
            if current is not None and revision.parent_revision_id != current.revision_id:
                raise DriveGovernanceError(
                    "stale_charter_parent",
                    "new charter parent is not the current active revision",
                )
            if current is None and revision.parent_revision_id:
                parent = conn.execute(
                    "SELECT charter_key FROM charter_revisions WHERE revision_id=?",
                    (revision.parent_revision_id,),
                ).fetchone()
                if parent is None or parent["charter_key"] != revision.charter_key:
                    raise DriveGovernanceError(
                        "unknown_charter_parent", "charter parent is unknown",
                    )
            for drive_id, _weight in revision.drive_weights:
                drive_row = conn.execute(
                    "SELECT payload_json FROM drive_definitions WHERE drive_id=?",
                    (drive_id,),
                ).fetchone()
                if drive_row is None:
                    raise DriveGovernanceError(
                        "drive_unknown", f"charter drive is unknown: {drive_id}",
                    )
                drive = DriveV1.from_payload(json.loads(drive_row["payload_json"]))
                drive_state = drive.state_at(now)
                if drive_state == "disabled":
                    raise DriveGovernanceError(
                        "drive_disabled", f"charter drive is disabled: {drive_id}",
                    )
                if drive_state == "expired":
                    raise DriveGovernanceError(
                        "drive_expired", f"charter drive is expired: {drive_id}",
                    )
                if not drive.scope.permits_child(revision.scope):
                    # A charter projection may be as private as, or more
                    # private than, its drive data. It must never make a
                    # private drive's existence visible more broadly.
                    raise DriveGovernanceError(
                        "scope_broadening",
                        "charter scope would broaden a referenced drive",
                    )
        elif transition == "revoke":
            if current is None or current.revision_id != revision.revision_id:
                raise DriveGovernanceError(
                    "revision_not_active", "only the active charter can be revoked",
                )
        else:
            raise DriveGovernanceError(
                "invalid_transition", "transition must be activate or revoke",
            )
        return current

    def preflight_transition(
        self,
        revision_id: str,
        *,
        transition: str,
        now: Optional[datetime] = None,
    ) -> tuple[CharterRevisionV1, Optional[CharterRevisionV1]]:
        revision = self.get_revision(revision_id)
        if revision is None:
            raise DriveGovernanceError(
                "revision_unknown", "charter revision is unknown",
            )
        normalized = str(transition or "").strip().lower()
        with self._lock:
            current = self._validate_transition_conn(
                self._conn, revision, normalized, _as_utc(now),
            )
        return revision, current

    def transition_operation(self, operation_id: str) -> Optional[Dict[str, Any]]:
        operation = _operation_id(operation_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM charter_transition_operations WHERE operation_id=?",
                (operation,),
            ).fetchone()
        return dict(row) if row else None

    def transition_authority_use(
        self, approval_request_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return validated application evidence for one approval request."""

        request_id = _identifier(
            approval_request_id, "approval_request_id",
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT u.approval_request_id,u.action_digest,u.operation_id,"
                "u.event_id,u.used_at,e.charter_key,e.revision_id,"
                "e.transition,e.decision_id,e.principal_id,e.payload_json,"
                "e.from_revision_id,e.authority_evidence,e.occurred_at "
                "FROM charter_authority_uses AS u "
                "JOIN charter_lifecycle_events AS e ON e.event_id=u.event_id "
                "WHERE u.approval_request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        payload = self._validate_lifecycle_event(row)
        result = dict(row)
        result.pop("payload_json", None)
        result["payload"] = payload
        return result

    @staticmethod
    def _transition_digest(
        *,
        revision_id: str,
        transition: str,
        binding: ActionBinding,
        approval_request_id: str,
    ) -> str:
        return _digest({
            "revision_id": revision_id,
            "transition": transition,
            "action_digest": binding.action_digest,
            "scope_digest": binding.scope_digest,
            "approval_request_id": approval_request_id,
        })

    def _apply_transition(
        self,
        revision: CharterRevisionV1,
        *,
        transition: str,
        binding: ActionBinding,
        approval_request: Mapping[str, Any],
        operation_id: str,
        principal_id: str,
        require_no_active_charter: bool = False,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Append an owner-ratified transition and atomically fence its use."""

        observed = _as_utc(now)
        operation = _operation_id(operation_id)
        request_id = _identifier(
            approval_request.get("request_id"), "approval_request_id",
        )
        decision_id = _identifier(
            approval_request.get("decision_id"), "decision_id",
        )
        evidence = _text(
            approval_request.get("authority_evidence"),
            "authority_evidence", 256,
        )
        normalized = str(transition or "").strip().lower()
        transition_digest = self._transition_digest(
            revision_id=revision.revision_id,
            transition=normalized,
            binding=binding,
            approval_request_id=request_id,
        )
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            prior = self._conn.execute(
                "SELECT * FROM charter_transition_operations WHERE operation_id=?",
                (operation,),
            ).fetchone()
            if prior is not None:
                if prior["transition_digest"] != transition_digest:
                    raise DriveGovernanceError(
                        "operation_replay_conflict",
                        "ratification operation replay changed its binding",
                    )
                projection = self._revision_projection_conn(
                    self._conn, revision.revision_id, observed,
                )
                projection.update({
                    "enabled": True,
                    "status": f"charter_{normalized}_replayed",
                    "event_id": prior["event_id"],
                    "replayed": True,
                })
                return projection

            used = self._conn.execute(
                "SELECT * FROM charter_authority_uses WHERE approval_request_id=?",
                (request_id,),
            ).fetchone()
            if used is not None:
                raise DriveGovernanceError(
                    "authority_replay",
                    "approval request was already consumed by another transition",
                )

            current = self._validate_transition_conn(
                self._conn, revision, normalized, observed,
            )
            if (
                require_no_active_charter
                and self._active_revisions_conn(self._conn, observed)
            ):
                raise DriveGovernanceError(
                    "bootstrap_transition_held",
                    "bootstrap may activate only the first global root charter",
                )
            event_payload = {
                "schema": "CharterLifecycleEventV1", "version": 1,
                "charter_key": revision.charter_key,
                "revision_id": revision.revision_id,
                "transition": normalized,
                "from_revision_id": (
                    current.revision_id if current is not None else None
                ),
                "action_digest": binding.action_digest,
                "approval_request_id": request_id,
                "decision_id": decision_id,
                "principal_id": principal_id,
                "authority_evidence": evidence,
                "occurred_at": _iso(observed),
            }
            event_id = f"charter-event:{_digest(event_payload)[:24]}"

            if normalized == "activate" and current is not None:
                supersede_payload = {
                    "schema": "CharterLifecycleEventV1", "version": 1,
                    "charter_key": revision.charter_key,
                    "revision_id": current.revision_id,
                    "transition": "supersede",
                    "from_revision_id": revision.revision_id,
                    "action_digest": binding.action_digest,
                    "approval_request_id": request_id,
                    "decision_id": decision_id,
                    "principal_id": principal_id,
                    "authority_evidence": evidence,
                    "occurred_at": _iso(observed),
                }
                supersede_id = (
                    f"charter-event:{_digest(supersede_payload)[:24]}"
                )
                self._conn.execute(
                    "INSERT INTO charter_lifecycle_events "
                    "(event_id,charter_key,revision_id,transition,from_revision_id,"
                    "action_digest,approval_request_id,decision_id,principal_id,"
                    "authority_evidence,occurred_at,payload_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        supersede_id, revision.charter_key, current.revision_id,
                        "supersede", revision.revision_id, binding.action_digest,
                        request_id, decision_id, principal_id, evidence,
                        _iso(observed), _canonical(supersede_payload),
                    ),
                )

            self._conn.execute(
                "INSERT INTO charter_lifecycle_events "
                "(event_id,charter_key,revision_id,transition,from_revision_id,"
                "action_digest,approval_request_id,decision_id,principal_id,"
                "authority_evidence,occurred_at,payload_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id, revision.charter_key, revision.revision_id,
                    normalized,
                    current.revision_id if current is not None else None,
                    binding.action_digest, request_id, decision_id, principal_id,
                    evidence, _iso(observed), _canonical(event_payload),
                ),
            )
            self._conn.execute(
                "INSERT INTO charter_authority_uses "
                "(approval_request_id,action_digest,operation_id,event_id,used_at) "
                "VALUES (?,?,?,?,?)",
                (
                    request_id, binding.action_digest, operation, event_id,
                    _iso(observed),
                ),
            )
            self._conn.execute(
                "INSERT INTO charter_transition_operations "
                "(operation_id,transition_digest,revision_id,transition,"
                "approval_request_id,event_id,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    operation, transition_digest, revision.revision_id,
                    normalized, request_id, event_id, _iso(observed),
                ),
            )
            projection = self._revision_projection_conn(
                self._conn, revision.revision_id, observed,
            )
            projection.update({
                "enabled": True,
                "status": f"charter_{normalized}d",
                "event_id": event_id,
                "replayed": False,
            })
            return projection

    def lifecycle_events(self, charter_key: str = "default") -> list[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM charter_lifecycle_events WHERE charter_key=? "
                "ORDER BY event_seq", (str(charter_key),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = self._validate_lifecycle_event(row)
            item.pop("payload_json")
            result.append(item)
        return result

    def observer_projection(
        self,
        *,
        viewer_person_id: str,
        owner_person_id: str,
        audiences: set[str] | frozenset[str] = frozenset(),
        charter_key: str = "default",
        now: Optional[datetime] = None,
        signal_limit: int = 100,
    ) -> Dict[str, Any]:
        observed = _as_utc(now)
        revisions = self.list_revision_projections(charter_key, now=observed)
        visible_revisions = [
            item for item in revisions
            if ScopeV1(**dict(item["scope"])).visible_to(
                viewer_person_id=viewer_person_id,
                owner_person_id=owner_person_id,
                audiences=audiences,
            )
        ]
        drives = [
            item for item in self.list_drives()
            if item.scope.visible_to(
                viewer_person_id=viewer_person_id,
                owner_person_id=owner_person_id,
                audiences=audiences,
            )
        ]
        bounded_limit = max(1, min(int(signal_limit), 500))
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM drive_signals "
                "ORDER BY observed_at DESC,signal_seq DESC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        signals = [
            item for item in (
                DriveSignalV1.from_payload(json.loads(row["payload_json"]))
                for row in rows
            )
            if item.scope.visible_to(
                viewer_person_id=viewer_person_id,
                owner_person_id=owner_person_id,
                audiences=audiences,
            )
        ]
        active = self.active_revision(charter_key, now=observed)
        active_visible = bool(
            active
            and active.scope.visible_to(
                viewer_person_id=viewer_person_id,
                owner_person_id=owner_person_id,
                audiences=audiences,
            )
        )
        return {
            "schema": "DriveGovernanceObserverV1", "version": 1,
            "charter_key": charter_key,
            "active_charter_revision_id": (
                active.revision_id if active_visible and active else None
            ),
            "charter_revisions": visible_revisions,
            "drives": [
                {**item.payload(), "effective_state": item.state_at(observed)}
                for item in drives
            ],
            "signals": [
                {**item.payload(), "effective_state": item.state_at(observed)}
                for item in signals
            ],
            "generated_at": _iso(observed),
        }


class DriveGovernance:
    """Mode-gated service reusing existing approvals for charter authority."""

    def __init__(
        self,
        store: Optional[DriveGovernanceStore],
        approval_store: Optional[ApprovalAuthorityStore],
        *,
        mode: Optional[str] = None,
    ) -> None:
        self.store = store
        self.approval_store = approval_store
        self.mode = _normalized_mode(mode)
        if self.mode != "off" and self.store is None:
            raise DriveGovernanceError(
                "store_required", "drive governance store is required",
            )

    @classmethod
    def lazy(
        cls,
        db_path: str | os.PathLike[str],
        *,
        approval_db_path: Optional[str | os.PathLike[str]] = None,
        mode: Optional[str] = None,
    ) -> "DriveGovernance":
        selected = _normalized_mode(mode)
        if selected == "off":
            # Default-off means no database, directory, or approval state is
            # created merely because a host imports or constructs the module.
            return cls(None, None, mode="off")
        store = DriveGovernanceStore(db_path)
        approval_store = ApprovalAuthorityStore(approval_db_path)
        return cls(store, approval_store, mode=selected)

    def _store(self) -> DriveGovernanceStore:
        if self.store is None:
            raise DriveGovernanceError(
                "store_required", "drive governance store is unavailable",
            )
        return self.store

    def register_drive(
        self,
        drive: DriveV1,
        *,
        operation_id: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if self.mode == "off":
            return {"enabled": False, "status": "off"}
        return self._store().register_drive(
            drive, operation_id=operation_id, now=now,
        )

    def record_signal(
        self,
        signal: DriveSignalV1,
        *,
        operation_id: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if self.mode == "off":
            return {"enabled": False, "status": "off"}
        if self.mode == "bootstrap":
            raise DriveGovernanceError(
                "bootstrap_operation_held",
                "drive signals are held during initial charter bootstrap",
            )
        return self._store().record_signal(
            signal, operation_id=operation_id, now=now,
        )

    def propose_charter(
        self,
        revision: CharterRevisionV1,
        *,
        operation_id: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if self.mode == "off":
            return {"enabled": False, "status": "off"}
        result = self._store().propose_revision(
            revision, operation_id=operation_id, now=now,
        )
        result.setdefault("status", (
            "charter_proposal_replayed" if result.get("replayed")
            else "charter_proposed"
        ))
        result["effect_executed"] = False
        return result

    def ensure_transition_request(
        self,
        revision_id: str,
        *,
        transition: str,
        ttl_seconds: int = 3600,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if self.mode == "off":
            return {"enabled": False, "status": "off"}
        observed = _as_utc(now)
        store = self._store()
        revision, current = store.preflight_transition(
            revision_id, transition=transition, now=observed,
        )
        normalized = str(transition).strip().lower()
        if self.mode == "bootstrap" and (
            normalized != "activate"
            or current is not None
            or revision.parent_revision_id is not None
            or store.active_revisions(now=observed)
        ):
            raise DriveGovernanceError(
                "bootstrap_transition_held",
                "bootstrap may activate only the first global root charter",
            )
        binding = charter_transition_binding(
            revision,
            transition=normalized,
            expected_active_revision_id=(
                current.revision_id if current is not None else None
            ),
        )
        if self.mode == "shadow":
            return {
                "enabled": True,
                "status": "shadow_transition_candidate",
                "transition": normalized,
                "revision_id": revision.revision_id,
                "action_digest": binding.action_digest,
                "scope_digest": binding.scope_digest,
                "expected_active_revision_id": (
                    current.revision_id if current else None
                ),
                "effect_executed": False,
            }
        if self.approval_store is None:
            raise DriveGovernanceError(
                "approval_store_required", "approval authority store is unavailable",
            )
        ttl = _bounded_int(
            ttl_seconds, "transition request ttl_seconds",
            minimum=60, maximum=24 * 60 * 60,
        )
        job_id = f"charter-transition:{binding.action_digest[:24]}"
        subject = charter_transition_subject(
            revision, transition=normalized,
        )
        presentation = charter_transition_presentation(
            revision, transition=normalized, binding=binding,
        )
        request = self.approval_store.ensure_request(
            job_id=job_id,
            binding=binding,
            ttl_seconds=ttl,
            presentation=presentation,
            subject=subject,
            now=observed,
        )
        return {
            **request,
            "enabled": True,
            "transition": normalized,
            "revision_id": revision.revision_id,
            "effect_executed": False,
        }

    def _validated_transition_approval(
        self,
        request_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> tuple[Dict[str, Any], CharterRevisionV1, str, bool, Optional[Dict[str, Any]]]:
        if self.approval_store is None:
            raise DriveGovernanceError(
                "approval_store_required",
                "approval authority store is unavailable",
            )
        observed = _as_utc(now)
        request = self.approval_store.get_request(request_id, now=observed)
        if request is None:
            raise DriveGovernanceError(
                "approval_request_missing", "approval request was not found",
            )
        subject = request.get("subject")
        if not isinstance(subject, Mapping) or subject.get("kind") != \
                "charter_transition":
            raise DriveGovernanceError(
                "approval_subject_unavailable",
                "approval request is not a charter transition subject",
            )
        normalized = str(subject.get("action") or "").strip().lower()
        if normalized not in _TRANSITIONS:
            raise DriveGovernanceError(
                "approval_subject_invalid",
                "charter transition approval action is invalid",
            )
        revision = self._store().get_revision(str(subject.get("subject_id") or ""))
        if revision is None or subject.get("revision") != revision.content_digest:
            raise DriveGovernanceError(
                "approval_subject_unavailable",
                "charter transition approval subject is orphaned",
            )
        expected_subject = charter_transition_subject(
            revision, transition=normalized,
        ).payload()
        if dict(subject) != expected_subject:
            raise DriveGovernanceError(
                "approval_subject_invalid",
                "charter transition approval subject binding is invalid",
            )

        scope = request.get("scope")
        constraints = scope.get("constraints") if isinstance(scope, Mapping) else None
        expected_constraints = {
            "charter_key_digest": _digest(revision.charter_key),
            "revision_id_digest": _digest(revision.revision_id),
            "content_digest": revision.content_digest,
        }
        valid_scope = bool(
            isinstance(scope, Mapping)
            and set(scope) == {
                "version", "job_type", "action_name", "risk", "constraints",
            }
            and scope.get("version") == 1
            and scope.get("job_type") == "charter_governance"
            and scope.get("action_name") == f"charter_revision_{normalized}"
            and scope.get("risk") == "authority_mutation"
            and isinstance(constraints, Mapping)
            and set(constraints) == {
                *expected_constraints,
                "expected_active_revision_digest",
            }
            and all(
                constraints.get(key) == value
                for key, value in expected_constraints.items()
            )
            and re.fullmatch(
                r"[0-9a-f]{64}",
                str(constraints.get("expected_active_revision_digest") or ""),
            ) is not None
            and _digest(scope) == request.get("scope_digest")
        )
        if not valid_scope:
            raise DriveGovernanceError(
                "approval_binding_invalid",
                "charter transition approval scope is invalid",
            )
        expected_presentation = charter_transition_presentation(
            revision,
            transition=normalized,
            binding=ActionBinding(
                action_digest=str(request.get("action_digest") or ""),
                scope=dict(scope),
                scope_digest=str(request.get("scope_digest") or ""),
            ),
        )
        if request.get("presentation") != expected_presentation:
            raise DriveGovernanceError(
                "approval_presentation_invalid",
                "charter transition approval presentation is invalid",
            )
        if request.get("job_id") != (
            f"charter-transition:{str(request.get('action_digest') or '')[:24]}"
        ):
            raise DriveGovernanceError(
                "approval_binding_invalid",
                "charter transition approval identifier is invalid",
            )
        if request.get("status") in {"approved", "rejected"}:
            decided_by = str(request.get("decided_by") or "").strip()
            evidence = str(request.get("authority_evidence") or "").strip()
            evidence_prefix = f"scoped_principal:{decided_by}:"
            credential_id = (
                evidence[len(evidence_prefix):]
                if decided_by and evidence.startswith(evidence_prefix)
                else ""
            )
            try:
                _identifier(decided_by, "decided_by")
                _identifier(credential_id, "decision_credential_id")
            except DriveGovernanceError as exc:
                raise DriveGovernanceError(
                    "approval_authority_integrity_error",
                    "charter transition decision authority is invalid",
                ) from exc

        use = self._store().transition_authority_use(request["request_id"])
        if use is not None:
            from_revision_id = use.get("from_revision_id")
            applied_binding = charter_transition_binding(
                revision,
                transition=normalized,
                expected_active_revision_id=from_revision_id,
            )
            if (
                use.get("revision_id") != revision.revision_id
                or use.get("transition") != normalized
                or use.get("action_digest") != request.get("action_digest")
                or applied_binding.action_digest != request.get("action_digest")
                or applied_binding.scope_digest != request.get("scope_digest")
            ):
                raise DriveGovernanceError(
                    "approval_application_integrity_error",
                    "charter transition application does not match its approval",
                )
            current_binding_matches = False
        else:
            store = self._store()
            current = store.active_revision(
                revision.charter_key, now=observed,
            )
            current_binding = charter_transition_binding(
                revision,
                transition=normalized,
                expected_active_revision_id=(
                    current.revision_id if current is not None else None
                ),
            )
            current_binding_matches = bool(
                current_binding.action_digest == request.get("action_digest")
                and current_binding.scope_digest == request.get("scope_digest")
                and not (
                    self.mode == "bootstrap"
                    and store.active_revisions(now=observed)
                )
            )
        return request, revision, normalized, current_binding_matches, use

    def transition_approval_projection(
        self,
        request_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Return one redacted typed approval, never a raw authority row."""

        observed = _as_utc(now)
        request, revision, transition, binding_current, use = \
            self._validated_transition_approval(request_id, now=observed)
        if use is not None:
            status = "approved_applied"
            application = {
                "status": "applied",
                "event_id": use["event_id"],
                "operation_id": use["operation_id"],
                "applied_at": use["used_at"],
            }
        elif request["status"] == "approved" and binding_current:
            status = "approved_unapplied"
            application = {
                "status": "recovery_required",
                "event_id": None,
                "operation_id": None,
                "applied_at": None,
            }
        elif request["status"] == "approved":
            status = "approved_stale"
            application = {
                "status": "new_request_required",
                "event_id": None,
                "operation_id": None,
                "applied_at": None,
            }
        elif request["status"] == "rejected":
            status = "rejected"
            application = {
                "status": "not_applicable",
                "event_id": None,
                "operation_id": None,
                "applied_at": None,
            }
        elif request["status"] == "pending" and not binding_current:
            status = "stale_pending"
            application = {
                "status": "binding_changed",
                "event_id": None,
                "operation_id": None,
                "applied_at": None,
            }
        else:
            status = request["status"]
            application = {
                "status": "awaiting_decision",
                "event_id": None,
                "operation_id": None,
                "applied_at": None,
            }
        return {
            "schema": CHARTER_TRANSITION_APPROVAL_PROJECTION_SCHEMA,
            "version": 1,
            "subject_kind": "charter_transition",
            "subject_id": revision.revision_id,
            "subject_revision": revision.content_digest,
            "subject_action": transition,
            "subject_digest": request["subject_digest"],
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
            "request_digest_version": request["request_digest_version"],
            "action_digest": request["action_digest"],
            "scope_digest": request["scope_digest"],
            "binding_digest": request["binding_digest"],
            "presentation": request["presentation"],
            "presentation_digest": request["presentation_digest"],
            "status": status,
            "authority_status": request["status"],
            "binding_current": binding_current,
            "created_at": request["created_at"],
            "expires_at": request["expires_at"],
            "decision": request.get("decision"),
            "decision_id": request.get("decision_id"),
            "decided_at": request.get("decided_at"),
            "decided_by": request.get("decided_by"),
            "authority_evidence": request.get("authority_evidence"),
            "application": application,
            "observed_at": _iso(observed),
        }

    def list_transition_approval_projections(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 100,
        now: Optional[datetime] = None,
    ) -> list[Dict[str, Any]]:
        return self.transition_approval_inventory(
            status=status, limit=limit, now=now,
        )["requests"]

    def transition_approval_inventory(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 100,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Return valid projections plus a bounded count of hidden invalids."""

        if self.approval_store is None:
            return {
                "requests": [], "invalid_hidden_count": 0,
                "candidate_count": 0, "complete": True,
            }
        observed = _as_utc(now)
        inspection = self.approval_store.inspect_subject_requests(
            subject_kind="charter_transition",
            job_prefix="charter-transition:",
            limit=500,
            now=observed,
        )
        candidates = inspection["requests"]
        invalid_hidden_count = int(inspection["invalid_hidden_count"])
        projections: list[Dict[str, Any]] = []
        bounded_limit = max(1, min(int(limit), 500))
        projection_overflow = False
        for candidate in candidates:
            try:
                projection = self.transition_approval_projection(
                    candidate["request_id"], now=observed,
                )
            except DriveGovernanceError as exc:
                if exc.code in {
                    "approval_subject_unavailable", "approval_subject_invalid",
                    "approval_binding_invalid", "approval_presentation_invalid",
                    "approval_application_integrity_error",
                    "approval_authority_integrity_error",
                }:
                    invalid_hidden_count += 1
                    continue
                raise
            if status and projection["status"] != status:
                continue
            if len(projections) < bounded_limit:
                projections.append(projection)
            else:
                projection_overflow = True
        return {
            "requests": projections,
            "invalid_hidden_count": invalid_hidden_count,
            "candidate_count": int(inspection["candidate_count"]),
            "complete": bool(
                inspection["complete"] and not projection_overflow
            ),
        }

    @staticmethod
    def _validate_transition_decision_authority(authority: Any) -> tuple[str, str]:
        allowed = bool(
            authority is not None
            and getattr(authority, "authenticated", False)
            and not getattr(authority, "legacy", False)
            and not getattr(authority, "anonymous", False)
            and callable(getattr(authority, "has_scope", None))
            and authority.has_scope("charter:approval-decide")
            and "owner" in set(getattr(authority, "audiences", ()))
            and str(getattr(authority, "principal_id", "")).strip()
            and str(getattr(authority, "credential_id", "")).strip()
        )
        if not allowed:
            raise DriveGovernanceError(
                "owner_charter_approval_authority_required",
                "scoped authenticated owner charter approval authority is required",
            )
        return (
            str(authority.principal_id).strip(),
            str(authority.credential_id).strip(),
        )

    def decide_transition_request(
        self,
        request_id: str,
        *,
        decision: str,
        decision_id: str,
        expected_action_digest: str,
        expected_request_digest: str,
        authority: Any,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Decide and converge one exact charter transition without grants."""

        if self.mode not in {"bootstrap", "live"}:
            raise DriveGovernanceError(
                "transition_not_authoritative",
                "charter transitions require bootstrap or live mode",
            )
        principal, credential = self._validate_transition_decision_authority(
            authority,
        )
        observed = _as_utc(now)
        request, revision, transition, binding_current, use = \
            self._validated_transition_approval(request_id, now=observed)
        if expected_action_digest != request["action_digest"]:
            raise DriveGovernanceError(
                "stale_action_digest",
                "decision does not match the immutable charter transition",
            )
        if expected_request_digest != request["request_digest"]:
            raise DriveGovernanceError(
                "stale_request_digest",
                "decision does not match the displayed subject, presentation, and expiry",
            )
        if request["status"] == "pending" and not binding_current:
            raise DriveGovernanceError(
                "approval_binding_stale",
                "charter transition binding changed before the decision",
            )
        if self.approval_store is None:
            raise DriveGovernanceError(
                "approval_store_required", "approval authority store is unavailable",
            )
        self.approval_store.decide(
            request["request_id"],
            decision=decision,
            decision_id=decision_id,
            expected_action_digest=expected_action_digest,
            decided_by=principal,
            authority_evidence=f"scoped_principal:{principal}:{credential}",
            grant_scope=None,
            now=observed,
        )
        if str(decision).strip().lower() == "approve" and use is None:
            operation_id = "charter-decision:" + _digest({
                "request_id": request["request_id"],
                "decision_id": decision_id,
            })[:32]
            self.ratify_transition(
                revision.revision_id,
                transition=transition,
                approval_request_id=request["request_id"],
                operation_id=operation_id,
                authority=authority,
                now=observed,
            )
        return self.transition_approval_projection(
            request["request_id"], now=observed,
        )

    @staticmethod
    def _validate_owner_authority(authority: Any) -> tuple[str, str]:
        allowed = bool(
            authority is not None
            and getattr(authority, "authenticated", False)
            and not getattr(authority, "legacy", False)
            and not getattr(authority, "anonymous", False)
            and callable(getattr(authority, "has_scope", None))
            and (
                authority.has_scope("approvals:decide")
                or authority.has_scope("charter:approval-decide")
            )
            and "owner" in set(getattr(authority, "audiences", ()))
            and str(getattr(authority, "principal_id", "")).strip()
            and str(getattr(authority, "credential_id", "")).strip()
        )
        if not allowed:
            raise DriveGovernanceError(
                "owner_authority_required",
                "scoped authenticated owner approval authority is required",
            )
        principal = str(authority.principal_id).strip()
        credential = str(authority.credential_id).strip()
        return principal, credential

    def ratify_transition(
        self,
        revision_id: str,
        *,
        transition: str,
        approval_request_id: str,
        operation_id: str,
        authority: Any,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Activate/revoke only from an exact owner-scoped approval decision."""

        if self.mode not in {"bootstrap", "live"}:
            raise DriveGovernanceError(
                "transition_not_authoritative",
                "charter transitions require bootstrap or live mode",
            )
        principal, credential = self._validate_owner_authority(authority)
        normalized = str(transition or "").strip().lower()
        if normalized not in _TRANSITIONS:
            raise DriveGovernanceError(
                "invalid_transition", "transition must be activate or revoke",
            )
        request_id = _identifier(
            approval_request_id, "approval_request_id",
        )
        operation = _operation_id(operation_id)
        store = self._store()

        # Exact retries remain idempotent after the lifecycle commit. A changed
        # request, revision, or transition using the same operation is refused.
        prior = store.transition_operation(operation)
        if prior is not None:
            if (
                prior["revision_id"] != revision_id
                or prior["transition"] != normalized
                or prior["approval_request_id"] != request_id
            ):
                raise DriveGovernanceError(
                    "operation_replay_conflict",
                    "ratification operation replay changed its immutable first use",
                )
            projection = store.revision_projection(revision_id, now=now)
            projection.update({
                "enabled": True,
                "status": f"charter_{normalized}_replayed",
                "event_id": prior["event_id"],
                "replayed": True,
            })
            return projection

        revision = store.get_revision(revision_id)
        if revision is None:
            raise DriveGovernanceError(
                "revision_unknown", "charter revision is unknown",
            )
        observed = _as_utc(now)
        current = store.active_revision(revision.charter_key, now=observed)
        if self.mode == "bootstrap" and (
            normalized != "activate"
            or current is not None
            or revision.parent_revision_id is not None
            or store.active_revisions(now=observed)
        ):
            raise DriveGovernanceError(
                "bootstrap_transition_held",
                "bootstrap may activate only the first global root charter",
            )
        binding = charter_transition_binding(
            revision,
            transition=normalized,
            expected_active_revision_id=(
                current.revision_id if current is not None else None
            ),
        )
        if self.approval_store is None:
            raise DriveGovernanceError(
                "approval_store_required", "approval authority store is unavailable",
            )
        request = self.approval_store.get_request(request_id, now=observed)
        if request is None:
            raise DriveGovernanceError(
                "approval_request_missing", "approval request was not found",
            )
        expires_at = _parse_time(request.get("expires_at"), field="approval expires_at")
        decided_at = (
            _parse_time(request.get("decided_at"), field="approval decided_at")
            if request.get("decided_at") else None
        )
        if (
            observed >= expires_at
            and not (
                request.get("status") == "approved"
                and request.get("decision") == "approve"
                and decided_at is not None
                and decided_at < expires_at
            )
        ):
            raise DriveGovernanceError(
                "approval_expired", "charter approval window has expired",
            )
        if request.get("status") != "approved" or request.get("decision") != "approve":
            raise DriveGovernanceError(
                "approval_not_approved", "charter transition is not approved",
            )
        if (
            request.get("action_digest") != binding.action_digest
            or request.get("scope_digest") != binding.scope_digest
            or _canonical(request.get("scope")) != _canonical(binding.scope)
        ):
            raise DriveGovernanceError(
                "approval_binding_mismatch",
                "approval does not bind the current immutable charter transition",
            )
        if (
            request.get("decided_by") != principal
            or not str(request.get("authority_evidence") or "").startswith(
                f"scoped_principal:{principal}:"
            )
        ):
            raise DriveGovernanceError(
                "owner_authority_mismatch",
                "approval decision does not match the server-derived owner authority",
            )
        return store._apply_transition(
            revision,
            transition=normalized,
            binding=binding,
            approval_request=request,
            operation_id=operation,
            principal_id=principal,
            require_no_active_charter=(self.mode == "bootstrap"),
            now=observed,
        )


class DriveRanker:
    """Deterministically order goals that P3 has already admitted.

    ``policy_decision_resolver`` must resolve durable decision references from
    ``CognitionSpineStore``. Caller-supplied booleans are never accepted as
    policy evidence. ``directive_manager`` is rechecked immediately before a
    ranking so a later boundary or global pause outranks an earlier P3 gate.
    """

    def __init__(
        self,
        store: DriveGovernanceStore,
        *,
        policy_decision_resolver: Optional[
            Callable[[str], Optional[Mapping[str, Any]]]
        ],
        directive_manager: Any,
        charter_key: str = "default",
    ) -> None:
        self.store = store
        self._resolve_policy = policy_decision_resolver
        self._directives = directive_manager
        self.charter_key = str(charter_key or "default")

    @staticmethod
    def _batch(
        *,
        status: str,
        mode: str,
        applied: bool,
        charter: Optional[CharterRevisionV1],
        goals: Sequence[GoalRankInputV1],
        suggested: Iterable[str] = (),
        effective: Optional[Iterable[str]] = None,
        held: Iterable[str] = (),
        results: Iterable[GoalRankResultV1] = (),
        now: datetime,
    ) -> RankingBatchV1:
        input_order = tuple(item.goal_id for item in goals)
        return RankingBatchV1(
            status=status,
            mode=mode,
            ranking_applied=applied,
            charter_revision_id=(charter.revision_id if charter else None),
            input_order=input_order,
            suggested_order=tuple(suggested),
            effective_order=(
                tuple(effective) if effective is not None else input_order
            ),
            held_goal_ids=tuple(held),
            results=tuple(results),
            generated_at=_iso(now),
        )

    def _p3_gate_state(
        self, goal: GoalRankInputV1,
    ) -> tuple[str, tuple[str, ...]]:
        if self._resolve_policy is None or not goal.policy_decision_refs:
            return "p3_policy_unknown", ()
        stages: Dict[str, Mapping[str, Any]] = {}
        evidence: list[str] = []
        for reference in goal.policy_decision_refs:
            try:
                resolved = self._resolve_policy(reference)
            except Exception:
                return "p3_policy_unknown", ()
            if resolved is None or not isinstance(resolved, Mapping):
                return "p3_policy_unknown", ()
            payload = resolved.get("payload", resolved)
            if not isinstance(payload, Mapping):
                return "p3_policy_conflict", ()
            if (
                payload.get("decision_ref") != reference
                or payload.get("proposal_id") != goal.proposal_id
                or not isinstance(payload.get("allowed"), bool)
            ):
                return "p3_policy_conflict", ()
            stage = str(payload.get("stage") or "")
            if stage in stages:
                return "p3_policy_conflict", ()
            stages[stage] = payload
            try:
                evidence.extend(_refs(
                    payload.get("evidence_refs") or (),
                    field="policy evidence", maximum=30,
                ))
            except DriveGovernanceError:
                return "p3_policy_conflict", ()
        if any(stage not in stages for stage in _REQUIRED_P3_STAGES):
            return "p3_policy_unknown", tuple(dict.fromkeys(evidence))
        for stage in _REQUIRED_P3_STAGES:
            if not stages[stage]["allowed"]:
                return f"p3_{stage}_denied", tuple(dict.fromkeys(evidence))
        return "p3_eligible", tuple(dict.fromkeys(evidence))

    def _directive_state(self, goal: GoalRankInputV1) -> tuple[str, str]:
        if self._directives is None:
            return "unknown", "directive_manager_unavailable"
        try:
            from colony_sidecar.directives import Action

            verdict = self._directives.check(Action(
                kind="project",
                text=f"{goal.title} {goal.objective_summary}",
                target=goal.title,
                high_risk=True,
            ))
        except Exception:
            return "unknown", "directive_check_failed"
        if bool(getattr(verdict, "allowed", False)):
            return "allowed", str(
                getattr(verdict, "reason", "boundary_allowed")
            )[:500]
        reason = str(getattr(verdict, "reason", "boundary_denied"))[:500]
        if reason == "global_pause_active":
            return "paused", reason
        return "denied", reason

    @staticmethod
    def _held_result(
        goal: GoalRankInputV1,
        *,
        state: str,
        policy_evidence: Iterable[str] = (),
        charter: Optional[CharterRevisionV1] = None,
    ) -> GoalRankResultV1:
        scope = goal.scope
        if charter is not None:
            scope = _narrower_scope(scope, charter.scope)
        evidence = tuple(dict.fromkeys([
            *goal.evidence_refs, *policy_evidence,
            *(charter.evidence_refs if charter else ()),
        ]))[:30]
        return GoalRankResultV1(
            goal_id=goal.goal_id,
            proposal_id=goal.proposal_id,
            state=state,
            eligible=False,
            total_score=None,
            contributions=(),
            drive_states={},
            evidence_refs=evidence,
            policy_decision_refs=goal.policy_decision_refs,
            rationale_summary=(
                "Goal was not ranked because a required deterministic gate "
                f"reported {state}."
            ),
            scope=scope,
        )

    def rank(
        self,
        goals: Sequence[GoalRankInputV1],
        *,
        mode: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> RankingBatchV1:
        selected_mode = _normalized_mode(mode)
        observed = _as_utc(now)
        bounded_goals = tuple(goals)
        if len({item.goal_id for item in bounded_goals}) != len(bounded_goals):
            raise DriveGovernanceError(
                "duplicate_goal", "ranking input repeats a goal id",
            )
        if selected_mode == "off":
            return self._batch(
                status="off", mode="off", applied=False, charter=None,
                goals=bounded_goals, now=observed,
            )

        # Pause is checked even when charter state is missing or expired. P7
        # must never turn an unavailable ranking policy into a pause bypass.
        if self._directives is None:
            return self._batch(
                status="pause_state_unknown", mode=selected_mode,
                applied=False, charter=None, goals=bounded_goals,
                now=observed,
            )
        directive_states: Dict[str, tuple[str, str]] = {}
        directive_unknown = False
        globally_paused = False
        for goal in bounded_goals:
            directive_states[goal.goal_id] = self._directive_state(goal)
            if directive_states[goal.goal_id][0] == "unknown":
                directive_unknown = True
            if directive_states[goal.goal_id][0] == "paused":
                globally_paused = True
        if directive_unknown:
            return self._batch(
                status="pause_state_unknown", mode=selected_mode,
                applied=False, charter=None, goals=bounded_goals,
                now=observed,
            )
        if globally_paused:
            results = tuple(
                self._held_result(goal, state="global_pause_active")
                for goal in bounded_goals
            )
            return self._batch(
                status="global_pause_active", mode=selected_mode,
                applied=False, charter=None, goals=bounded_goals,
                effective=(), held=(item.goal_id for item in bounded_goals),
                results=results, now=observed,
            )

        charter = self.store.active_revision(self.charter_key, now=observed)
        if charter is None:
            return self._batch(
                status="charter_unknown", mode=selected_mode, applied=False,
                charter=None, goals=bounded_goals, now=observed,
            )
        budget = charter.ranking_budget
        if len(bounded_goals) > budget.max_goals:
            return self._batch(
                status="goal_budget_exceeded", mode=selected_mode,
                applied=False, charter=charter, goals=bounded_goals,
                now=observed,
            )
        results: list[GoalRankResultV1] = []
        total_signals_used = 0
        weights = charter.weight_map()
        drives: Dict[str, Optional[DriveV1]] = {
            drive_id: self.store.get_drive(drive_id) for drive_id in weights
        }
        for goal in bounded_goals:
            gate_state, policy_evidence = self._p3_gate_state(goal)
            if gate_state != "p3_eligible":
                results.append(self._held_result(
                    goal, state=gate_state,
                    policy_evidence=policy_evidence, charter=charter,
                ))
                continue
            if directive_states[goal.goal_id][0] == "denied":
                results.append(self._held_result(
                    goal,
                    state="boundary_recheck_denied",
                    policy_evidence=policy_evidence,
                    charter=charter,
                ))
                continue

            result_scope = _narrower_scope(goal.scope, charter.scope)
            evidence: list[str] = list(dict.fromkeys([
                *goal.evidence_refs, *policy_evidence, *charter.evidence_refs,
            ]))[:budget.max_evidence_refs_per_goal]
            drive_states: Dict[str, str] = {}
            contributions: list[DriveContributionV1] = []
            goal_signals = self.store.signals_for_goal(
                goal.goal_fingerprint, now=observed, include_expired=True,
            )
            grouped: Dict[str, list[DriveSignalV1]] = {}
            for item in goal_signals:
                grouped.setdefault(item.drive_id, []).append(item)

            total_score = 0.0
            for drive_id, weight in sorted(weights.items()):
                drive = drives.get(drive_id)
                if drive is None:
                    drive_state = "unknown"
                    drive_states[drive_id] = drive_state
                    contributions.append(DriveContributionV1(
                        drive_id=drive_id, state=drive_state, weight=weight,
                        raw_signal_value=0.0, mean_confidence=0.0,
                        weighted_value=0.0, abs_budget=0.0,
                        signal_refs=(), evidence_refs=(), rationale_summaries=(),
                    ))
                    continue
                effective_drive_state = drive.state_at(observed)
                if effective_drive_state != "enabled":
                    drive_states[drive_id] = effective_drive_state
                    contributions.append(DriveContributionV1(
                        drive_id=drive_id, state=effective_drive_state,
                        weight=weight, raw_signal_value=0.0,
                        mean_confidence=0.0, weighted_value=0.0,
                        abs_budget=drive.max_abs_contribution,
                        signal_refs=(), evidence_refs=(), rationale_summaries=(),
                    ))
                    continue
                candidates = [
                    item for item in grouped.get(drive_id, ())
                    if item.state_at(observed) == "active"
                ]
                limit = min(
                    drive.max_signals_per_goal,
                    budget.max_signals_per_drive,
                    max(0, budget.max_total_signals - total_signals_used),
                )
                selected = candidates[:limit]
                if not selected:
                    observed_states = {
                        item.state_at(observed)
                        for item in grouped.get(drive_id, ())
                    }
                    if candidates and limit == 0:
                        drive_state = "budget_exhausted"
                    elif "unknown" in observed_states:
                        drive_state = "unknown"
                    elif "disabled" in observed_states:
                        drive_state = "disabled"
                    elif "expired" in observed_states:
                        drive_state = "expired"
                    else:
                        drive_state = "unknown"
                    drive_states[drive_id] = drive_state
                    contributions.append(DriveContributionV1(
                        drive_id=drive_id, state=drive_state, weight=weight,
                        raw_signal_value=0.0, mean_confidence=0.0,
                        weighted_value=0.0,
                        abs_budget=drive.max_abs_contribution,
                        signal_refs=(), evidence_refs=(), rationale_summaries=(),
                    ))
                    continue
                total_signals_used += len(selected)
                raw_value = sum(
                    item.normalized_value for item in selected
                ) / len(selected)
                mean_confidence = sum(
                    item.confidence for item in selected
                ) / len(selected)
                evidence_adjusted = sum(
                    item.normalized_value * item.confidence for item in selected
                ) / len(selected)
                weighted = max(
                    -drive.max_abs_contribution,
                    min(drive.max_abs_contribution, evidence_adjusted * weight),
                )
                weighted = round(weighted, 8)
                total_score += weighted
                drive_states[drive_id] = "active"
                raw_contribution_evidence = tuple(dict.fromkeys(
                    ref for item in selected for ref in item.evidence_refs
                ))
                visible_contribution_evidence: list[str] = []
                for reference in raw_contribution_evidence:
                    if reference in evidence:
                        visible_contribution_evidence.append(reference)
                    elif len(evidence) < budget.max_evidence_refs_per_goal:
                        evidence.append(reference)
                        visible_contribution_evidence.append(reference)
                contribution_evidence = tuple(visible_contribution_evidence)
                for item in selected:
                    result_scope = _narrower_scope(result_scope, item.scope)
                contributions.append(DriveContributionV1(
                    drive_id=drive_id,
                    state="active",
                    weight=weight,
                    raw_signal_value=round(raw_value, 8),
                    mean_confidence=round(mean_confidence, 8),
                    weighted_value=weighted,
                    abs_budget=drive.max_abs_contribution,
                    signal_refs=tuple(item.signal_id for item in selected),
                    evidence_refs=contribution_evidence,
                    rationale_summaries=tuple(
                        item.rationale_summary for item in selected
                    ),
                ))
            evidence_refs = tuple(evidence)
            results.append(GoalRankResultV1(
                goal_id=goal.goal_id,
                proposal_id=goal.proposal_id,
                state="ranked",
                eligible=True,
                total_score=round(max(-1.0, min(1.0, total_score)), 8),
                contributions=tuple(contributions),
                drive_states=drive_states,
                evidence_refs=evidence_refs,
                policy_decision_refs=goal.policy_decision_refs,
                rationale_summary=(
                    "Weighted score uses only active, unexpired, "
                    "evidence-referenced drive signals within the ratified "
                    "charter budgets."
                ),
                scope=result_scope,
            ))

        eligible = [item for item in results if item.eligible]
        ordered = sorted(
            eligible,
            key=lambda item: (
                -(item.total_score if item.total_score is not None else -1.0),
                item.goal_id,
            ),
        )
        suggested = tuple(item.goal_id for item in ordered)
        held = tuple(item.goal_id for item in results if not item.eligible)
        applied = selected_mode == "live"
        effective = suggested if applied else tuple(
            item.goal_id for item in bounded_goals
        )
        return self._batch(
            status="ranked" if applied else "shadow_ranked",
            mode=selected_mode,
            applied=applied,
            charter=charter,
            goals=bounded_goals,
            suggested=suggested,
            effective=effective,
            held=held,
            results=results,
            now=observed,
        )


__all__ = [
    "CHARTER_TRANSITION_APPROVAL_PROJECTION_SCHEMA",
    "CharterAdmissionConstraintsV1", "CharterRevisionV1",
    "DriveContributionV1", "DriveGovernance",
    "DriveGovernanceError", "DriveGovernanceStore", "DriveRanker",
    "DriveSignalV1", "DriveV1", "GoalRankInputV1", "GoalRankResultV1",
    "RankingBatchV1", "RankingBudgetV1", "ScopeV1",
    "charter_transition_binding", "charter_transition_presentation",
    "charter_transition_subject", "drive_governance_mode",
]
