"""Explicit, deployment-neutral ResponseGuard surface policy.

The policy separates asynchronous text/artifact delivery from real-time speech.
It deliberately does not infer a surface from a gateway name: a text adapter
cannot bypass the guard by calling itself ``voice``, and an embedding deployment
does not need to maintain an ever-growing gateway exclusion list.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Literal, Optional, cast


ResponseGuardSurfaceName = Literal[
    "api_text",
    "artifact",
    "cold_text",
    "cron_text",
    "meeting_speech",
    "meeting_text",
    "proactive_text",
    "realtime_voice",
    "text_chat",
    "text_message",
]


POLICY_ID = "response-guard-surface-policy-v1"
GUARDED_TEXT_SURFACES = frozenset({
    "api_text",
    "cold_text",
    "cron_text",
    "meeting_text",
    "proactive_text",
    "text_chat",
    "text_message",
})
GUARDED_ARTIFACT_SURFACES = frozenset({"artifact"})
EXCLUDED_SPEECH_SURFACES = frozenset({
    "meeting_speech",
    "realtime_voice",
})
ALL_SURFACES = (
    GUARDED_TEXT_SURFACES
    | GUARDED_ARTIFACT_SURFACES
    | EXCLUDED_SPEECH_SURFACES
)


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


_POLICY_PAYLOAD = {
    "schema": "ResponseGuardSurfacePolicyV1",
    "version": 1,
    "policy_id": POLICY_ID,
    "guarded_text_surfaces": sorted(GUARDED_TEXT_SURFACES),
    "guarded_artifact_surfaces": sorted(GUARDED_ARTIFACT_SURFACES),
    "excluded_speech_surfaces": sorted(EXCLUDED_SPEECH_SURFACES),
    "mode_rule": "caller_may_strengthen_never_weaken",
}
POLICY_DIGEST = hashlib.sha256(
    _canonical(_POLICY_PAYLOAD).encode("utf-8")
).hexdigest()


@dataclass(frozen=True)
class SurfacePolicyDecisionV1:
    surface: str
    family: str
    disposition: str
    configured_mode: str
    requested_mode: str
    effective_mode: str
    failure_behavior: str
    policy_id: str = POLICY_ID
    policy_digest: str = POLICY_DIGEST
    schema: str = "ResponseGuardSurfaceDecisionV1"
    version: int = 1

    @property
    def guarded(self) -> bool:
        return self.disposition == "guarded"

    def public(self) -> dict:
        return {
            "schema": self.schema,
            "version": self.version,
            "policy_id": self.policy_id,
            "policy_digest": self.policy_digest,
            "surface": self.surface,
            "family": self.family,
            "disposition": self.disposition,
            "configured_mode": self.configured_mode,
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "failure_behavior": self.failure_behavior,
        }


class ResponseGuardSurfacePolicyV1:
    """Resolve one exact outbound surface without gateway-name inference."""

    policy_id = POLICY_ID
    policy_digest = POLICY_DIGEST

    @staticmethod
    def validate_surface(surface: object) -> ResponseGuardSurfaceName:
        if not isinstance(surface, str) or surface not in ALL_SURFACES:
            raise ValueError("unsupported ResponseGuard surface")
        return cast(ResponseGuardSurfaceName, surface)

    @staticmethod
    def _mode(value: object, *, field: str) -> str:
        text = str(getattr(value, "value", value) or "").strip().lower()
        if text not in {"shadow", "enforce"}:
            raise ValueError(f"{field} must be shadow or enforce")
        return text

    def resolve(
        self,
        surface: object,
        *,
        configured_mode: object,
        requested_mode: Optional[object] = None,
    ) -> SurfacePolicyDecisionV1:
        exact = self.validate_surface(surface)
        configured = self._mode(configured_mode, field="configured_mode")
        requested = (
            self._mode(requested_mode, field="requested_mode")
            if requested_mode is not None
            else configured
        )
        if exact in EXCLUDED_SPEECH_SURFACES:
            return SurfacePolicyDecisionV1(
                surface=exact,
                family="speech",
                disposition="excluded",
                configured_mode=configured,
                requested_mode=requested,
                effective_mode="excluded",
                failure_behavior="allow",
            )
        family = "artifact" if exact in GUARDED_ARTIFACT_SURFACES else "text"
        # A request can explicitly graduate a shadow deployment to enforce for
        # this call, but can never downgrade a configured enforce deployment.
        effective = (
            "enforce"
            if "enforce" in {configured, requested}
            else "shadow"
        )
        return SurfacePolicyDecisionV1(
            surface=exact,
            family=family,
            disposition="guarded",
            configured_mode=configured,
            requested_mode=requested,
            effective_mode=effective,
            failure_behavior="block" if effective == "enforce" else "allow",
        )

    def public(self) -> dict:
        # Return a detached projection: callers cannot mutate the canonical
        # list objects while the regression-locked digest stays unchanged.
        projection = json.loads(_canonical(_POLICY_PAYLOAD))
        projection["policy_digest"] = self.policy_digest
        return projection


__all__ = [
    "ALL_SURFACES",
    "EXCLUDED_SPEECH_SURFACES",
    "GUARDED_ARTIFACT_SURFACES",
    "GUARDED_TEXT_SURFACES",
    "POLICY_DIGEST",
    "POLICY_ID",
    "ResponseGuardSurfaceName",
    "ResponseGuardSurfacePolicyV1",
    "SurfacePolicyDecisionV1",
]
