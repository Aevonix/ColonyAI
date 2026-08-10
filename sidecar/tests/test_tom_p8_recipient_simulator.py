"""P8 deterministic recipient simulation, advisory and side-effect free."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from colony_sidecar.tom.arcs import ArcEventV1, ArcStore
from colony_sidecar.tom.recipient_simulator import (
    FAIL_BEHAVIOR_BY_RISK,
    RecipientSimulationRequestV1,
    RecipientSimulator,
    recipient_simulator_mode,
)
from colony_sidecar.tom.visibility import (
    FactCandidateV1,
    FactVisibilityV1,
    ViewerContextV1,
    content_digest,
)


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def _viewer(person="alice", *, attested=True):
    return ViewerContextV1(
        principal_id=f"surface:{person}" if person else "",
        viewer_person_id=person,
        owner_person_id="owner",
        audiences=("viewer",),
        conversation_scope="dm:alice",
        scope_revision="scope-rev-1" if attested else "",
        attested=attested,
    )


def _fact(ref, text, *, viewer="alice", fresh=True):
    return FactCandidateV1(
        content=text,
        visibility=FactVisibilityV1(
            fact_ref=ref,
            content_digest=content_digest(text),
            source_ref=f"turn:{ref}",
            subject_person_id=viewer,
            viewer_scope=f"person:{viewer}",
            shareability="subject_private",
            confidence=0.9,
            observed_at=(NOW - timedelta(hours=1)).isoformat(),
            fresh_until=(
                NOW + timedelta(days=1) if fresh
                else NOW - timedelta(seconds=1)
            ).isoformat(),
            evidence_refs=(f"turn:{ref}",),
        ),
    )


def _request(
    *,
    draft="Here is the launch update.",
    refs=("fact:alice",),
    risk_class="medium",
    surface="chat",
    recipient=None,
    high_salience=True,
):
    return RecipientSimulationRequestV1(
        simulation_id="simulation:1",
        draft_text=draft,
        draft_fact_refs=refs,
        recipient=recipient or _viewer(),
        risk_class=risk_class,
        surface=surface,
        high_salience=high_salience,
        created_at=NOW.isoformat(),
    )


def _arc_store(tmp_path):
    store = ArcStore(str(tmp_path / "arcs.db"))
    store.append(ArcEventV1.open(
        event_id="event:stress",
        idempotency_key="arc:stress:open",
        arc_id="arc:stress",
        arc_type="stress_topic",
        topic="launch deadline",
        people=("alice",),
        source_turn_ref="turn:stress",
        source_ref="turn:stress",
        viewer_scope="person:alice",
        shareability="subject_private",
        subject_person_id="alice",
        evidence_refs=("turn:stress",),
        occurred_at=(NOW - timedelta(hours=1)).isoformat(),
    ))
    return store


def test_mode_defaults_off_and_invalid_is_off(monkeypatch):
    monkeypatch.delenv("COLONY_RECIPIENT_SIMULATOR_MODE", raising=False)
    assert recipient_simulator_mode() == "off"
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "surprise")
    assert recipient_simulator_mode() == "off"


def test_off_mode_does_not_touch_dependencies_or_create_effect(monkeypatch):
    monkeypatch.delenv("COLONY_RECIPIENT_SIMULATOR_MODE", raising=False)

    class ExplodingArcs:
        def project_active(self, *args, **kwargs):
            raise AssertionError("off mode touched arcs")

    result = RecipientSimulator(arc_store=ExplodingArcs()).simulate(
        _request(), fact_candidates=(_fact("fact:alice", "allowed"),), now=NOW)
    assert result.mode == "off"
    assert result.evaluated is False
    assert result.recommended_action == "no_effect"
    assert result.external_effect is False
    assert result.authority_granted is False
    assert result.synchronous_gate is False


def test_shadow_uses_only_authorized_facts_and_flags_cross_person_ref(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    allowed = _fact("fact:alice", "Alice approved the launch update")
    denied = _fact("fact:bob", "Bob's private medical diagnosis", viewer="bob")
    simulator = RecipientSimulator(arc_store=_arc_store(tmp_path))
    result = simulator.simulate(
        _request(refs=("fact:alice", "fact:bob")),
        fact_candidates=(allowed, denied),
        now=NOW,
    )
    assert result.evaluated is True
    assert result.authorized_fact_refs == ("fact:alice",)
    assert "fact:bob" not in result.authorized_fact_refs
    assert any(r.code == "fact_ref_not_recipient_authorized"
               and r.severity == "critical" for r in result.risks)
    assert result.would_recommend == "hold"
    assert result.recommended_action == "observe_only"
    assert result.external_effect is False
    assert result.authority_granted is False
    assert "medical diagnosis" not in repr(result.public())


def test_stale_and_unknown_facts_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    stale = _fact("fact:stale", "stale update", fresh=False)
    result = RecipientSimulator(arc_store=ArcStore(
        str(tmp_path / "arcs.db"))).simulate(
            _request(refs=("fact:stale", "fact:unknown")),
            fact_candidates=(stale,),
            now=NOW,
        )
    boundary = [r for r in result.risks
                if r.code == "fact_ref_not_recipient_authorized"]
    assert len(boundary) == 2
    assert result.would_recommend == "hold"


def test_active_stress_arc_produces_structured_repair(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "live")
    result = RecipientSimulator(arc_store=_arc_store(tmp_path)).simulate(
        _request(
            draft="URGENT: you must finish the launch deadline now.",
            refs=("fact:alice",),
        ),
        fact_candidates=(_fact("fact:alice", "launch deadline is Friday"),),
        now=NOW,
    )
    assert result.active_arc_refs == ("arc:stress",)
    assert any(r.code == "stress_topic_pressure" for r in result.risks)
    assert any(s.code == "soften_pressure_language" for s in result.repairs)
    assert result.recommended_action in {"review", "repair"}


@pytest.mark.parametrize(
    "risk_class, expected",
    [("low", "observe"), ("medium", "review"),
     ("high", "hold"), ("critical", "hold")],
)
def test_dependency_failure_behavior_is_explicit_per_risk(
    risk_class, expected, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "live")

    class BrokenArcs:
        def project_active(self, *args, **kwargs):
            raise RuntimeError("arc store unavailable")

    result = RecipientSimulator(arc_store=BrokenArcs()).simulate(
        _request(risk_class=risk_class),
        fact_candidates=(_fact("fact:alice", "allowed"),),
        now=NOW,
    )
    assert FAIL_BEHAVIOR_BY_RISK[risk_class] == expected
    assert result.fail_behavior == expected
    assert result.recommended_action == expected
    assert any(r.code == "simulation_dependency_error" for r in result.risks)
    assert result.authority_granted is False


def test_unknown_recipient_never_receives_fact_or_arc_projection(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    unknown = _viewer("", attested=False)
    result = RecipientSimulator(arc_store=_arc_store(tmp_path)).simulate(
        _request(recipient=unknown),
        fact_candidates=(_fact("fact:alice", "allowed"),),
        now=NOW,
    )
    assert result.authorized_fact_refs == ()
    assert result.active_arc_refs == ()
    assert any(r.code == "recipient_identity_unattested" for r in result.risks)
    assert result.would_recommend == "hold"


@pytest.mark.parametrize(
    "surface",
    [
        "voice", "phone", "intercom", "meet", "google_meet",
        "google-meet", "googlemeet", "phone_call", "whatsapp_call",
        "sip", "pstn", "webrtc",
    ],
)
def test_realtime_voice_surfaces_are_async_observation_only(
    surface, tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "live")
    result = RecipientSimulator(arc_store=_arc_store(tmp_path)).simulate(
        _request(
            surface=surface, refs=("fact:bob",), risk_class="critical"),
        fact_candidates=(_fact("fact:bob", "private", viewer="bob"),),
        now=NOW,
    )
    assert result.would_recommend == "hold"
    assert result.recommended_action == "observe_async"
    assert result.synchronous_gate is False
    assert result.evaluation_path == "async_observation"


@pytest.mark.parametrize("surface", ["chat", "text", "rcs", "whatsapp"])
def test_text_surfaces_are_not_misclassified_as_realtime_voice(
    surface, tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "live")
    result = RecipientSimulator(arc_store=ArcStore(
        str(tmp_path / f"{surface}.db"))).simulate(
            _request(surface=surface, high_salience=False),
            fact_candidates=(_fact("fact:alice", "allowed"),),
            now=NOW,
        )
    assert result.would_recommend == "send"
    assert result.recommended_action == "send"
    assert result.evaluation_path == "pre_send_advisory"
    assert result.synchronous_gate is False


def test_replay_is_deterministic_and_simulation_has_no_store_effect(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    store = _arc_store(tmp_path)
    before = store.event_count()
    simulator = RecipientSimulator(arc_store=store)
    request = _request()
    candidates = (_fact("fact:alice", "allowed"),)
    first = simulator.simulate(request, fact_candidates=candidates, now=NOW)
    second = simulator.simulate(request, fact_candidates=candidates, now=NOW)
    assert first == second
    assert first.audit_digest == second.audit_digest
    assert store.event_count() == before
    assert first.external_effect is False
    assert first.authority_granted is False


def test_high_salience_message_without_fact_refs_is_unknown_provenance(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    result = RecipientSimulator(
        arc_store=ArcStore(str(tmp_path / "arcs.db"))).simulate(
            _request(refs=(), high_salience=True),
            fact_candidates=(_fact("fact:alice", "allowed"),),
            now=NOW,
        )
    assert any(r.code == "high_salience_provenance_unknown"
               for r in result.risks)
    assert result.would_recommend in {"review", "repair"}
