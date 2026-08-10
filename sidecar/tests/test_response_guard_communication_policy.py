"""CommunicationPolicyContextV1 exact binding and no-authority contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from colony_sidecar.api.routers import host as host_mod
from colony_sidecar.api.schemas.host import ResponseGuardCheckRequest
from colony_sidecar.gate.communication_policy import (
    COMMUNICATION_DISCLOSURE_CLASSES,
    CommunicationPolicyContextV1,
    MAX_COMMUNICATION_DISCLOSURE_STATEMENT_CHARS,
    MAX_COMMUNICATION_PURPOSE_CHARS,
)
from colony_sidecar.gate.context_provenance import (
    ContextProvenanceStore,
    ProvenanceCrossContextGuard,
)
from colony_sidecar.gate.guard_audit import GuardAuditStore
from colony_sidecar.gate.layers.l6_review import SecondaryReviewer
from colony_sidecar.gate.models import GatePayload
from colony_sidecar.gate.response_guard import GuardMode, ResponseGuard
from colony_sidecar.intelligence.relationships.trust_tiers import TrustTier


def _policy_dict(**changes):
    value = {
        "schema": "CommunicationPolicyContextV1",
        "version": 1,
        "target_contact_id": "contact-maya-0001",
        "route_id": "route-maya-0001",
        "grant_id": "grant-maya-0001",
        "grant_digest": "a" * 64,
        "policy_id": "policy-maya-0001",
        "policy_digest": "b" * 64,
        "purpose": "Coordinate the exact airport pickup for this trip.",
        "disclosure_class": "contact_scoped",
        "disclosure_statement": (
            "Use only this conversation and facts explicitly shared for the pickup."
        ),
        "grants_execution_authority": False,
        "grants_control_authority": False,
        "selects_owner_private_context": False,
    }
    value.update(changes)
    return value


def _policy(**changes) -> CommunicationPolicyContextV1:
    return CommunicationPolicyContextV1.model_validate(_policy_dict(**changes))


def test_exact_schema_requires_every_field_and_rejects_unknown_fields():
    valid = _policy_dict()
    assert _policy().canonical_dict() == valid
    json_schema = CommunicationPolicyContextV1.model_json_schema(by_alias=True)
    assert json_schema["additionalProperties"] is False
    assert set(json_schema["properties"]) == set(valid)
    assert set(json_schema["required"]) == set(valid)
    for field in tuple(valid):
        partial = dict(valid)
        partial.pop(field)
        with pytest.raises(ValidationError):
            CommunicationPolicyContextV1.model_validate(partial)
    with pytest.raises(ValidationError):
        CommunicationPolicyContextV1.model_validate({**valid, "authority": "owner"})
    with pytest.raises(ValidationError):
        _policy(schema="CommunicationPolicyContextV2")
    with pytest.raises(ValidationError):
        _policy(version=2)


@pytest.mark.parametrize("field", [
    "grants_execution_authority",
    "grants_control_authority",
    "selects_owner_private_context",
])
@pytest.mark.parametrize("value", [True, 0, "false", None])
def test_capability_statements_are_required_literal_false(field, value):
    with pytest.raises(ValidationError):
        _policy(**{field: value})


def test_digest_identifier_target_and_text_boundaries():
    for field in ("grant_digest", "policy_digest"):
        for value in ("a" * 63, "A" * 64, "g" * 64):
            with pytest.raises(ValidationError):
                _policy(**{field: value})
    for field in ("route_id", "grant_id", "policy_id"):
        for value in ("short", " invalid-id", "invalid id"):
            with pytest.raises(ValidationError):
                _policy(**{field: value})
    for target in ("all", "everyone", "contact-*", " contact-maya-0001"):
        with pytest.raises(ValidationError):
            _policy(target_contact_id=target)

    _policy(purpose="p" * MAX_COMMUNICATION_PURPOSE_CHARS)
    _policy(
        disclosure_statement=(
            "d" * MAX_COMMUNICATION_DISCLOSURE_STATEMENT_CHARS
        )
    )
    for changes in (
        {"purpose": ""},
        {"purpose": " purpose"},
        {"purpose": "purpose\ncontinued"},
        {"purpose": "p" * (MAX_COMMUNICATION_PURPOSE_CHARS + 1)},
        {"disclosure_statement": ""},
        {"disclosure_statement": "statement\tcontinued"},
        {
            "disclosure_statement": (
                "d" * (MAX_COMMUNICATION_DISCLOSURE_STATEMENT_CHARS + 1)
            )
        },
    ):
        with pytest.raises(ValidationError):
            _policy(**changes)


def test_disclosure_class_is_closed():
    for disclosure_class in sorted(COMMUNICATION_DISCLOSURE_CLASSES):
        assert _policy(disclosure_class=disclosure_class).disclosure_class == disclosure_class
    for disclosure_class in ("owner_private", "PUBLIC", "contact", ""):
        with pytest.raises(ValidationError):
            _policy(disclosure_class=disclosure_class)


def test_context_digest_is_deterministic_and_detects_field_tamper():
    first = _policy()
    second = CommunicationPolicyContextV1.model_validate(
        json.loads(json.dumps(first.canonical_dict(), sort_keys=False))
    )
    assert first.context_digest == second.context_digest
    assert first.context_digest == (
        "1d358379f5b61a00b964e07c02a2d7ce"
        "7fe1a58869435b2eff8e58a67540ec94"
    )

    # Colony cannot reconstruct the host's private full policy record.  It
    # therefore preserves that external digest and separately binds every
    # field it saw, making a substituted purpose detectable by the caller.
    tampered = _policy(purpose="Coordinate a different approved task.")
    assert tampered.policy_digest == first.policy_digest
    assert tampered.context_digest != first.context_digest


def test_request_target_must_match_policy_target():
    with pytest.raises(ValidationError):
        ResponseGuardCheckRequest(
            surface="text_chat",
            response_text="hello",
            target_contact_id="contact-other-0001",
            communication_policy=_policy(),
        )
    with pytest.raises(ValidationError):
        ResponseGuardCheckRequest(
            surface="text_chat",
            response_text="hello",
            communication_policy=_policy(),
        )


@pytest.mark.asyncio
async def test_legacy_omission_preserves_exact_response_shape(monkeypatch):
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE)
    monkeypatch.setattr(host_mod, "_response_guard", guard)
    result = await host_mod.response_guard_check(ResponseGuardCheckRequest(
        surface="text_chat",
        response_text="see you at six",
        target_contact_id="contact-maya-0001",
    ))
    assert set(result) == {
        "decision", "mode", "surface", "surface_family", "applicability",
        "guard_status", "policy_id", "policy_digest", "candidate_digest",
        "findings",
    }


@pytest.mark.asyncio
async def test_result_binds_external_policy_and_full_context_digests(monkeypatch):
    policy = _policy()
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE)
    monkeypatch.setattr(host_mod, "_response_guard", guard)
    result = await host_mod.response_guard_check(ResponseGuardCheckRequest(
        surface="text_chat",
        response_text="see you at six",
        target_contact_id=policy.target_contact_id,
        communication_policy=policy,
    ))
    assert result["decision"] == "allow"
    assert result["communication_policy_digest"] == policy.policy_digest
    assert result["communication_context_digest"] == policy.context_digest
    assert set(result) == {
        "decision", "mode", "surface", "surface_family", "applicability",
        "guard_status", "policy_id", "policy_digest", "candidate_digest",
        "findings", "communication_policy_digest", "communication_context_digest",
    }


@pytest.mark.asyncio
async def test_missing_guard_result_still_binds_policy(monkeypatch):
    policy = _policy()
    monkeypatch.setattr(host_mod, "_response_guard", None)
    monkeypatch.setenv("COLONY_GUARD_MODE", "enforce")
    result = await host_mod.response_guard_check(ResponseGuardCheckRequest(
        surface="text_message",
        response_text="hello",
        target_contact_id=policy.target_contact_id,
        communication_policy=policy,
    ))
    assert result["decision"] == "block"
    assert result["communication_policy_digest"] == policy.policy_digest
    assert result["communication_context_digest"] == policy.context_digest


@pytest.mark.asyncio
async def test_policy_is_visible_to_guard_inputs_without_authority_widening(monkeypatch):
    monkeypatch.setenv("COLONY_GUARD_ENFORCE_CHECKS", "all")
    store = ContextProvenanceStore(":memory:")
    store.record("conversation-private", ["Project Falcon"])

    class CapturingCrossContext(ProvenanceCrossContextGuard):
        seen = None

        async def check(self, **kwargs):
            self.seen = kwargs["communication_policy"]
            return await super().check(**kwargs)

    cross = CapturingCrossContext(store)
    guard = ResponseGuard(
        default_mode=GuardMode.ENFORCE,
        cross_context=cross,
    )
    monkeypatch.setattr(host_mod, "_response_guard", guard)
    policy = _policy(
        disclosure_class="owner_explicit",
        disclosure_statement="Only the exact owner-approved statement may be used.",
    )
    result = await host_mod.response_guard_check(ResponseGuardCheckRequest(
        surface="text_chat",
        response_text="Project Falcon is ready",
        target_contact_id=policy.target_contact_id,
        target_gateway="rcs",
        conversation_key="conversation-target",
        mentioned_entities=["Project Falcon"],
        communication_policy=policy,
        # Caller-supplied authority remains ignored by the host endpoint.
        authorized=True,
    ))
    assert cross.seen == policy
    assert result["decision"] == "revise"
    assert any(
        finding["check"] == "cross_context"
        and finding["severity"] == "block"
        for finding in result["findings"]
    )


@pytest.mark.asyncio
async def test_restricted_review_model_sees_exact_policy_fields():
    class CapturingClient:
        prompt = ""

        async def complete(self, prompt):
            self.prompt = prompt
            return '{"verdict":"appropriate"}'

    policy = _policy()
    payload = GatePayload(
        response_text="see you at six",
        target_contact_id=policy.target_contact_id,
        target_gateway="rcs",
        session_id="session-0001",
        trust_tier=TrustTier.REGULAR,
        mentioned_entities=frozenset(),
        turn_id="turn-0001",
        incoming_message_text="",
        communication_policy=policy,
    )
    client = CapturingClient()
    result = await SecondaryReviewer(llm_client=client).review(payload)
    assert result.flagged is False
    assert json.dumps(
        policy.canonical_dict(),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ) in client.prompt
    assert "grants no execution authority" in client.prompt
    assert "owner_principal" not in client.prompt


@pytest.mark.asyncio
async def test_clean_policy_evaluation_has_deterministic_separate_audit_row():
    policy = _policy()
    audit = GuardAuditStore(":memory:")
    guard = ResponseGuard(
        default_mode=GuardMode.ENFORCE,
        audit_store=audit,
    )
    result = await guard.evaluate(
        surface="text_chat",
        response_text="see you at six",
        target_contact_id=policy.target_contact_id,
        target_gateway="RCS",
        conversation_key="conversation-target",
        communication_policy=policy,
    )
    assert result.decision == "allow"
    assert audit.recent() == []
    rows = audit.recent_communication_policy()
    assert len(rows) == 1
    row = rows[0]
    assert row["target_contact_id"] == policy.target_contact_id
    assert row["communication_schema"] == "CommunicationPolicyContextV1"
    assert row["communication_version"] == 1
    assert row["route_id"] == policy.route_id
    assert row["grant_id"] == policy.grant_id
    assert row["grant_digest"] == policy.grant_digest
    assert row["communication_policy_id"] == policy.policy_id
    assert row["communication_policy_digest"] == policy.policy_digest
    assert row["communication_context_digest"] == policy.context_digest
    assert row["gateway"] == "rcs"
    assert row["decision"] == result.decision
    assert row["candidate_digest"] == result.candidate_digest
    assert row["grants_execution_authority"] == 0
    assert row["grants_control_authority"] == 0
    assert row["selects_owner_private_context"] == 0


@pytest.mark.asyncio
async def test_direct_guard_requires_typed_policy_and_matching_target():
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE)
    policy = _policy()
    with pytest.raises(TypeError):
        # The API performs Pydantic validation first; direct callers must not
        # bypass that contract with an untyped mapping.
        await guard.evaluate(
            surface="text_chat",
            response_text="hello",
            target_contact_id=policy.target_contact_id,
            communication_policy=policy.canonical_dict(),
        )
    with pytest.raises(ValueError):
        await guard.evaluate(
            surface="text_chat",
            response_text="hello",
            target_contact_id="contact-other-0001",
            communication_policy=policy,
        )
    bypassed_validation = policy.model_copy(
        update={"grants_execution_authority": True}
    )
    with pytest.raises(ValidationError):
        await guard.evaluate(
            surface="text_chat",
            response_text="hello",
            target_contact_id=policy.target_contact_id,
            communication_policy=bypassed_validation,
        )
