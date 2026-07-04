"""Belief maintenance: contradictions, resolution, audit, decay (item 7)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from colony_sidecar.beliefs import (
    BeliefEngine, BeliefStore, Claim, claims_from_text, detect_conflicts,
    pick_winner, source_trust,
)
from colony_sidecar.self_model import ActionJournal


# ---------------------------------------------------------------------------
# Claim extraction (conservative)
# ---------------------------------------------------------------------------

def test_claims_from_copular_text():
    claims = claims_from_text("Jordan works at Initech. Some noise here that "
                              "should not match anything at all.")
    assert len(claims) == 1
    c = claims[0]
    assert c.subject == "Jordan" and c.predicate == "works_at"
    assert c.value == "Initech"


def test_claims_possessive_and_location():
    claims = claims_from_text("Avery's title is staff engineer. "
                              "Quinn lives in Lisbon.")
    preds = {c.predicate for c in claims}
    assert "title" in preds and "location" in preds


def test_long_or_junk_sentences_ignored():
    assert claims_from_text("it was a dark and stormy night " * 10) == []
    assert claims_from_text("The value is unknown.") == []


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

def _claim(value, subject="Jordan", predicate="works_at", conf=0.5, ts=0.0,
           source="inference", ref=""):
    return Claim(subject=subject, predicate=predicate, value=value,
                 confidence=conf, ts=ts, source=source, ref=ref)


def test_same_subject_predicate_conflicting_value_detected():
    conflicts = detect_conflicts([_claim("Initech"), _claim("Globex")])
    assert len(conflicts) == 1


def test_agreeing_values_are_corroboration_not_conflict():
    assert detect_conflicts([_claim("Initech"), _claim("initech corp")]) == []


def test_different_predicates_do_not_conflict():
    assert detect_conflicts([_claim("Initech"),
                             _claim("Lisbon", predicate="location")]) == []


# ---------------------------------------------------------------------------
# Resolution ordering: recency > confidence > source trust
# ---------------------------------------------------------------------------

def test_recency_wins_first():
    old = _claim("Initech", ts=time.time() - 30 * 86400, conf=0.9)
    new = _claim("Globex", ts=time.time(), conf=0.5)
    winner, loser = pick_winner(old, new)
    assert winner.value == "Globex"


def test_confidence_breaks_recency_tie():
    now = time.time()
    a = _claim("Initech", ts=now, conf=0.9)
    b = _claim("Globex", ts=now - 100, conf=0.5)
    winner, _ = pick_winner(a, b)
    assert winner.value == "Initech"


def test_source_trust_breaks_remaining_tie(monkeypatch):
    now = time.time()
    a = _claim("Initech", ts=now, conf=0.5, source="user_assertion")
    b = _claim("Globex", ts=now, conf=0.5, source="inference")
    winner, _ = pick_winner(a, b)
    assert winner.value == "Initech"


def test_full_tie_is_unresolvable():
    now = time.time()
    a = _claim("Initech", ts=now, conf=0.5, source="inference")
    b = _claim("Globex", ts=now, conf=0.5, source="inference")
    assert pick_winner(a, b) is None


def test_source_trust_env_override(monkeypatch):
    monkeypatch.setenv("COLONY_SOURCE_TRUST", "connector:0.95")
    assert source_trust("connector") == 0.95
    assert source_trust("owner") == 1.0


# ---------------------------------------------------------------------------
# Engine passes (fakes; no Neo4j)
# ---------------------------------------------------------------------------

class FakeGraph:
    def __init__(self, rows):
        self.rows = rows
        self.transitions = []

    async def run_query(self, cypher, params):
        return self.rows

    async def transition_epistemic_state(self, memory_id, new_state,
                                         superseded_by=None):
        self.transitions.append((memory_id, new_state, superseded_by))


class FakeInitStore:
    def __init__(self):
        self.created = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id="init-x")


class FakeWorld:
    def __init__(self, entities):
        self.entities = entities
        self.upserts = []

    async def find_entities(self, query="", min_confidence=0.0, limit=500,
                            entity_type=None):
        return self.entities

    async def upsert_entity(self, e):
        self.upserts.append(e)
        return e


def _entity(name="Widget API", conf=0.8, last_seen=None, props=None):
    return SimpleNamespace(
        id="we-1", name=name, entity_type="project", confidence=conf,
        properties=props or {}, last_seen=last_seen, updated_at=last_seen,
        created_at=last_seen, aliases=[])


def _mem_row(mid, content, conf=0.5, source="conversation", ts=None):
    class _TS:
        def __init__(self, dt):
            self._dt = dt

        def to_native(self):
            return self._dt
    return {"id": mid, "content": content, "confidence": conf,
            "source_type": source,
            "created_at": _TS(ts or datetime.now(timezone.utc))}


@pytest.mark.asyncio
async def test_live_resolution_supersedes_and_audits(monkeypatch):
    monkeypatch.setenv("COLONY_BELIEFS_MODE", "live")
    old_ts = datetime.now(timezone.utc) - timedelta(days=30)
    graph = FakeGraph([
        _mem_row("m-old", "Jordan works at Initech.", ts=old_ts),
        _mem_row("m-new", "Jordan works at Globex."),
    ])
    store = BeliefStore()
    engine = BeliefEngine(store, graph=graph, journal=ActionJournal())
    report = await engine.run()
    assert report["conflicts_detected"] == 1
    assert report["resolved"] == 1
    # loser memory transitioned to superseded
    assert graph.transitions and graph.transitions[0][0] == "m-old"
    assert graph.transitions[0][1] == "superseded"
    # audit row written
    sups = store.supersessions()
    assert sups and sups[0]["scope"] == "graph"
    assert store.conflicts(status="resolved")


@pytest.mark.asyncio
async def test_shadow_detects_but_never_mutates(monkeypatch):
    monkeypatch.setenv("COLONY_BELIEFS_MODE", "shadow")
    old_ts = datetime.now(timezone.utc) - timedelta(days=30)
    graph = FakeGraph([
        _mem_row("m-old", "Jordan works at Initech.", ts=old_ts),
        _mem_row("m-new", "Jordan works at Globex."),
    ])
    store = BeliefStore()
    engine = BeliefEngine(store, graph=graph)
    report = await engine.run()
    assert report["conflicts_detected"] == 1
    assert report["resolved"] == 0
    assert graph.transitions == []           # nothing mutated
    assert store.conflicts(status="open")    # recorded for review


@pytest.mark.asyncio
async def test_unresolvable_surfaces_review_initiative(monkeypatch):
    monkeypatch.setenv("COLONY_BELIEFS_MODE", "live")
    now = datetime.now(timezone.utc)
    graph = FakeGraph([
        _mem_row("m-a", "Jordan works at Initech.", ts=now),
        _mem_row("m-b", "Jordan works at Globex.", ts=now),
    ])
    inits = FakeInitStore()
    engine = BeliefEngine(BeliefStore(), graph=graph, initiative_store=inits)
    report = await engine.run()
    assert report["review_initiatives"] == 1
    assert graph.transitions == []           # no silent pick
    assert inits.created and inits.created[0]["type"] == "data_quality"
    assert "Belief conflict" in inits.created[0]["description"]


@pytest.mark.asyncio
async def test_world_property_change_writes_supersession_audit(monkeypatch):
    monkeypatch.setenv("COLONY_BELIEFS_MODE", "shadow")
    ent = _entity(props={"status": "active", "_conf_status": 0.6})
    world = FakeWorld([ent])
    store = BeliefStore()
    engine = BeliefEngine(store, world_store=world)
    await engine.run()                        # first scan: snapshot only
    assert store.supersessions() == []
    ent.properties["status"] = "paused"
    report = await engine.run()               # second scan: value changed
    assert report["supersessions"] == 1
    sup = store.supersessions()[0]
    assert sup["old_value"] == "active" and sup["new_value"] == "paused"


@pytest.mark.asyncio
async def test_decay_lowers_confidence_past_ttl(monkeypatch):
    monkeypatch.setenv("COLONY_BELIEFS_MODE", "live")
    monkeypatch.setenv("COLONY_BELIEFS_STALE_DAYS", "10")
    stale_dt = datetime.now(timezone.utc) - timedelta(days=30)
    fresh_dt = datetime.now(timezone.utc)
    stale = _entity(name="Old Thing", conf=0.8, last_seen=stale_dt)
    fresh = _entity(name="Fresh Thing", conf=0.8, last_seen=fresh_dt)
    world = FakeWorld([stale, fresh])
    engine = BeliefEngine(BeliefStore(), world_store=world,
                          journal=ActionJournal())
    report = await engine.run()
    assert report["decayed"] == 1
    assert stale.confidence < 0.8
    assert fresh.confidence == 0.8


@pytest.mark.asyncio
async def test_decay_shadow_does_not_mutate(monkeypatch):
    monkeypatch.setenv("COLONY_BELIEFS_MODE", "shadow")
    monkeypatch.setenv("COLONY_BELIEFS_STALE_DAYS", "10")
    stale = _entity(conf=0.8,
                    last_seen=datetime.now(timezone.utc) - timedelta(days=30))
    world = FakeWorld([stale])
    engine = BeliefEngine(BeliefStore(), world_store=world)
    await engine.run()
    assert stale.confidence == 0.8 and world.upserts == []


def test_beliefs_graduation_requires_act_first(monkeypatch):
    from colony_sidecar.self_model import (
        ActionJournal, CompetenceStore, SelfModel, TrustEngine,
    )
    monkeypatch.setenv("COLONY_BELIEFS_MODE", "shadow")
    store = CompetenceStore()
    trust = TrustEngine(store, journal=ActionJournal())
    sm = SelfModel(store, trust=trust)
    engine = BeliefEngine(BeliefStore(), self_model=sm)
    assert engine._effective_mode() == "shadow"
    trust.set_stage("beliefs", "ask_first", notify=False)
    assert engine._effective_mode() == "shadow"   # ask_first is not enough
    trust.set_stage("beliefs", "act_first", notify=False)
    assert engine._effective_mode() == "live"
    monkeypatch.setenv("COLONY_BELIEFS_MODE", "off")
    assert engine._effective_mode() == "off"


def test_inline_property_hook_records_audit():
    store = BeliefStore()
    engine = BeliefEngine(store)
    engine.note_property_update("we-1", "status", "active", "paused", 0.5, 0.7)
    sup = store.supersessions()
    assert sup and sup[0]["predicate"] == "status"
    # same value -> no audit row
    engine.note_property_update("we-1", "status", "paused", "paused", 0.7, 0.8)
    assert len(store.supersessions()) == 1
