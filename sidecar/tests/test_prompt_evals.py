"""Prompt eval harness: a golden set of canned situations/model outputs with
expected decisions, run on every prompt change so prompt work is measurable.

Two layers:
1. Composition contracts: what every charter-built prompt MUST contain
   (doctrine, role, injected sections, budgets, output contracts demanding
   confidence).
2. Decision goldens: canned model outputs -> the parse/gate decision the
   system must reach (propose vs drop, act vs ask vs hold, plan vs reject).

Doctrine changes go ONLY in the charter; if a golden here breaks, either the
charter version was bumped deliberately (update the golden with the doc note)
or the behavior regressed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from colony_sidecar.cognition.charter import (
    PROMPT_VERSION, ROLE_BLOCKS, SECTION_BUDGETS, build_system_prompt,
)


class FakeRouter:
    def __init__(self, content):
        self.content = content
        self.messages = None

    async def complete(self, messages, **kwargs):
        self.messages = messages
        return SimpleNamespace(content=self.content)


# ===========================================================================
# Layer 1: composition contracts
# ===========================================================================

def test_every_role_carries_charter_doctrine_and_output_contract():
    for role in ROLE_BLOCKS:
        fmt = {}
        if role == "thinker":
            fmt = {"max_items": 3, "allowed": ["task"]}
        elif role == "planner":
            fmt = {"max_steps": 8}
        p = build_system_prompt(role, **fmt)
        assert p.startswith("<charter>")
        assert "Agency doctrine" in p
        assert "stated confidence is data" in p.lower() or \
               "Your stated confidence is data" in p
        assert "<role>" in p and "<output>" in p


def test_unknown_role_raises():
    with pytest.raises(KeyError):
        build_system_prompt("mastermind")


def test_sections_injected_only_when_supplied():
    p = build_system_prompt("executor")
    for tag in ("<self_model>", "<boundaries>", "<skills>", "<corrections>"):
        assert tag not in p
    p2 = build_system_prompt(
        "executor", self_brief="You reliably complete research (p=0.9, n=12).",
        boundaries="MUST NOT: touch the billing spreadsheet",
        skills="- Recover serving stack: check logs first",
        corrections=["reporting completion without observed evidence"])
    assert "<self_model>" in p2 and "billing spreadsheet" in p2
    assert "<skills>" in p2
    assert "avoid: reporting completion without observed evidence" in p2


def test_budgets_cap_injections_with_marker():
    huge = "x" * (SECTION_BUDGETS["skills"] + 500)
    p = build_system_prompt("executor", skills=huge)
    body = p.split("<skills>")[1].split("</skills>")[0]
    assert len(body) <= SECTION_BUDGETS["skills"] + 2
    assert "truncated to budget" in body


def test_judgment_bearing_contracts_demand_confidence():
    thinker = build_system_prompt("thinker", max_items=3, allowed=["task"])
    planner = build_system_prompt("planner", max_steps=8)
    executor = build_system_prompt("executor")
    assert '"confidence": float 0.0-1.0' in thinker
    assert '"evidence"' in thinker
    assert '"confidence": float 0.0-1.0' in planner
    assert "confidence 0.0-1.0" in executor


def test_prompt_version_recorded_in_journal():
    from colony_sidecar.self_model import ActionJournal
    j = ActionJournal()
    j.record("research", "did a thing", decision="acted")
    assert j.recent()[0]["prompt_version"] == PROMPT_VERSION


# ===========================================================================
# Layer 2: decision goldens (canned outputs -> expected system decisions)
# ===========================================================================

# ---- thinker goldens ------------------------------------------------------

def _thinker(content):
    from colony_sidecar.intelligence.components.self_directed_thinker import (
        SelfDirectedThinker,
    )
    return SelfDirectedThinker(FakeRouter(content), interval_secs=0)


def _item(**over):
    base = {"title": "Consolidate duplicate memory entries", "type": "task",
            "priority": 0.6, "confidence": 0.7,
            "rationale": "duplication is degrading recall",
            "evidence": "memory stats: 40% duplicate rate this week"}
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_golden_thinker_grounded_item_proposes():
    out = await _thinker(json.dumps([_item()])).think({"memory_stats": {}})
    assert len(out) == 1
    assert out[0].trigger_data["stated_confidence"] == 0.7
    assert "duplicate rate" in out[0].trigger_data["evidence"]


@pytest.mark.asyncio
async def test_golden_thinker_ungrounded_item_dropped():
    out = await _thinker(json.dumps([_item(evidence="")])).think({})
    assert out == []


@pytest.mark.asyncio
async def test_golden_thinker_forbidden_type_dropped():
    out = await _thinker(json.dumps([_item(type="agent_action")])).think({})
    assert out == []


@pytest.mark.asyncio
async def test_golden_thinker_empty_list_is_a_good_answer():
    out = await _thinker("[]").think({"all": "quiet"})
    assert out == []


@pytest.mark.asyncio
async def test_golden_thinker_priority_capped():
    out = await _thinker(json.dumps([_item(priority=1.0)])).think({})
    assert out and out[0].priority <= 0.85


@pytest.mark.asyncio
async def test_golden_thinker_prose_garbage_yields_nothing():
    out = await _thinker("I think we should do lots of things!").think({})
    assert out == []


@pytest.mark.asyncio
async def test_golden_thinker_prompt_carries_situation_and_briefs():
    thinker = _thinker("[]")
    thinker._self_brief_fn = lambda: "You often time out on research (n=6)."
    thinker._boundaries_fn = lambda: "MUST NOT: contact vendors"
    await thinker.think({"open_goals": ["ship the report"]})
    system = thinker._router.messages[0]["content"]
    assert "<self_model>" in system and "time out on research" in system
    assert "<boundaries>" in system and "contact vendors" in system
    assert "ship the report" in thinker._router.messages[1]["content"]


# ---- planner goldens ------------------------------------------------------

def _plan_items(*items):
    return json.dumps(list(items))


@pytest.mark.asyncio
async def test_golden_planner_valid_plan_with_confidence():
    from colony_sidecar.projects.planner import plan_project
    content = _plan_items(
        {"ordinal": 1, "description": "Collect the existing notes",
         "action_kind": "analyze", "depends_on": [], "confidence": 0.9},
        {"ordinal": 2, "description": "Deliver the summary",
         "action_kind": "deliver", "depends_on": [1], "confidence": 0.75},
    )
    steps = await plan_project(FakeRouter(content), "summarize the notes")
    assert [s.confidence for s in steps] == [0.9, 0.75]
    assert steps[1].depends_on == [1]


@pytest.mark.asyncio
async def test_golden_planner_rejects_invented_action_kind():
    from colony_sidecar.projects.planner import plan_project
    content = _plan_items(
        {"ordinal": 1, "description": "ok", "action_kind": "analyze"},
        {"ordinal": 2, "description": "escape the sandbox",
         "action_kind": "self_modify"},
    )
    steps = await plan_project(FakeRouter(content), "objective")
    assert [s.action_kind for s in steps] == ["analyze"]


@pytest.mark.asyncio
async def test_golden_planner_prompt_composed_via_charter():
    from colony_sidecar.projects.planner import plan_project
    router = FakeRouter("[]")
    await plan_project(router, "objective",
                       boundaries="MUST NOT: touch prod",
                       self_brief="load high")
    system = router.messages[0]["content"]
    assert system.startswith("<charter>")
    assert "<boundaries>" in system and "touch prod" in system
    assert "Allowed action kinds" in system     # vocabulary in <context>


# ---- trust gate goldens ---------------------------------------------------

def _trust(wins=0, losses=0, stage=None, domain="research"):
    from colony_sidecar.self_model import ActionJournal, CompetenceStore, TrustEngine
    store = CompetenceStore()
    for _ in range(wins):
        store.record(domain, "success")
    for _ in range(losses):
        store.record(domain, "failure")
    t = TrustEngine(store, journal=ActionJournal())
    if stage:
        t.set_stage(domain, stage, notify=False)
    return t


def test_golden_trust_strong_record_acts():
    t = _trust(wins=10, stage="act_first")
    assert t.gate("research", "summarize new papers")["decision"] == "act"


def test_golden_trust_weak_record_asks_even_at_act_first():
    t = _trust(wins=1, losses=4, stage="act_first")
    assert t.gate("research", "summarize new papers")["decision"] == "ask"


def test_golden_trust_calibration_stage_holds():
    t = _trust()
    assert t.gate("research", "summarize", default_stage="shadow")["decision"] == "hold"


def test_golden_trust_floor_asks_regardless():
    t = _trust(wins=50, stage="act_first")
    out = t.gate("research", "rotate the api key for the billing service")
    assert out["decision"] == "ask" and out["floor"] == "credential_change"


# ---- calibration golden ---------------------------------------------------

def test_golden_stated_vs_realized_calibration():
    from colony_sidecar.self_model import CompetenceStore
    s = CompetenceStore()
    s.record("research", "success", stated_confidence=0.8)   # err 0.2
    s.record("research", "failure", stated_confidence=0.6)   # err 0.6
    cal = s.calibration("research")
    assert cal["n"] == 2
    assert abs(cal["mean_abs_error"] - 0.4) < 0.001
    assert s.calibration("nothing") is None
