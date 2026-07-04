"""Directed action under the trust engine (Amendment 1.7)."""

from __future__ import annotations

import pytest

from colony_sidecar.directed import DirectedActionService, ScopedTaskStore
from colony_sidecar.self_model import (
    ActionJournal, CompetenceStore, SelfModel, TrustEngine,
)

_KNOWN_DIRECTIVE_READ = "look at the widget repo and summarize recent changes"
_KNOWN_DIRECTIVE_MUTATE = "fix the retry bug in the billing service code"


def _self_model():
    store = CompetenceStore()
    journal = ActionJournal()
    trust = TrustEngine(store, journal=journal)
    return SelfModel(store, trust=trust, journal=journal)


def _service(sm=None, deliver=None):
    return DirectedActionService(
        store=ScopedTaskStore(), self_model=sm, delivery_router=deliver)


@pytest.mark.asyncio
async def test_read_only_auto_approves_and_journals():
    sm = _self_model()
    svc = _service(sm)
    task = await svc.intake(_KNOWN_DIRECTIVE_READ)
    assert task.status == "approved"
    entries = sm.journal.recent(domain="directed:read")
    assert entries and entries[0]["decision"] == "acted"


@pytest.mark.asyncio
async def test_mutating_asks_first_with_reasoning_and_confidence():
    sm = _self_model()
    sent = []

    async def deliver(payload):
        sent.append(payload)
        return True

    svc = _service(sm, deliver=deliver)
    task = await svc.intake(_KNOWN_DIRECTIVE_MUTATE)
    assert task.mutating and task.status == "awaiting_approval"
    assert "confidence" in task.approval
    # the ask reached the owner WITH reasoning + confidence
    assert sent and sent[0]["type"] == "proposal"
    assert "Approval needed" in sent[0]["title"]
    assert "confidence" in sent[0]["description"].lower()


@pytest.mark.asyncio
async def test_mutating_earned_act_first_self_approves():
    sm = _self_model()
    svc = _service(sm)
    probe = await svc.intake(_KNOWN_DIRECTIVE_MUTATE)
    domain = svc._trust_domain(probe)
    # earn the class: clean audited real outcomes
    for _ in range(6):
        sm.record(domain, "success")
    sm.trust.set_stage(domain, "act_first", notify=False)
    task = await svc.intake(_KNOWN_DIRECTIVE_MUTATE)
    assert task.status == "approved"
    assert task.approval.get("granted_by") == "trust_engine"


@pytest.mark.asyncio
async def test_violation_records_and_demotes():
    sm = _self_model()
    svc = _service(sm)
    task = await svc.intake(_KNOWN_DIRECTIVE_MUTATE)
    domain = svc._trust_domain(task)
    sm.trust.set_stage(domain, "act_first", notify=False)
    task.status = "approved"
    svc.store.save(task)
    # delegate reports an out-of-scope op -> audit violation
    out = await svc.complete(task.id, {
        "summary": "did things", "operations": ["push_branch", "commit"],
        "files_touched": [], "commits": 99, "branch": "main"})
    assert out["verdict"] == "violation"
    assert sm.trust.stage(domain) == "ask_first"     # circuit breaker fired


@pytest.mark.asyncio
async def test_clean_read_completion_builds_track_record():
    sm = _self_model()
    svc = _service(sm)
    task = await svc.intake(_KNOWN_DIRECTIVE_READ)
    await svc.complete(task.id, {
        "summary": "summarized", "operations": ["analyze", "read"],
        "files_touched": [], "commits": 0, "branch": ""})
    events = sm.store.events("directed:read", include_shadow=False)
    assert events and events[0]["outcome"] == "success"
