"""Type-aware decay + single-writer discipline (U10).

Locks:
- default half-lives (env unset) produce byte-identical decay math to the
  historical hardcoded 7-day value, for every memory type;
- fact/semantic memories get their own half-life ONLY when
  COLONY_DECAY_HALF_LIFE_SEMANTIC_DAYS is set (defaults to episodic);
- the Cypher pass and _compute_decay_factor use the same lambdas;
- StrategyAdjuster._decay_signals is retired and never touches the graph.
"""

from __future__ import annotations

import math

import pytest

from colony_sidecar.intelligence.graph import client as client_mod


# --- _compute_decay_factor unit math -------------------------------------------

def _expected(importance, days, recalls, half_life):
    lam = math.log(2) / max(half_life, 0.001)
    return min(1.0, max(0.0, importance * math.exp(-lam * days) * (1 + recalls * 0.2)))


class TestComputeDecayFactor:
    def test_episodic_default_formula(self):
        got = client_mod.ColonyGraph._compute_decay_factor(
            importance=0.8, days_elapsed=7, recalls=0,
            half_life_days=7.0, memory_type="episodic")
        assert got == pytest.approx(_expected(0.8, 7, 0, 7.0))
        assert got == pytest.approx(0.4)  # exactly one half-life

    def test_identity_never_decays(self):
        got = client_mod.ColonyGraph._compute_decay_factor(
            importance=0.9, days_elapsed=1000, recalls=0,
            half_life_days=7.0, memory_type="identity")
        assert got == 0.9

    def test_procedural_half_rate(self):
        got = client_mod.ColonyGraph._compute_decay_factor(
            importance=1.0, days_elapsed=14, recalls=0,
            half_life_days=7.0, memory_type="procedural")
        # lambda/2 == a doubled half-life
        assert got == pytest.approx(_expected(1.0, 14, 0, 14.0))

    @pytest.mark.parametrize("mtype", ["fact", "semantic"])
    def test_semantic_defaults_to_episodic_half_life(self, mtype):
        """Regression lock: without a semantic half-life the math is
        byte-identical to the pre-U10 behavior (same lambda as episodic)."""
        got = client_mod.ColonyGraph._compute_decay_factor(
            importance=0.6, days_elapsed=10, recalls=2,
            half_life_days=7.0, memory_type=mtype)
        assert got == pytest.approx(_expected(0.6, 10, 2, 7.0))

    @pytest.mark.parametrize("mtype", ["fact", "semantic"])
    def test_semantic_half_life_overrides(self, mtype):
        got = client_mod.ColonyGraph._compute_decay_factor(
            importance=0.6, days_elapsed=10, recalls=0,
            half_life_days=7.0, memory_type=mtype,
            semantic_half_life_days=30.0)
        assert got == pytest.approx(_expected(0.6, 10, 0, 30.0))

    def test_semantic_half_life_does_not_touch_episodic(self):
        got = client_mod.ColonyGraph._compute_decay_factor(
            importance=0.6, days_elapsed=10, recalls=0,
            half_life_days=7.0, memory_type="episodic",
            semantic_half_life_days=30.0)
        assert got == pytest.approx(_expected(0.6, 10, 0, 7.0))


# --- decay_memories Cypher parameters -------------------------------------------

class _FakeResult:
    async def single(self):
        return None


class _FakeSession:
    def __init__(self, owner):
        self._owner = owner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def run(self, cypher, **params):
        self._owner.queries.append((cypher, params))
        return _FakeResult()


class _FakeDriver:
    def __init__(self, owner):
        self._owner = owner

    def session(self, database=None):
        return _FakeSession(self._owner)


class _DecayFixture:
    def __init__(self):
        self.queries = []
        g = client_mod.ColonyGraph.__new__(client_mod.ColonyGraph)
        g.driver = _FakeDriver(self)
        g.database = "neo4j"

        async def _noop_batch(batch_size=1000):
            pass

        g._update_effective_confidence_batch = _noop_batch
        self.graph = g

    @property
    def decay_params(self):
        cypher, params = self.queries[0]
        assert "SET m.strength" in cypher
        return cypher, params


