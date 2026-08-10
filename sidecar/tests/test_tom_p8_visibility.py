"""P8 fact-level visibility: immutable scope before relevance or prose."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from colony_sidecar.tom.visibility import (
    FactCandidateV1,
    FactVisibilityV1,
    ViewerContextV1,
    content_digest,
    project_facts,
)


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def _viewer(
    person: str,
    *,
    audiences=("viewer",),
    conversation_scope="dm:owner",
    attested=True,
):
    return ViewerContextV1(
        principal_id=f"surface:{person}" if person else "",
        viewer_person_id=person,
        owner_person_id="owner",
        audiences=audiences,
        conversation_scope=conversation_scope,
        scope_revision="scope-rev-1" if attested else "",
        attested=attested,
    )


def _visibility(
    fact_ref: str,
    content: str,
    *,
    subject="alice",
    viewer_scope="person:alice",
    shareability="shared",
    confidence=0.9,
    observed_at=NOW,
    fresh_until=None,
    evidence_refs=("turn:source-1",),
):
    return FactVisibilityV1(
        fact_ref=fact_ref,
        content_digest=content_digest(content),
        source_ref="turn:source-1",
        subject_person_id=subject,
        viewer_scope=viewer_scope,
        shareability=shareability,
        confidence=confidence,
        observed_at=observed_at.isoformat(),
        fresh_until=(fresh_until or observed_at + timedelta(days=30)).isoformat(),
        evidence_refs=evidence_refs,
    )


def _candidate(ref, content, **kwargs):
    return FactCandidateV1(
        content=content,
        visibility=_visibility(ref, content, **kwargs),
    )


def test_visibility_is_immutable_and_digest_bound():
    visibility = _visibility("fact:alice-1", "Alice prefers short updates")
    assert visibility.schema_version == 1
    assert len(visibility.audit_digest) == 64
    with pytest.raises(FrozenInstanceError):
        visibility.viewer_scope = "public"  # type: ignore[misc]
    with pytest.raises(ValueError, match="content digest"):
        FactCandidateV1(content="changed", visibility=visibility)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"fact_ref": "contains spaces"}, "fact_ref"),
        ({"source_ref": ""}, "source_ref"),
        ({"subject_person_id": ""}, "subject_person_id"),
        ({"confidence": 1.1}, "confidence"),
        ({"evidence_refs": ()}, "evidence"),
        ({"viewer_scope": "public", "shareability": "owner_private"},
         "owner_private"),
        ({"viewer_scope": "person:bob", "shareability": "subject_private"},
         "subject_private"),
    ],
)
def test_visibility_rejects_unbounded_or_incoherent_fields(overrides, message):
    content = "bounded"
    values = {
        "fact_ref": "fact:bounded",
        "content_digest": content_digest(content),
        "source_ref": "turn:source-1",
        "subject_person_id": "alice",
        "viewer_scope": "person:alice",
        "shareability": "shared",
        "confidence": 0.8,
        "observed_at": NOW.isoformat(),
        "fresh_until": (NOW + timedelta(days=1)).isoformat(),
        "evidence_refs": ("turn:source-1",),
    }
    values.update(overrides)
    with pytest.raises(ValueError, match=message):
        FactVisibilityV1(**values)


def test_owner_subject_exact_shared_audience_and_public_rules():
    owner = _viewer("owner", audiences=("owner", "shared", "global"))
    alice = _viewer("alice")
    bob = _viewer("bob")
    shared = _viewer("carol", audiences=("viewer", "shared"))
    global_viewer = _viewer("dave", audiences=("viewer", "global"))

    owner_private = _candidate(
        "fact:owner", "owner only", subject="owner",
        viewer_scope="owner", shareability="owner_private")
    subject_private = _candidate(
        "fact:alice-private", "alice only", subject="alice",
        viewer_scope="person:alice", shareability="subject_private")
    exact_shared = _candidate(
        "fact:alice-shared", "shared with alice", subject="bob",
        viewer_scope="person:alice", shareability="shared")
    audience_shared = _candidate(
        "fact:team", "shared audience", subject="owner",
        viewer_scope="audience:shared", shareability="shared")
    public = _candidate(
        "fact:public", "public fact", subject="owner",
        viewer_scope="audience:global", shareability="public")

    all_facts = (
        owner_private, subject_private, exact_shared, audience_shared, public)
    assert {f.fact_ref for f in project_facts(
        all_facts, owner, now=NOW).facts} == {
            "fact:owner", "fact:alice-private", "fact:alice-shared",
            "fact:team", "fact:public",
        }
    assert {f.fact_ref for f in project_facts(
        all_facts, alice, now=NOW).facts} == {
            "fact:alice-private", "fact:alice-shared",
        }
    assert project_facts(all_facts, bob, now=NOW).facts == ()
    assert {f.fact_ref for f in project_facts(
        all_facts, shared, now=NOW).facts} == {"fact:team"}
    assert {f.fact_ref for f in project_facts(
        all_facts, global_viewer, now=NOW).facts} == {"fact:public"}


def test_conversation_scope_is_exact_and_never_falls_back_global():
    candidate = _candidate(
        "fact:room", "room-scoped", subject="alice",
        viewer_scope="conversation:group:42", shareability="shared")
    allowed = _viewer("alice", conversation_scope="group:42")
    wrong_room = _viewer("alice", conversation_scope="group:43")
    assert [f.fact_ref for f in project_facts(
        (candidate,), allowed, now=NOW).facts] == ["fact:room"]
    denied = project_facts((candidate,), wrong_room, now=NOW)
    assert denied.facts == ()
    assert denied.denied[0].reason == "conversation_scope_mismatch"


def test_unattested_unknown_stale_and_low_confidence_fail_closed():
    candidates = (
        _candidate("fact:fresh", "fresh"),
        _candidate(
            "fact:stale", "stale",
            observed_at=NOW - timedelta(days=1),
            fresh_until=NOW - timedelta(seconds=1)),
        _candidate("fact:weak", "weak", confidence=0.2),
    )
    unknown = project_facts(
        candidates, _viewer("", attested=False), now=NOW, min_confidence=0.5)
    assert unknown.facts == ()
    assert {d.reason for d in unknown.denied} == {"viewer_unattested"}

    alice = project_facts(
        candidates, _viewer("alice"), now=NOW, min_confidence=0.5)
    assert [f.fact_ref for f in alice.facts] == ["fact:fresh"]
    assert {d.fact_ref: d.reason for d in alice.denied} == {
        "fact:stale": "fact_stale",
        "fact:weak": "confidence_below_floor",
    }


def test_future_observation_is_not_fresh_evidence_yet():
    candidate = _candidate(
        "fact:future",
        "not observed yet",
        observed_at=NOW + timedelta(hours=1),
        fresh_until=NOW + timedelta(hours=2),
    )
    batch = project_facts((candidate,), _viewer("alice"), now=NOW)
    assert batch.facts == ()
    assert batch.denied[0].reason == "fact_not_yet_observed"


def test_conflicting_duplicate_ref_is_dropped_not_first_writer_wins():
    first = _candidate("fact:conflict", "first")
    second = _candidate("fact:conflict", "second")
    batch = project_facts((first, second), _viewer("alice"), now=NOW)
    assert batch.facts == ()
    assert batch.denied[0].reason == "fact_ref_content_conflict"


def test_projection_is_deterministic_bounded_and_carries_no_denied_content():
    candidates = tuple(
        _candidate(f"fact:{i:02d}", f"fact text {i}", confidence=0.99 - i / 100)
        for i in range(8)
    ) + (
        _candidate(
            "fact:bob-private", "Bob's private medical detail", subject="bob",
            viewer_scope="person:bob", shareability="subject_private"),
    )
    one = project_facts(
        tuple(reversed(candidates)), _viewer("alice"), now=NOW,
        max_facts=3, max_total_chars=100)
    two = project_facts(
        candidates, _viewer("alice"), now=NOW,
        max_facts=3, max_total_chars=100)
    assert one == two
    assert [f.fact_ref for f in one.facts] == ["fact:00", "fact:01", "fact:02"]
    assert one.truncated is True
    assert len(one.audit_digest) == 64
    public = one.public()
    assert "Bob's private medical detail" not in repr(public)
    assert "fact text 7" not in repr(public)


def test_projection_rejects_unbounded_candidate_batch():
    candidate = _candidate("fact:one", "one")
    with pytest.raises(ValueError, match="candidate limit"):
        project_facts((candidate,) * 513, _viewer("alice"), now=NOW)
