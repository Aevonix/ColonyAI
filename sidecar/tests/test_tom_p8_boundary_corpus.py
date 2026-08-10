"""Small person-crossing corpus: no disallowed fact or arc may cross."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from colony_sidecar.tom.arcs import ArcEventV1, ArcStore
from colony_sidecar.tom.recipient_simulator import (
    RecipientSimulationRequestV1,
    RecipientSimulator,
)
from colony_sidecar.tom.visibility import (
    FactCandidateV1,
    FactVisibilityV1,
    ViewerContextV1,
    content_digest,
    project_facts,
)


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def _viewer(person):
    return ViewerContextV1(
        principal_id=f"surface:{person}", viewer_person_id=person,
        owner_person_id="owner", audiences=("viewer",),
        conversation_scope=f"dm:{person}", scope_revision="scope-rev-1",
        attested=True,
    )


def _fact(ref, text, subject):
    return FactCandidateV1(
        content=text,
        visibility=FactVisibilityV1(
            fact_ref=ref, content_digest=content_digest(text),
            source_ref=f"turn:{ref}", subject_person_id=subject,
            viewer_scope=f"person:{subject}",
            shareability="subject_private", confidence=0.95,
            observed_at=NOW.isoformat(),
            fresh_until=(NOW + timedelta(days=1)).isoformat(),
            evidence_refs=(f"turn:{ref}",),
        ),
    )


def _add_arc(store, person):
    store.append(ArcEventV1.open(
        event_id=f"event:{person}", idempotency_key=f"arc:{person}:open",
        arc_id=f"arc:{person}", arc_type="unresolved_social_moment",
        topic=f"{person} sensitive conversation", people=(person,),
        source_turn_ref=f"turn:{person}", source_ref=f"turn:{person}",
        viewer_scope=f"person:{person}", shareability="subject_private",
        subject_person_id=person,
        evidence_refs=(f"turn:{person}",), occurred_at=NOW.isoformat(),
    ))


def test_cross_person_visibility_and_simulation_corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    candidates = (
        _fact("fact:alice", "Alice private launch detail", "alice"),
        _fact("fact:bob", "Bob private health detail", "bob"),
        _fact("fact:carol", "Carol private finance detail", "carol"),
    )
    store = ArcStore(str(tmp_path / "arcs.db"))
    for person in ("alice", "bob", "carol"):
        _add_arc(store, person)
    simulator = RecipientSimulator(arc_store=store)

    for person in ("alice", "bob", "carol"):
        viewer = _viewer(person)
        projected = project_facts(candidates, viewer, now=NOW)
        assert [fact.fact_ref for fact in projected.facts] == [f"fact:{person}"]
        arcs = store.project_active(viewer, now=NOW)
        assert [arc.arc_id for arc in arcs.arcs] == [f"arc:{person}"]

        other = next(candidate for candidate in candidates
                     if candidate.visibility.subject_person_id != person)
        result = simulator.simulate(
            RecipientSimulationRequestV1(
                simulation_id=f"simulation:{person}",
                draft_text="A bounded update",
                draft_fact_refs=(f"fact:{person}",
                                 other.visibility.fact_ref),
                recipient=viewer, risk_class="high", surface="chat",
                high_salience=True, created_at=NOW.isoformat(),
            ),
            fact_candidates=candidates,
            now=NOW,
        )
        assert result.authorized_fact_refs == (f"fact:{person}",)
        assert result.active_arc_refs == (f"arc:{person}",)
        assert result.would_recommend == "hold"
        rendered = repr(result.public())
        for candidate in candidates:
            if candidate.visibility.subject_person_id != person:
                assert candidate.content not in rendered
