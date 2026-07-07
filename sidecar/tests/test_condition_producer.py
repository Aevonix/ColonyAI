"""Per-goal external-condition producer: a goal blocked with a condition is
polled by the autonomy loop at the condition's cadence and auto-unblocks when
the condition is met. Pins the previously-missing producer path shut."""

import time

import pytest

import colony_sidecar.autonomy.condition_worker as cw
from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.goals.config import GoalEngineConfig
from colony_sidecar.goals.engine import GoalEngine
from colony_sidecar.goals.models import GoalStatus


def _blocked_goal(engine, condition_type=None, condition_params=None):
    g = engine.propose_goal(title="await vendor api", description="x")
    engine.accept_goal(g.goal_id)
    engine.activate_goal(g.goal_id)
    return engine.block_goal(g.goal_id, reason="waiting on vendor",
                             condition_type=condition_type,
                             condition_params=condition_params)


@pytest.fixture
def engine(tmp_path):
    return GoalEngine(config=GoalEngineConfig(
        db_path=str(tmp_path / "goals.db"), inference_enabled=False))


def _fake_loop_self(engine):
    registry = type("R", (), {"goals": engine})()
    return type("S", (), {"_registry": registry, "_periodic_last": {}})()


def test_block_with_condition_stores_and_unblock_clears(engine):
    g = _blocked_goal(engine, "api_response", {"url": "http://x/health"})
    assert g.status == GoalStatus.BLOCKED
    assert g.context["condition_type"] == "api_response"
    assert g.context["condition_params"] == {"url": "http://x/health"}
    g2 = engine.unblock_goal(g.goal_id)
    assert g2.status == GoalStatus.ACTIVE
    for k in ("block_reason", "condition_type", "condition_params",
              "condition_last_check"):
        assert k not in g2.context


async def test_sweep_unblocks_when_condition_met(engine, monkeypatch):
    calls = {"n": 0}

    async def met(params):
        calls["n"] += 1
        return {"condition_met": True}

    monkeypatch.setattr(cw, "_check_api_response", met)
    g = _blocked_goal(engine, "api_response", {"url": "http://x"})
    await AutonomyLoop._poll_blocked_goal_conditions(_fake_loop_self(engine))
    assert calls["n"] == 1
    fresh = engine._store.get_goal(g.goal_id)
    assert fresh.status == GoalStatus.ACTIVE
    assert "condition_type" not in fresh.context


async def test_sweep_not_met_persists_cadence(engine, monkeypatch):
    calls = {"n": 0}

    async def not_met(params):
        calls["n"] += 1
        return {"condition_met": False}

    monkeypatch.setattr(cw, "_check_api_response", not_met)
    g = _blocked_goal(engine, "api_response", {})
    fake = _fake_loop_self(engine)

    await AutonomyLoop._poll_blocked_goal_conditions(fake)
    assert calls["n"] == 1
    fresh = engine._store.get_goal(g.goal_id)
    assert fresh.status == GoalStatus.BLOCKED
    assert float(fresh.context["condition_last_check"]) > 0

    # within the api_response cadence (60s): no second poll
    await AutonomyLoop._poll_blocked_goal_conditions(fake)
    assert calls["n"] == 1

    # cadence elapsed: polled again
    fresh.context["condition_last_check"] = time.time() - 3600
    engine._store.save_goal(fresh)
    await AutonomyLoop._poll_blocked_goal_conditions(fake)
    assert calls["n"] == 2


async def test_goal_blocked_without_condition_is_left_alone(engine, monkeypatch):
    calls = {"n": 0}

    async def boom(params):
        calls["n"] += 1
        return {"condition_met": True}

    for name in ("_check_api_response", "_check_email_reply",
                 "_check_deployment_health", "_check_delivery_status"):
        monkeypatch.setattr(cw, name, boom)
    g = _blocked_goal(engine)                     # human-blocked, no condition
    await AutonomyLoop._poll_blocked_goal_conditions(_fake_loop_self(engine))
    assert calls["n"] == 0
    assert engine._store.get_goal(g.goal_id).status == GoalStatus.BLOCKED
