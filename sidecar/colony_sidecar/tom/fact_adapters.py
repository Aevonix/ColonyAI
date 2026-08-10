"""Pure P8 fact adapters with an explicit server-authority boundary.

Models and request bodies may propose only fact content and confidence.  Fact
identity, source, subject, audience, shareability, evidence, and freshness are
accepted solely through the typed ``ServerFactAuthorityV1`` input.  Unknown
untrusted fields are rejected rather than silently ignored so a future caller
cannot accidentally make a body/model field authoritative during refactoring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Mapping

from colony_sidecar.tom.visibility import (
    FactCandidateV1,
    FactVisibilityV1,
    content_digest,
)


class FactAuthorityBoundaryError(ValueError):
    """Untrusted input attempted to cross the fact-authority boundary."""


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
class FactPayloadV1:
    """The complete set of fields an untrusted fact producer may propose."""

    content: str
    confidence: float

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("fact content is required")
        if isinstance(self.confidence, bool):
            raise ValueError("fact confidence must be finite and between zero and one")
        try:
            confidence = float(self.confidence)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "fact confidence must be finite and between zero and one") from exc
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError(
                "fact confidence must be finite and between zero and one")
        object.__setattr__(self, "confidence", confidence)

    @classmethod
    def from_untrusted(
        cls,
        fields: Mapping[str, Any],
        *,
        origin: str,
    ) -> "FactPayloadV1":
        """Whitelist content/confidence and reject every other input field."""

        label = str(origin or "untrusted").strip().lower() or "untrusted"
        if not isinstance(fields, Mapping):
            raise FactAuthorityBoundaryError(
                f"{label} fact payload must be a structured mapping")
        allowed = {"content", "confidence"}
        rejected = sorted(str(key) for key in fields if key not in allowed)
        if rejected:
            raise FactAuthorityBoundaryError(
                f"{label} field {rejected[0]} cannot supply fact authority")
        missing = sorted(allowed.difference(fields))
        if missing:
            raise ValueError(f"{label} fact payload is missing {missing[0]}")
        return cls(
            content=fields["content"],
            confidence=fields["confidence"],
        )


@dataclass(frozen=True, slots=True)
class ServerFactAuthorityV1:
    """Structured authority derived from authenticated server-side state."""

    fact_ref: str
    source_ref: str
    subject_person_id: str
    viewer_scope: str
    shareability: str
    observed_at: datetime | str
    fresh_until: datetime | str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "observed_at", _iso(self.observed_at, field="observed_at"))
        object.__setattr__(
            self, "fresh_until", _iso(self.fresh_until, field="fresh_until"))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))


def build_fact_candidate(
    *,
    authority: ServerFactAuthorityV1,
    payload: FactPayloadV1,
) -> FactCandidateV1:
    """Combine typed server authority with a non-authoritative fact payload."""

    if not isinstance(authority, ServerFactAuthorityV1):
        raise FactAuthorityBoundaryError(
            "fact visibility authority must be a server-derived structured input")
    if not isinstance(payload, FactPayloadV1):
        raise FactAuthorityBoundaryError(
            "fact content must be a bounded FactPayloadV1 input")
    visibility = FactVisibilityV1(
        fact_ref=authority.fact_ref,
        content_digest=content_digest(payload.content),
        source_ref=authority.source_ref,
        subject_person_id=authority.subject_person_id,
        viewer_scope=authority.viewer_scope,
        shareability=authority.shareability,
        confidence=payload.confidence,
        observed_at=authority.observed_at,
        fresh_until=authority.fresh_until,
        evidence_refs=authority.evidence_refs,
    )
    return FactCandidateV1(content=payload.content, visibility=visibility)


__all__ = [
    "FactAuthorityBoundaryError",
    "FactPayloadV1",
    "ServerFactAuthorityV1",
    "build_fact_candidate",
]
