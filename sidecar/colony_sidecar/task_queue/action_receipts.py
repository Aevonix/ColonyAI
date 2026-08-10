"""Strict, digest-bound receipt attestation for generic Action Plane work."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping, Tuple

from colony_sidecar.execution_results import bounded_refs


ACTION_RECEIPT_SCHEMA = "ActionReceiptAttestationV1"
ACTION_RECEIPT_VERSION = 1
ACTION_EFFECT_CLASSES = frozenset({"mutation", "disclosure"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = frozenset({
    "schema", "version", "job_id", "action_digest", "claim_attempt_id",
    "effect_class", "terminal_outcome", "receipt_refs", "observed_at",
    "summary",
})


class ActionReceiptError(ValueError):
    """The verifier supplied a malformed or unbound attestation."""


def _bounded(value: object, field: str, maximum: int, *, required=True) -> str:
    text = str(value or "").strip()
    if (required and not text) or len(text) > maximum:
        qualifier = f"1..{maximum}" if required else f"0..{maximum}"
        raise ActionReceiptError(f"{field} must be {qualifier} characters")
    return text


@dataclass(frozen=True)
class ActionReceiptAttestationV1:
    """One verifier assertion bound to an exact action and claim attempt."""

    job_id: str
    action_digest: str
    claim_attempt_id: str
    effect_class: str
    terminal_outcome: str
    receipt_refs: Tuple[str, ...]
    observed_at: str
    summary: str = ""
    schema: str = ACTION_RECEIPT_SCHEMA
    version: int = ACTION_RECEIPT_VERSION

    @classmethod
    def from_payload(cls, value: object) -> "ActionReceiptAttestationV1":
        if not isinstance(value, Mapping):
            raise ActionReceiptError("action receipt must be an object")
        raw = dict(value)
        unknown = set(raw) - _FIELDS
        missing = _FIELDS - set(raw)
        if unknown or missing:
            raise ActionReceiptError(
                "action receipt fields do not match V1 contract"
            )
        if raw.get("schema") != ACTION_RECEIPT_SCHEMA:
            raise ActionReceiptError("action receipt schema is unsupported")
        if (
            type(raw.get("version")) is not int
            or raw["version"] != ACTION_RECEIPT_VERSION
        ):
            raise ActionReceiptError("action receipt version is unsupported")
        for field in (
            "job_id", "action_digest", "claim_attempt_id", "effect_class",
            "terminal_outcome", "observed_at", "summary",
        ):
            if type(raw.get(field)) is not str:
                raise ActionReceiptError(
                    f"action receipt {field} must be a string"
                )
        digest = str(raw.get("action_digest") or "").strip().lower()
        if not _SHA256_RE.fullmatch(digest) or set(digest) == {"0"}:
            raise ActionReceiptError(
                "action_digest must be a non-null lowercase SHA-256"
            )
        effect_class = str(raw.get("effect_class") or "").strip().lower()
        if effect_class not in ACTION_EFFECT_CLASSES:
            raise ActionReceiptError(
                "effect_class must be mutation or disclosure"
            )
        if raw.get("terminal_outcome") != "succeeded":
            raise ActionReceiptError(
                "success attestation terminal_outcome must be succeeded"
            )
        try:
            observed = datetime.fromisoformat(
                str(raw.get("observed_at") or "").replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ActionReceiptError(
                "observed_at must be an ISO-8601 timestamp"
            ) from exc
        if observed.tzinfo is None:
            raise ActionReceiptError("observed_at must include a timezone")
        refs = bounded_refs(raw.get("receipt_refs"))
        if not refs:
            raise ActionReceiptError(
                "verified action effect requires a receipt reference"
            )
        return cls(
            job_id=_bounded(raw.get("job_id"), "job_id", 128),
            action_digest=digest,
            claim_attempt_id=_bounded(
                raw.get("claim_attempt_id"), "claim_attempt_id", 128,
            ),
            effect_class=effect_class,
            terminal_outcome="succeeded",
            receipt_refs=refs,
            observed_at=observed.astimezone(timezone.utc).isoformat(),
            summary=_bounded(
                raw.get("summary"), "summary", 500, required=False,
            ),
        )

    def payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["receipt_refs"] = list(self.receipt_refs)
        return payload

    def evidence_sha256(
        self,
        *,
        verifier_identity: str,
        verifier_type: str,
    ) -> str:
        """Digest the canonical assertion plus server-derived authority."""

        document = {
            **self.payload(),
            "verifier_identity": _bounded(
                verifier_identity, "verifier_identity", 256,
            ),
            "verifier_type": _bounded(verifier_type, "verifier_type", 128),
        }
        return hashlib.sha256(json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")).hexdigest()
