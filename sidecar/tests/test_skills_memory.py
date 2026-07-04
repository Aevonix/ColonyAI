"""Skills memory: distillation triggers, dedup, cap, retrieval (item 3)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from colony_sidecar.skills_memory import (
    Skill, SkillStore, distill_from_completion, format_block,
    relevant_skills, should_distill, signature_overlap,
)


class FakeRouter:
    def __init__(self, content):
        self.content = content
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(content=self.content)


def _skill_json(title="Recover a wedged serving stack",
                situation="inference server hangs on long prompts after deploy"):
    return json.dumps({
        "title": title,
        "situation": situation,
        "steps": ["check the serving logs for kernel errors",
                  "re-apply the live patch set", "re-run the preflight probe"],
        "gotchas": ["a restarted container loses live patches"],
    })


# ---------------------------------------------------------------------------
# Trigger conditions
# ---------------------------------------------------------------------------

def test_retry_success_triggers():
    assert should_distill(1, "done", SkillStore()) is True


def test_first_try_plain_success_does_not_trigger():
    assert should_distill(0, "completed the task fine", SkillStore()) is False


def test_novel_diagnosis_triggers():
    assert should_distill(0, "Root cause was a stale cache entry; fixed by "
                          "flushing the index.", SkillStore()) is True


def test_known_diagnosis_does_not_retrigger():
    store = SkillStore()
    store.add(Skill(title="Flush stale cache",
                    situation="root cause stale cache entry fixed by "
                              "flushing the index"))
    assert should_distill(0, "Root cause was a stale cache entry; fixed by "
                          "flushing the index.", store) is False


# ---------------------------------------------------------------------------
# Distillation + dedup + modes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_distill_live_stores_skill():
    store = SkillStore()
    skill = await distill_from_completion(
        FakeRouter(_skill_json()), store, domain="research",
        task_text="diagnose the wedged inference stack",
        result_text="fixed after retries", mode="live")
    assert skill is not None and store.count() == 1
    assert store.list()[0].domain == "research"


@pytest.mark.asyncio
async def test_distill_shadow_logs_without_storing():
    store = SkillStore()
    skill = await distill_from_completion(
        FakeRouter(_skill_json()), store, domain="research",
        task_text="t", result_text="r", mode="shadow")
    assert skill is not None
    assert store.count() == 0


@pytest.mark.asyncio
async def test_distill_dedup_bumps_existing():
    store = SkillStore()
    first = await distill_from_completion(
        FakeRouter(_skill_json()), store, domain="research",
        task_text="t", result_text="r", mode="live")
    again = await distill_from_completion(
        FakeRouter(_skill_json()), store, domain="research",
        task_text="t", result_text="r", mode="live")
    assert store.count() == 1
    assert again.id == first.id
    assert store.get(first.id).uses == 1


@pytest.mark.asyncio
async def test_distill_null_and_garbage_dropped():
    store = SkillStore()
    assert await distill_from_completion(
        FakeRouter("null"), store, domain="d", task_text="t",
        result_text="r", mode="live") is None
    assert await distill_from_completion(
        FakeRouter("not json at all"), store, domain="d", task_text="t",
        result_text="r", mode="live") is None
    assert store.count() == 0


# ---------------------------------------------------------------------------
# Cap / evict
# ---------------------------------------------------------------------------

def test_cap_evicts_lowest_score():
    store = SkillStore()
    keeper = Skill(title="high value", situation="alpha beta gamma",
                   confidence=0.9)
    keeper.wins = 5
    store.add(keeper)
    for i in range(4):
        store.add(Skill(title=f"low value {i}",
                        situation=f"delta epsilon {i}", confidence=0.2))
    evicted = store.evict_to_cap(2)
    assert evicted == 3
    remaining = {s.id for s in store.list()}
    assert keeper.id in remaining and len(remaining) == 2


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def test_retrieval_ranks_by_overlap_and_formats_block():
    store = SkillStore()
    store.add(Skill(title="Recover serving stack",
                    situation="inference server hangs after deploy",
                    steps=["check logs", "re-apply patches"],
                    gotchas=["container restart loses patches"],
                    domain="research"))
    store.add(Skill(title="Unrelated gardening",
                    situation="prune tomato plants in summer"))
    hits = relevant_skills(store, "the inference server hangs after a deploy",
                           k=3)
    assert len(hits) >= 1
    assert hits[0].title == "Recover serving stack"
    block = format_block(hits, strategy_note="- avoid re-running failed calls")
    assert "Relevant past procedures" in block
    assert "1. check logs" in block
    assert "! gotcha:" in block
    assert "Lessons from past failures" in block


def test_retrieval_empty_situation_or_store():
    assert relevant_skills(None, "anything") == []
    assert relevant_skills(SkillStore(), "") == []
    assert format_block([]) == ""


# ---------------------------------------------------------------------------
# Failure post-mortem notes
# ---------------------------------------------------------------------------

def test_failure_note_appends_dedups_and_caps():
    store = SkillStore()
    store.record_failure_note("research", "web fetch timed out on site X")
    store.record_failure_note("research", "web fetch timed out on site X")
    note = store.get_note("research")
    assert note.count("web fetch timed out") == 1
    for i in range(40):
        store.record_failure_note("research", f"failure mode number {i} with details")
    assert len(store.get_note("research")) <= 600


def test_signature_overlap_bounds():
    assert signature_overlap("a b c", "a b c") == 1.0
    assert signature_overlap("a b", "c d") == 0.0
    assert signature_overlap("", "a") == 0.0
