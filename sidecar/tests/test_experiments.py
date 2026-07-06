"""Experiment framework (Mind M0b): lifecycle, guards, honest refusals."""

import time
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import colony_sidecar.api.routers.host as host_mod
from colony_sidecar.self_model.benchmark import BenchmarkStore, SelfhoodBenchmark
from colony_sidecar.self_model.experiments import (
    ExperimentEngine, ExperimentStore,
)
from colony_sidecar.self_model.params import AdaptiveParamStore


def make_engine(tmp_path, *, rollups=None):
    params = AdaptiveParamStore(db_path=str(tmp_path / "params.db"))
    params.register("recall.min_relevance", 0.1, 0.0, 0.5, "test knob")
    bstore = BenchmarkStore(db_path=str(tmp_path / "bench.db"))
    for wk, val in (rollups or {"2026-W26": 0.8}).items():
        bstore.write_rollup(wk, "actions.success", val)
    bench = SelfhoodBenchmark(bstore)
    store = ExperimentStore(db_path=str(tmp_path / "exp.db"))
    return ExperimentEngine(store, params=params, benchmark=bench,
                            journal=None), params, bstore


def start_default(engine, **kw):
    args = dict(hypothesis="looser recall floor improves outcomes",
                ref="recall.min_relevance", variant=0.3,
                metric="actions.success", max_regression=0.05,
                window_days=7)
    args.update(kw)
    return engine.propose_and_start(**args)


def test_start_applies_and_baselines(tmp_path):
    engine, params, _ = make_engine(tmp_path)
    exp = start_default(engine)
    assert exp["status"] == "running"
    assert exp["baseline_param"] == pytest.approx(0.1)
    assert exp["baseline_metric"] == pytest.approx(0.8)
    assert exp["baseline_week"] == "2026-W26"
    assert params.get("recall.min_relevance") == pytest.approx(0.3)


def test_refusals(tmp_path):
    engine, params, _ = make_engine(tmp_path)
    with pytest.raises(ValueError, match="unknown adaptive param"):
        start_default(engine, ref="not.a.param")
    with pytest.raises(ValueError, match="param only"):
        start_default(engine, kind="prompt")
    with pytest.raises(ValueError, match="no rollup exists"):
        start_default(engine, metric="never.computed")
    start_default(engine)
    with pytest.raises(ValueError, match="already open"):
        start_default(engine, variant=0.2)


def test_no_decision_without_new_week(tmp_path):
    engine, params, _ = make_engine(tmp_path)
    exp = start_default(engine)
    engine.store.update(exp["id"], ends_at=time.time() - 1)
    assert engine.evaluate() == []  # only the baseline week exists
    assert params.get("recall.min_relevance") == pytest.approx(0.3)


def test_adopt_within_guard(tmp_path):
    engine, params, bstore = make_engine(tmp_path)
    exp = start_default(engine)
    engine.store.update(exp["id"], ends_at=time.time() - 1)
    bstore.write_rollup("2026-W27", "actions.success", 0.82)
    decided = engine.evaluate()
    assert decided and decided[0]["status"] == "adopted"
    assert params.get("recall.min_relevance") == pytest.approx(0.3)


def test_revert_on_regression(tmp_path):
    engine, params, bstore = make_engine(tmp_path)
    exp = start_default(engine)
    engine.store.update(exp["id"], ends_at=time.time() - 1)
    bstore.write_rollup("2026-W27", "actions.success", 0.6)
    decided = engine.evaluate()
    assert decided and decided[0]["status"] == "reverted"
    assert params.get("recall.min_relevance") == pytest.approx(0.1)
    assert "regression" in decided[0]["decision_reason"]


def test_latency_metric_direction(tmp_path):
    """For latency.* lower is better: a DROP must adopt, a RISE reverts."""
    engine, params, bstore = make_engine(
        tmp_path, rollups={"2026-W26": 10.0})
    bstore.write_rollup("2026-W26", "latency.jobs_p50_secs", 10.0)
    exp = start_default(engine, metric="latency.jobs_p50_secs",
                        max_regression=1.0)
    engine.store.update(exp["id"], ends_at=time.time() - 1)
    bstore.write_rollup("2026-W27", "latency.jobs_p50_secs", 5.0)
    decided = engine.evaluate()
    assert decided and decided[0]["status"] == "adopted"


def test_superseded_abort(tmp_path):
    engine, params, _ = make_engine(tmp_path)
    exp = start_default(engine)
    params.set("recall.min_relevance", 0.45, reason="manual", source="owner")
    decided = engine.evaluate()
    assert decided and decided[0]["status"] == "aborted"
    assert "superseded" in decided[0]["decision_reason"]
    # never re-applies its variant
    assert params.get("recall.min_relevance") == pytest.approx(0.45)


def test_manual_abort_restores(tmp_path):
    engine, params, _ = make_engine(tmp_path)
    exp = start_default(engine)
    assert engine.abort(exp["id"], reason="owner said stop")
    assert params.get("recall.min_relevance") == pytest.approx(0.1)
    assert not engine.abort(exp["id"])  # not running anymore


# --- API -------------------------------------------------------------------

@asynccontextmanager
async def _client(engine):
    orig = host_mod._experiments
    host_mod._experiments = engine
    app = FastAPI()
    app.include_router(host_mod.router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            yield c
    finally:
        host_mod._experiments = orig


async def test_api_lifecycle(tmp_path):
    engine, params, _ = make_engine(tmp_path)
    async with _client(engine) as c:
        r = await c.post("/v1/host/self/experiments", json={
            "hypothesis": "h", "ref": "recall.min_relevance",
            "variant": 0.25, "metric": "actions.success"})
        assert r.status_code == 200
        exp_id = r.json()["experiment"]["id"]

        r = await c.get("/v1/host/self/experiments")
        assert len(r.json()["running"]) == 1

        r = await c.post("/v1/host/self/experiments", json={
            "hypothesis": "h2", "ref": "recall.min_relevance",
            "variant": 0.2, "metric": "actions.success"})
        assert r.status_code == 400
        assert "already open" in r.json()["detail"]

        r = await c.post(f"/v1/host/self/experiments/{exp_id}/abort")
        assert r.status_code == 200
        assert params.get("recall.min_relevance") == pytest.approx(0.1)

        r = await c.post("/v1/host/self/experiments/nope/abort")
        assert r.status_code == 404
