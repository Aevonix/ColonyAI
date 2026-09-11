"""P8 fact-level provenance and recipient visibility.

This module is deliberately a small, pure boundary primitive.  It does not
authenticate a caller, retrieve data, rank memories, or grant authority.  A
host supplies an already-attested :class:`ViewerContextV1`; facts are filtered
before any relevance, prose generation, or recipient simulation can consume
their content.

``FactVisibilityV1`` is immutable and binds one fact content digest to one
source, subject, exact viewer scope, freshness window, confidence, and bounded
evidence set.  A changed fact or widened audience requires a new record and a
new audit digest.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping, Optional, Sequence


SCHEMA_VERSION = 1
SHAREABILITIES = frozenset({
    "owner_private", "subject_private", "shared", "public",
})
MAX_EVIDENCE_REFS = 32
MAX_VIEWER_AUDIENCES = 16
MAX_FACT_CONTENT_CHARS = 4_000
MAX_CANDIDATES = 512
MAX_PROJECTED_FACTS = 64
MAX_PROJECTED_CHARS = 24_000

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,191}$")
_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")


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


def content_digest(content: str) -> str:
    """Bind exact UTF-8 fact content without retaining it in scope records."""

    if not isinstance(content, str):
        raise ValueError("fact content must be text")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _safe_ref(value: Any, *, field: str, allow_empty: bool = False) -> str:
    normalized = str(value or "").strip()
    if allow_empty and not normalized:
        return ""
    if not _REF_RE.fullmatch(normalized):
        raise ValueError(f"{field} is not a bounded opaque reference")
    return normalized


def _refs(values: Iterable[Any], *, field: str, maximum: int) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(
        _safe_ref(value, field=field) for value in values
    ))
    if len(normalized) > maximum:
        raise ValueError(f"{field} exceeds bounded reference limit")
    return tuple(sorted(normalized))


def _as_utc(value: datetime | str, *, field: str) -> datetime:
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


def _iso(value: datetime | str, *, field: str) -> str:
    return _as_utc(value, field=field).isoformat()


def validate_visibility_scope(
    *,
    viewer_scope: str,
    shareability: str,
    subject_person_id: str,
) -> tuple[str, str, str]:
    """Normalize and enforce one non-widening shareability/scope pairing."""

    subject = _safe_ref(subject_person_id, field="subject_person_id")
    scope = _safe_ref(viewer_scope, field="viewer_scope")
    share = str(shareability or "").strip().lower()
    if share not in SHAREABILITIES:
        raise ValueError("shareability is not a supported P8 value")
    if share == "owner_private" and scope != "owner":
        raise ValueError("owner_private visibility requires viewer_scope=owner")
    if share == "subject_private" and scope != f"person:{subject}":
        raise ValueError(
            "subject_private visibility requires the exact subject person")
    if share == "shared" and not (
        scope.startswith("person:")
        or scope == "audience:shared"
        or scope.startswith("conversation:")
    ):
        raise ValueError(
            "shared visibility requires an exact person, conversation, or "
            "shared audience scope")
    if share == "public" and scope not in {"public", "audience:global"}:
        raise ValueError("public visibility requires public/global scope")
    return scope, share, subject


@dataclass(frozen=True, slots=True)
class ViewerContextV1:
    """Server-attested viewer input; constructing it grants no authority."""

    principal_id: str
    viewer_person_id: str
    owner_person_id: str
    audiences: tuple[str, ...] = ()
    conversation_scope: str = ""
    scope_revision: str = ""
    attested: bool = False
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported viewer context version")
        principal = _safe_ref(
            self.principal_id, field="principal_id", allow_empty=not self.attested)
        viewer = _safe_ref(
            self.viewer_person_id,
            field="viewer_person_id",
            allow_empty=not self.attested,
        )
        owner = _safe_ref(
            self.owner_person_id,
            field="owner_person_id",
            allow_empty=not self.attested,
        )
        revision = _safe_ref(
            self.scope_revision,
            field="scope_revision",
            allow_empty=not self.attested,
        )
        conversation = _safe_ref(
            self.conversation_scope,
            field="conversation_scope",
            allow_empty=True,
        )
        audiences = _refs(
            self.audiences,
            field="audience",
            maximum=MAX_VIEWER_AUDIENCES,
        )
        if self.attested and not (principal and viewer and owner and revision):
            raise ValueError(
                "attested viewer requires principal, viewer, owner, and revision")
        object.__setattr__(self, "principal_id", principal)
        object.__setattr__(self, "viewer_person_id", viewer)
        object.__setattr__(self, "owner_person_id", owner)
        object.__setattr__(self, "scope_revision", revision)
        object.__setattr__(self, "conversation_scope", conversation)
        object.__setattr__(self, "audiences", audiences)

    @property
    def audit_digest(self) -> str:
        return _digest({
            "schema_version": self.schema_version,
            "principal_id": self.principal_id,
            "viewer_person_id": self.viewer_person_id,
            "owner_person_id": self.owner_person_id,
            "audiences": self.audiences,
            "conversation_scope": self.conversation_scope,
            "scope_revision": self.scope_revision,
            "attested": self.attested,
        })


@dataclass(frozen=True, slots=True)
class VisibilityDecisionV1:
    allowed: bool
    reason: str
    visibility_digest: str


def visibility_scope_decision(
    *,
    viewer_scope: str,
    shareability: str,
    subject_person_id: str,
    viewer: ViewerContextV1,
) -> tuple[bool, str]:
    """Pure exact-scope decision shared by facts and conversational arcs."""

    if not viewer.attested or not viewer.viewer_person_id:
        return False, "viewer_unattested"
    if viewer.viewer_person_id == viewer.owner_person_id:
        return True, "owner_scope"
    if shareability == "owner_private":
        return False, "owner_private"
    if shareability == "subject_private":
        return (
            (True, "subject_scope")
            if viewer.viewer_person_id == subject_person_id
            else (False, "subject_scope_mismatch")
        )
    if shareability == "shared":
        if viewer_scope.startswith("person:"):
            target = viewer_scope.split(":", 1)[1]
            return (
                (True, "exact_person_scope")
                if viewer.viewer_person_id == target
                else (False, "person_scope_mismatch")
            )
        if viewer_scope == "audience:shared":
            return (
                (True, "shared_audience")
                if "shared" in viewer.audiences
                else (False, "shared_audience_not_granted")
            )
        if viewer_scope.startswith("conversation:"):
            target = viewer_scope.split(":", 1)[1]
            return (
                (True, "conversation_scope")
                if viewer.conversation_scope == target
                else (False, "conversation_scope_mismatch")
            )
        return False, "unknown_shared_scope"
    if shareability == "public":
        if viewer_scope == "public":
            return True, "public_scope"
        return (
            (True, "global_audience")
            if "global" in viewer.audiences
            else (False, "global_audience_not_granted")
        )
    return False, "unknown_shareability"


@dataclass(frozen=True, slots=True)
class FactVisibilityV1:
    fact_ref: str
    content_digest: str
    source_ref: str
    subject_person_id: str
    viewer_scope: str
    shareability: str
    confidence: float
    observed_at: str
    fresh_until: str
    evidence_refs: tuple[str, ...]
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported fact visibility version")
        fact_ref = _safe_ref(self.fact_ref, field="fact_ref")
        source_ref = _safe_ref(self.source_ref, field="source_ref")
        digest = str(self.content_digest or "").strip().lower()
        if not _DIGEST_RE.fullmatch(digest):
            raise ValueError("content digest must be a SHA-256 hex digest")
        scope, share, subject = validate_visibility_scope(
            viewer_scope=self.viewer_scope,
            shareability=self.shareability,
            subject_person_id=self.subject_person_id,
        )
        try:
            confidence = float(self.confidence)
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence must be finite and between zero and one") from exc
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be finite and between zero and one")
        observed = _as_utc(self.observed_at, field="observed_at")
        fresh_until = _as_utc(self.fresh_until, field="fresh_until")
        if fresh_until <= observed:
            raise ValueError("fresh_until must be later than observed_at")
        evidence = _refs(
            self.evidence_refs,
            field="evidence_ref",
            maximum=MAX_EVIDENCE_REFS,
        )
        if not evidence:
            raise ValueError("at least one evidence reference is required")
        object.__setattr__(self, "fact_ref", fact_ref)
        object.__setattr__(self, "source_ref", source_ref)
        object.__setattr__(self, "content_digest", digest)
        object.__setattr__(self, "subject_person_id", subject)
        object.__setattr__(self, "viewer_scope", scope)
        object.__setattr__(self, "shareability", share)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "observed_at", observed.isoformat())
        object.__setattr__(self, "fresh_until", fresh_until.isoformat())
        object.__setattr__(self, "evidence_refs", evidence)

    @property
    def audit_digest(self) -> str:
        return _digest(self.public())

    def public(self) -> dict[str, Any]:
        """Reference-only projection; fact text never lives in this record."""

        return {
            "schema_version": self.schema_version,
            "fact_ref": self.fact_ref,
            "content_digest": self.content_digest,
            "source_ref": self.source_ref,
            "subject_person_id": self.subject_person_id,
            "viewer_scope": self.viewer_scope,
            "shareability": self.shareability,
            "confidence": self.confidence,
            "observed_at": self.observed_at,
            "fresh_until": self.fresh_until,
            "evidence_refs": list(self.evidence_refs),
        }

    def decision(
        self,
        viewer: ViewerContextV1,
        *,
        now: datetime | str,
        min_confidence: float = 0.0,
    ) -> VisibilityDecisionV1:
        if not viewer.attested or not viewer.viewer_person_id:
            return VisibilityDecisionV1(
                False, "viewer_unattested", self.audit_digest)
        decision_time = _as_utc(now, field="now")
        fact_observed = _as_utc(self.observed_at, field="observed_at")
        if decision_time < fact_observed:
            return VisibilityDecisionV1(
                False, "fact_not_yet_observed", self.audit_digest)
        if decision_time >= _as_utc(self.fresh_until, field="fresh_until"):
            return VisibilityDecisionV1(False, "fact_stale", self.audit_digest)
        if self.confidence < float(min_confidence):
            return VisibilityDecisionV1(
                False, "confidence_below_floor", self.audit_digest)
        allowed, reason = visibility_scope_decision(
            viewer_scope=self.viewer_scope,
            shareability=self.shareability,
            subject_person_id=self.subject_person_id,
            viewer=viewer,
        )
        return VisibilityDecisionV1(allowed, reason, self.audit_digest)


@dataclass(frozen=True, slots=True)
class FactCandidateV1:
    content: str
    visibility: FactVisibilityV1

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("fact content is required")
        if len(self.content) > MAX_FACT_CONTENT_CHARS:
            raise ValueError("fact content exceeds bounded length")
        if content_digest(self.content) != self.visibility.content_digest:
            raise ValueError("fact content digest does not match visibility")


@dataclass(frozen=True, slots=True)
class ProjectedFactV1:
    fact_ref: str
    content: str
    source_ref: str
    subject_person_id: str
    shareability: str
    confidence: float
    fresh_until: str
    evidence_refs: tuple[str, ...]
    visibility_digest: str

    def public(self) -> dict[str, Any]:
        return {
            "fact_ref": self.fact_ref,
            "content": self.content,
            "source_ref": self.source_ref,
            "subject_person_id": self.subject_person_id,
            "shareability": self.shareability,
            "confidence": self.confidence,
            "fresh_until": self.fresh_until,
            "evidence_refs": list(self.evidence_refs),
            "visibility_digest": self.visibility_digest,
        }


@dataclass(frozen=True, slots=True)
class FactDenialV1:
    fact_ref: str
    reason: str


@dataclass(frozen=True, slots=True)
class FactProjectionBatchV1:
    facts: tuple[ProjectedFactV1, ...]
    denied: tuple[FactDenialV1, ...]
    truncated: bool
    viewer_digest: str
    audit_digest: str

    def public(self) -> dict[str, Any]:
        """Bounded projection; denied fact text and topology are omitted."""

        return {
            "facts": [fact.public() for fact in self.facts],
            "denied_counts": dict(sorted(Counter(
                denial.reason for denial in self.denied).items())),
            "truncated": self.truncated,
            "viewer_digest": self.viewer_digest,
            "audit_digest": self.audit_digest,
        }


def project_facts(
    candidates: Sequence[FactCandidateV1],
    viewer: ViewerContextV1,
    *,
    now: datetime | str,
    min_confidence: float = 0.0,
    max_facts: int = 24,
    max_total_chars: int = 12_000,
) -> FactProjectionBatchV1:
    """Filter, deduplicate, and bound facts before any downstream consumer.

    Input ordering cannot affect the result.  A duplicate opaque fact ref with
    conflicting content is dropped entirely.  An empty scoped result remains
    empty; there is no global retry or relevance-based widening.
    """

    if len(candidates) > MAX_CANDIDATES:
        raise ValueError("fact candidate limit exceeded")
    if not 1 <= int(max_facts) <= MAX_PROJECTED_FACTS:
        raise ValueError("max_facts is outside the bounded projection range")
    if not 1 <= int(max_total_chars) <= MAX_PROJECTED_CHARS:
        raise ValueError(
            "max_total_chars is outside the bounded projection range")
    floor = float(min_confidence)
    if not math.isfinite(floor) or not 0.0 <= floor <= 1.0:
        raise ValueError("min_confidence must be between zero and one")

    grouped: dict[str, list[FactCandidateV1]] = defaultdict(list)
    for candidate in candidates:
        if not isinstance(candidate, FactCandidateV1):
            raise ValueError("fact candidates must be FactCandidateV1 records")
        grouped[candidate.visibility.fact_ref].append(candidate)

    def group_key(item: tuple[str, list[FactCandidateV1]]) -> tuple[Any, ...]:
        fact_ref, rows = item
        return (-max(row.visibility.confidence for row in rows), fact_ref)

    facts: list[ProjectedFactV1] = []
    denied: list[FactDenialV1] = []
    total_chars = 0
    truncated = False
    for fact_ref, rows in sorted(grouped.items(), key=group_key):
        digests = {row.visibility.content_digest for row in rows}
        if len(digests) != 1:
            denied.append(FactDenialV1(
                fact_ref=fact_ref, reason="fact_ref_content_conflict"))
            continue
        allowed: list[tuple[FactCandidateV1, VisibilityDecisionV1]] = []
        decisions: list[VisibilityDecisionV1] = []
        for row in sorted(
            rows,
            key=lambda value: (
                -value.visibility.confidence,
                value.visibility.audit_digest,
            ),
        ):
            decision = row.visibility.decision(
                viewer, now=now, min_confidence=floor)
            decisions.append(decision)
            if decision.allowed:
                allowed.append((row, decision))
        if not allowed:
            reason = decisions[0].reason if decisions else "visibility_unknown"
            denied.append(FactDenialV1(fact_ref=fact_ref, reason=reason))
            continue
        row, decision = allowed[0]
        if len(facts) >= int(max_facts) \
                or total_chars + len(row.content) > int(max_total_chars):
            denied.append(FactDenialV1(
                fact_ref=fact_ref, reason="projection_budget_exhausted"))
            truncated = True
            continue
        visibility = row.visibility
        facts.append(ProjectedFactV1(
            fact_ref=fact_ref,
            content=row.content,
            source_ref=visibility.source_ref,
            subject_person_id=visibility.subject_person_id,
            shareability=visibility.shareability,
            confidence=visibility.confidence,
            fresh_until=visibility.fresh_until,
            evidence_refs=visibility.evidence_refs,
            visibility_digest=decision.visibility_digest,
        ))
        total_chars += len(row.content)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "viewer_digest": viewer.audit_digest,
        "facts": [
            {
                "fact_ref": fact.fact_ref,
                "content_digest": content_digest(fact.content),
                "visibility_digest": fact.visibility_digest,
            }
            for fact in facts
        ],
        "denied": [
            {"fact_ref": item.fact_ref, "reason": item.reason}
            for item in denied
        ],
        "truncated": truncated,
        "min_confidence": floor,
        "max_facts": int(max_facts),
        "max_total_chars": int(max_total_chars),
        "now": _iso(now, field="now"),
    }
    return FactProjectionBatchV1(
        facts=tuple(facts),
        denied=tuple(denied),
        truncated=truncated,
        viewer_digest=viewer.audit_digest,
        audit_digest=_digest(payload),
    )


__all__ = [
    "FactCandidateV1",
    "FactDenialV1",
    "FactProjectionBatchV1",
    "FactVisibilityV1",
    "ProjectedFactV1",
    "SHAREABILITIES",
    "ViewerContextV1",
    "VisibilityDecisionV1",
    "content_digest",
    "project_facts",
    "validate_visibility_scope",
    "visibility_scope_decision",
]
