"""Pure P8 adapters keep authority out of model/body fields."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from colony_sidecar.tom.fact_adapters import (
    FactAuthorityBoundaryError,
    FactPayloadV1,
    ServerFactAuthorityV1,
    build_fact_candidate,
)
from colony_sidecar.tom.visibility import content_digest


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def _authority() -> ServerFactAuthorityV1:
    return ServerFactAuthorityV1(
        fact_ref="fact:alice:preference",
        source_ref="turn:owner:42",
        subject_person_id="alice",
        viewer_scope="person:alice",
        shareability="subject_private",
        observed_at=NOW,
        fresh_until=NOW + timedelta(days=30),
        evidence_refs=("turn:owner:42",),
    )


def test_structured_adapter_derives_digest_and_exact_server_authority():
    payload = FactPayloadV1.from_untrusted(
        {"content": "Alice prefers concise updates", "confidence": 0.91},
        origin="model",
    )
    candidate = build_fact_candidate(authority=_authority(), payload=payload)
    assert candidate.visibility.content_digest == content_digest(payload.content)
    assert candidate.visibility.fact_ref == "fact:alice:preference"
    assert candidate.visibility.subject_person_id == "alice"
    assert candidate.visibility.viewer_scope == "person:alice"
    assert candidate.visibility.shareability == "subject_private"
    assert candidate.visibility.confidence == 0.91


@pytest.mark.parametrize(
    "authority_field",
    [
        "fact_ref", "content_digest", "source_ref", "subject_person_id",
        "viewer_scope", "shareability", "observed_at", "fresh_until",
        "evidence_refs", "principal_id", "viewer_person_id",
        "owner_person_id", "audiences", "conversation_scope",
        "scope_revision", "attested",
    ],
)
@pytest.mark.parametrize("origin", ["model", "body"])
def test_model_and_body_authority_fields_are_rejected(origin, authority_field):
    values = {
        "content": "bounded fact",
        "confidence": 0.8,
        authority_field: "malicious-authority",
    }
    with pytest.raises(
        FactAuthorityBoundaryError,
        match=f"{origin}.*{authority_field}",
    ):
        FactPayloadV1.from_untrusted(values, origin=origin)


def test_unknown_or_nested_untrusted_fields_are_rejected_not_ignored():
    with pytest.raises(FactAuthorityBoundaryError, match="model.*authority"):
        FactPayloadV1.from_untrusted({
            "content": "fact",
            "confidence": 0.8,
            "authority": {"viewer_scope": "public"},
        }, origin="model")
    with pytest.raises(FactAuthorityBoundaryError, match="body.*extra"):
        FactPayloadV1.from_untrusted({
            "content": "fact", "confidence": 0.8, "extra": "ignored?",
        }, origin="body")


def test_builder_requires_typed_server_authority_not_a_mapping():
    payload = FactPayloadV1.from_untrusted(
        {"content": "fact", "confidence": 0.8}, origin="model")
    with pytest.raises(FactAuthorityBoundaryError, match="server-derived"):
        build_fact_candidate(
            authority={  # type: ignore[arg-type]
                "fact_ref": "fact:forged",
                "viewer_scope": "public",
                "shareability": "public",
            },
            payload=payload,
        )


def test_adapter_is_pure_and_deterministic():
    payload = FactPayloadV1.from_untrusted(
        {"content": "same fact", "confidence": 0.7}, origin="body")
    first = build_fact_candidate(authority=_authority(), payload=payload)
    second = build_fact_candidate(authority=_authority(), payload=payload)
    assert first == second
    assert first.visibility.audit_digest == second.visibility.audit_digest
