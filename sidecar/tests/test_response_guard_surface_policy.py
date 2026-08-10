"""ResponseGuardSurfacePolicyV1 is exact, monotonic, and deployment-neutral."""

import hashlib
from typing import get_args

import pytest

from colony_sidecar.gate.response_guard import unavailable_guard_result
from colony_sidecar.gate.surface_policy import (
    ALL_SURFACES,
    EXCLUDED_SPEECH_SURFACES,
    GUARDED_ARTIFACT_SURFACES,
    GUARDED_TEXT_SURFACES,
    POLICY_DIGEST,
    POLICY_ID,
    ResponseGuardSurfaceName,
    ResponseGuardSurfacePolicyV1,
)


def test_surface_sets_and_digest_are_regression_locked():
    assert GUARDED_TEXT_SURFACES == {
        "api_text",
        "cold_text",
        "cron_text",
        "meeting_text",
        "proactive_text",
        "text_chat",
        "text_message",
    }
    assert GUARDED_ARTIFACT_SURFACES == {"artifact"}
    assert EXCLUDED_SPEECH_SURFACES == {
        "meeting_speech",
        "realtime_voice",
    }
    assert len(ALL_SURFACES) == 10
    assert set(get_args(ResponseGuardSurfaceName)) == ALL_SURFACES
    assert POLICY_ID == "response-guard-surface-policy-v1"
    assert POLICY_DIGEST == (
        "712a2b620aa135b372e738ca56e83549b830132e3574b256890edd5cab281c8f"
    )


@pytest.mark.parametrize("surface", sorted(GUARDED_TEXT_SURFACES))
def test_text_surfaces_are_guarded(surface):
    decision = ResponseGuardSurfacePolicyV1().resolve(
        surface,
        configured_mode="shadow",
    )
    assert decision.family == "text"
    assert decision.disposition == "guarded"
    assert decision.effective_mode == "shadow"
    assert decision.failure_behavior == "allow"


def test_artifact_is_guarded_and_speech_is_excluded():
    policy = ResponseGuardSurfacePolicyV1()
    artifact = policy.resolve("artifact", configured_mode="enforce")
    assert artifact.family == "artifact"
    assert artifact.disposition == "guarded"
    assert artifact.failure_behavior == "block"

    for surface in EXCLUDED_SPEECH_SURFACES:
        speech = policy.resolve(surface, configured_mode="enforce")
        assert speech.family == "speech"
        assert speech.disposition == "excluded"
        assert speech.effective_mode == "excluded"
        assert speech.failure_behavior == "allow"


def test_request_can_strengthen_but_never_weaken_mode():
    policy = ResponseGuardSurfacePolicyV1()
    strengthened = policy.resolve(
        "text_chat",
        configured_mode="shadow",
        requested_mode="enforce",
    )
    assert strengthened.effective_mode == "enforce"
    not_weakened = policy.resolve(
        "text_chat",
        configured_mode="enforce",
        requested_mode="shadow",
    )
    assert not_weakened.effective_mode == "enforce"


def test_public_policy_projection_cannot_mutate_canonical_policy():
    policy = ResponseGuardSurfacePolicyV1()
    projection = policy.public()
    projection["guarded_text_surfaces"].append("caller_injected")
    assert "caller_injected" not in policy.public()["guarded_text_surfaces"]
    assert policy.policy_digest == POLICY_DIGEST


@pytest.mark.parametrize("surface", [None, "", "voice", "TEXT_CHAT", " text_chat"])
def test_unknown_or_inexact_surface_is_rejected(surface):
    with pytest.raises(ValueError, match="unsupported ResponseGuard surface"):
        ResponseGuardSurfacePolicyV1().resolve(
            surface,
            configured_mode="shadow",
        )


def test_guard_outage_behavior_is_surface_and_mode_specific():
    shadow = unavailable_guard_result(
        surface="text_message",
        configured_mode="shadow",
        response_text="candidate",
    )
    assert shadow.decision == "allow"
    assert shadow.guard_status == "degraded"
    assert shadow.findings[0].severity == "warn"
    assert shadow.candidate_digest == hashlib.sha256(
        b"candidate"
    ).hexdigest()

    enforce = unavailable_guard_result(
        surface="artifact",
        configured_mode="enforce",
    )
    assert enforce.decision == "block"
    assert enforce.guard_status == "degraded"
    assert enforce.findings[0].severity == "block"

    speech = unavailable_guard_result(
        surface="realtime_voice",
        configured_mode="enforce",
    )
    assert speech.decision == "allow"
    assert speech.mode == "excluded"
    assert speech.guard_status == "bypassed"
    assert not speech.findings
