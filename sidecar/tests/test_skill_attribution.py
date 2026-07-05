"""Skill outcome attribution: use changes future retrieval (item 3 closed).

The executor remembers which skills it retrieved into a run's prompt and,
at the terminal outcome, updates their wins/losses; retrieval then weights
by that track record.
"""

import pytest

from colony_sidecar.skills_memory.models import Skill, situation_signature
from colony_sidecar.skills_memory.retrieve import relevant_skills
from colony_sidecar.skills_memory.store import SkillStore


def _skill(title: str, situation: str, domain: str = "research") -> Skill:
    return Skill(
        title=title, situation=situation,
        situation_signature=situation_signature(situation),
        steps=["do the thing"], gotchas=[], domain=domain, source_ref="t",
    )


class _FakeInitiative:
    def __init__(self, iid="init-1", description="probe the flaky endpoint"):
        self.id = iid
        self.description = description
        self.context = {}
        self.attempt_count = 0


def _executor(store):
    from colony_sidecar.services.initiative_executor import (
        InitiativeExecutorService,
    )
    return InitiativeExecutorService(
        initiative_store=object(), reasoning_loop=object(),
        skill_store=store)


class TestAttribution:
    def test_prompt_build_remembers_retrieved_skills(self):
        store = SkillStore()
        s = store.add(_skill("Probe endpoints", "probe a flaky endpoint"))
        svc = _executor(store)
        init = _FakeInitiative()
        svc._build_system_prompt(init, "research")
        assert svc._skills_used.get("init-1") == [s.id]

    def test_success_bumps_wins_failure_bumps_losses(self):
        store = SkillStore()
        s = store.add(_skill("Probe endpoints", "probe a flaky endpoint"))
        svc = _executor(store)

        init = _FakeInitiative("init-w")
        svc._build_system_prompt(init, "research")
        svc._record_outcome("research", "success", initiative=init)
        assert store.get(s.id).wins == 1

        init2 = _FakeInitiative("init-l")
        svc._build_system_prompt(init2, "research")
        svc._record_outcome("research", "failure", initiative=init2)
        got = store.get(s.id)
        assert got.wins == 1 and got.losses == 1

    def test_attribution_is_consumed_once(self):
        store = SkillStore()
        s = store.add(_skill("Probe endpoints", "probe a flaky endpoint"))
        svc = _executor(store)
        init = _FakeInitiative()
        svc._build_system_prompt(init, "research")
        svc._record_outcome("research", "success", initiative=init)
        svc._record_outcome("research", "success", initiative=init)
        assert store.get(s.id).wins == 1  # popped on first record

    def test_no_skills_retrieved_records_nothing(self):
        store = SkillStore()
        svc = _executor(store)
        init = _FakeInitiative(description="zzz qqq completely unrelated")
        svc._build_system_prompt(init, "research")
        svc._record_outcome("research", "success", initiative=init)
        # no crash, no attribution
        assert svc._skills_used == {}


class TestTrackRecordWeighting:
    def test_losing_skill_ranks_below_winning_twin(self):
        store = SkillStore()
        winner = store.add(_skill("Probe endpoints A",
                                  "probe a flaky endpoint carefully"))
        loser = store.add(_skill("Probe endpoints B",
                                 "probe a flaky endpoint carefully"))
        for _ in range(5):
            store.record_outcome(winner.id, True)
            store.record_outcome(loser.id, False)
        top = relevant_skills(store, "probe a flaky endpoint carefully", k=2)
        assert [s.id for s in top][0] == winner.id

    def test_fresh_skill_is_neutral(self):
        # Laplace prior: no record => factor 1.0 (overlap order preserved).
        store = SkillStore()
        exact = store.add(_skill("Exact", "restart the ingest worker safely"))
        store.add(_skill("Vaguer", "restart something"))
        top = relevant_skills(store, "restart the ingest worker safely", k=1)
        assert top and top[0].id == exact.id
