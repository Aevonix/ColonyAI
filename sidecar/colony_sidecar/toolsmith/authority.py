"""One-shot, owner-scoped authority for publishing a Toolsmith artifact."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Mapping

from colony_sidecar.toolsmith.integrity import digest_json


AUTHORITY_VERSION = "toolsmith.graduation-authority.v1"
MAX_AUTHORITY_TTL = timedelta(minutes=15)
MAX_CLOCK_SKEW = timedelta(minutes=2)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class GraduationAuthorityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_time(value: str, field: str) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise GraduationAuthorityError(
            "invalid_authority_time", f"{field} must be an RFC3339 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise GraduationAuthorityError(
            "invalid_authority_time", f"{field} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class GraduationAuthorityV1:
    authority_id: str
    decision_id: str
    tool_id: str
    candidate_digest: str
    artifact_digest: str
    principal_id: str
    owner_person_id: str
    issued_at: str
    expires_at: str
    max_uses: int = 1
    version: str = AUTHORITY_VERSION

    @classmethod
    def from_request(
        cls,
        body: Mapping[str, Any],
        *,
        tool_id: str,
        principal_id: str,
        owner_person_id: str,
        now: datetime | None = None,
    ) -> "GraduationAuthorityV1":
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        authority_id = str(body.get("authority_id") or "").strip()
        decision_id = str(body.get("decision_id") or "").strip()
        candidate = str(body.get("expected_candidate_digest") or "").strip()
        artifact = str(body.get("expected_artifact_digest") or "").strip()
        issued = _parse_time(str(body.get("issued_at") or ""), "issued_at")
        expires = _parse_time(str(body.get("expires_at") or ""), "expires_at")
        max_uses = body.get("max_uses", 1)

        if not _ID_RE.fullmatch(authority_id):
            raise GraduationAuthorityError(
                "invalid_authority_id", "authority_id is malformed"
            )
        if not _ID_RE.fullmatch(decision_id):
            raise GraduationAuthorityError(
                "invalid_decision_id", "decision_id is malformed"
            )
        if not _DIGEST_RE.fullmatch(candidate) or not _DIGEST_RE.fullmatch(artifact):
            raise GraduationAuthorityError(
                "invalid_artifact_digest", "expected digests must be sha256 hex"
            )
        if max_uses != 1:
            raise GraduationAuthorityError(
                "invalid_use_bound", "graduation authority must have max_uses=1"
            )
        if issued > current + MAX_CLOCK_SKEW:
            raise GraduationAuthorityError(
                "authority_not_yet_valid", "issued_at is too far in the future"
            )
        if expires <= current:
            raise GraduationAuthorityError(
                "authority_expired", "graduation authority has expired"
            )
        if expires <= issued or expires - issued > MAX_AUTHORITY_TTL:
            raise GraduationAuthorityError(
                "invalid_authority_ttl", "authority lifetime must be 15 minutes or less"
            )
        if not principal_id or not owner_person_id:
            raise GraduationAuthorityError(
                "owner_authority_required", "server-derived owner authority is required"
            )
        return cls(
            authority_id=authority_id,
            decision_id=decision_id,
            tool_id=tool_id,
            candidate_digest=candidate,
            artifact_digest=artifact,
            principal_id=principal_id,
            owner_person_id=owner_person_id,
            issued_at=issued.isoformat(),
            expires_at=expires.isoformat(),
            max_uses=1,
        )

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def authority_digest(self) -> str:
        return digest_json(self.payload())

    def assert_current(self, *, now: datetime | None = None) -> None:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if self.version != AUTHORITY_VERSION:
            raise GraduationAuthorityError(
                "invalid_authority_version", "unsupported graduation authority version"
            )
        if (
            not _ID_RE.fullmatch(self.authority_id)
            or not _ID_RE.fullmatch(self.decision_id)
        ):
            raise GraduationAuthorityError(
                "invalid_authority_id", "authority identifiers are malformed"
            )
        if (
            not _DIGEST_RE.fullmatch(self.candidate_digest)
            or not _DIGEST_RE.fullmatch(self.artifact_digest)
        ):
            raise GraduationAuthorityError(
                "invalid_artifact_digest", "authority digests are malformed"
            )
        if self.max_uses != 1 or not self.principal_id or not self.owner_person_id:
            raise GraduationAuthorityError(
                "invalid_authority_bound", "authority must be owner-bound and one-shot"
            )
        issued = _parse_time(self.issued_at, "issued_at")
        expires = _parse_time(self.expires_at, "expires_at")
        if issued > current + MAX_CLOCK_SKEW:
            raise GraduationAuthorityError(
                "authority_not_yet_valid", "issued_at is too far in the future"
            )
        if expires <= issued or expires - issued > MAX_AUTHORITY_TTL:
            raise GraduationAuthorityError(
                "invalid_authority_ttl", "authority lifetime must be 15 minutes or less"
            )
        if expires <= current:
            raise GraduationAuthorityError(
                "authority_expired", "graduation authority has expired"
            )