@pytest.mark.asyncio
async def test_default_lambdas_match_legacy_seven_days(monkeypatch):
    """Regression lock: env unset -> all three lambdas derive from the
    historical 7-day half-life, and semantic == normal (no type divergence)."""
    monkeypatch.delenv("COLONY_DECAY_HALF_LIFE_DAYS", raising=False)
    monkeypatch.delenv("COLONY_DECAY_HALF_LIFE_SEMANTIC_DAYS", raising=False)
    fx = _DecayFixture()
    await fx.graph.decay_memories()
    cypher, params = fx.decay_params
    lam7 = math.log(2) / 7.0
    assert params["lambda_norm"] == pytest.approx(lam7)
    assert params["lambda_proc"] == pytest.approx(lam7 / 2)
    assert params["lambda_sem"] == pytest.approx(lam7)
    # type-aware branch exists in the query and uses the semantic lambda
    assert "IN ['fact', 'semantic']" in cypher


@pytest.mark.asyncio
async def test_env_half_lives_respected(monkeypatch):
    monkeypatch.setenv("COLONY_DECAY_HALF_LIFE_DAYS", "14")
    monkeypatch.setenv("COLONY_DECAY_HALF_LIFE_SEMANTIC_DAYS", "60")
    fx = _DecayFixture()
    await fx.graph.decay_memories()
    _, params = fx.decay_params
    assert params["lambda_norm"] == pytest.approx(math.log(2) / 14.0)
    assert params["lambda_sem"] == pytest.approx(math.log(2) / 60.0)


@pytest.mark.asyncio
async def test_semantic_env_defaults_to_episodic_env(monkeypatch):
    monkeypatch.setenv("COLONY_DECAY_HALF_LIFE_DAYS", "21")
    monkeypatch.delenv("COLONY_DECAY_HALF_LIFE_SEMANTIC_DAYS", raising=False)
    fx = _DecayFixture()
    await fx.graph.decay_memories()
    _, params = fx.decay_params
    assert params["lambda_sem"] == pytest.approx(math.log(2) / 21.0)


@pytest.mark.asyncio
async def test_explicit_args_beat_env(monkeypatch):
    monkeypatch.setenv("COLONY_DECAY_HALF_LIFE_DAYS", "14")
    monkeypatch.setenv("COLONY_DECAY_HALF_LIFE_SEMANTIC_DAYS", "60")
    fx = _DecayFixture()
    await fx.graph.decay_memories(half_life_days=3.0,
                                  semantic_half_life_days=9.0)
    _, params = fx.decay_params
    assert params["lambda_norm"] == pytest.approx(math.log(2) / 3.0)
    assert params["lambda_sem"] == pytest.approx(math.log(2) / 9.0)


@pytest.mark.asyncio
async def test_cypher_and_unit_math_agree(monkeypatch):
    """The Cypher lambda for a fact memory equals what
    _compute_decay_factor uses for the same configuration."""
    monkeypatch.delenv("COLONY_DECAY_HALF_LIFE_DAYS", raising=False)
    monkeypatch.setenv("COLONY_DECAY_HALF_LIFE_SEMANTIC_DAYS", "30")
    fx = _DecayFixture()
    await fx.graph.decay_memories()
    _, params = fx.decay_params
    unit = client_mod.ColonyGraph._compute_decay_factor(
        importance=1.0, days_elapsed=10, recalls=0,
        half_life_days=7.0, memory_type="fact",
        semantic_half_life_days=30.0)
    assert unit == pytest.approx(math.exp(-params["lambda_sem"] * 10))


# --- StrategyAdjuster._decay_signals retired -------------------------------------

class _ExplodingGraph:
    async def decay_memories(self, *a, **k):
        raise AssertionError("retired action must never touch the graph")


@pytest.mark.asyncio
async def test_decay_signals_retired_refuses():
    from colony_sidecar.intelligence.cognition.strategy_adjuster import (
        StrategyAdjuster,
    )
    adj = StrategyAdjuster(graph=_ExplodingGraph())
    out = await adj._decay_signals(factor=0.5)
    assert out["success"] is False
    assert "retired" in out["error"]


@pytest.mark.asyncio
async def test_decay_old_signals_action_refuses_via_dispatch():
    from colony_sidecar.intelligence.cognition.strategy_adjuster import (
        StrategyAdjuster,
    )
    adj = StrategyAdjuster(graph=_ExplodingGraph())
    out = await adj._execute_action(
        {"type": "decay_old_signals", "params": {"factor": 0.5}})
    assert out["success"] is False
