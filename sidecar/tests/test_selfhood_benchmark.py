"""Selfhood benchmark (Mind M0a): store, derivations, honest skips, API."""

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import colony_sidecar.api.routers.host as host_mod
from colony_sidecar.self_model.benchmark import (
    BenchmarkStore, SelfhoodBenchmark, previous_week, week_window,
)

WEEK = "2026-W26"
START, END = week_window(WEEK)
T0 = START.timestamp()


# --- fakes -----------------------------------------------------------------

class FakeCommitments:
    def list(self, status=None, limit=50, **kw):
        inside = (START + timedelta(days=1)).isoformat()
        outside = (START - timedelta(days=2)).isoformat()
        return {"commitments": [
            {"fulfilled_at": inside}, {"fulfilled_at": inside},
            {"fulfilled_at": outside},
        ]}

    def get_overdue(self):
        return [{"id": "c1"}]


class FakeCompetence:
    def snapshot(self):
        return [{"domain": "worker:research"}, {"domain": "delivery"}]

    def events(self, domain, since=None, include_shadow=True):
        if domain == "delivery":
            return [
                {"ts": T0 + 3600, "outcome": "success"},
                {"ts": T0 + 7200, "outcome": "success"},
                {"ts": T0 + 9000, "outcome": "success"},
                {"ts": T0 + 10800, "outcome": "failure"},
            ]
        return [
            {"ts": T0 + 3600, "outcome": "success"},
            {"ts": T0 + 7200, "outcome": "success"},
        ]


class FakeJournal:
    def recent(self, limit=50, domain=None, since=None):
        return ([{"ts": T0 + 100, "decision": "acted"}] * 3
                + [{"ts": T0 + 200, "decision": "asked"}]
                + [{"ts": T0 + 300, "decision": "noted"}] * 2)


class FakeComms:
    def inbound_since(self, contact_id, since_iso):
        assert contact_id == "cid-owner"
        # responds after the first two deliveries only
        from datetime import datetime, timezone
        return [
            datetime.fromtimestamp(T0 + 4000, tz=timezone.utc).isoformat(),
            datetime.fromtimestamp(T0 + 7500, tz=timezone.utc).isoformat(),
        ]


class FakeFacts:
    def list_facts(self, min_confidence=0.0, limit=100, **kw):
        return {"facts": [
            {"id": "f1", "fact": "the reranker service runs on port 8093"},
            {"id": "f2", "fact": "quarterly report deadline moved to friday"},
        ]}


class FakeGraph:
    async def recall(self, query, limit=10, min_strength=0.1,
                     min_confidence=0.1):
        if "reranker" in query:
            return [{"content": "the reranker service runs on port 8093 "
                                "behind the tunnel"}]
        return [{"content": "something entirely unrelated"}]


class FakeQueue:
    async def completed_durations(self, since_iso, until_iso, limit=1000):
        assert since_iso[:19] <= until_iso[:19]
        return [1.0, 2.0, 3.0, 100.0]


def make_bench(tmp_path, **overrides):
    store = BenchmarkStore(db_path=str(tmp_path / "bench.db"))
    deps = dict(
        commitments=FakeCommitments(), competence=FakeCompetence(),
        journal=FakeJournal(), comms=FakeComms(), graph=FakeGraph(),
        facts=FakeFacts(), queue=FakeQueue(),
        owner_contact_id="cid-owner", probes=2,
    )
    deps.update(overrides)
    return SelfhoodBenchmark(store, **deps)


# --- store -----------------------------------------------------------------

def test_store_sample_validation(tmp_path):
    s = BenchmarkStore(db_path=str(tmp_path / "b.db"))
    assert s.add_sample("latency.voice_ttfb_ms", 42.0)
    assert not s.add_sample("BadMetric", 1.0)
    assert not s.add_sample("noprefix", 1.0)
    assert not s.add_sample("latency.x", "nan-ish")  # type: ignore[arg-type]


def test_store_rollup_roundtrip(tmp_path):
    s = BenchmarkStore(db_path=str(tmp_path / "b.db"))
    s.write_rollup("2026-W25", "actions.success", 0.8, numerator=8,
                   denominator=10, detail={"domains": {}})
    s.write_rollup("2026-W26", "actions.success", 0.9, numerator=9,
                   denominator=10)
    rolls = s.rollups(weeks=8)
    assert list(rolls.keys()) == ["2026-W26", "2026-W25"]
    assert rolls["2026-W26"]["actions.success"]["value"] == 0.9


def test_week_helpers():
    start, end = week_window("2026-W26")
    assert start.isoweekday() == 1
    assert (end - start).days == 7
    assert previous_week(start) != "2026-W26"


# --- derivations -----------------------------------------------------------

async def test_compute_week_full(tmp_path):
    bench = make_bench(tmp_path)
    bench.store.add_sample("latency.voice_ttfb_ms", 30, ts=T0 + 50)
    bench.store.add_sample("latency.voice_ttfb_ms", 90, ts=T0 + 60)
    bench.store.add_sample("surface.wake_accuracy", 0.5, ts=T0 + 70)

    out = (await bench.compute_week(WEEK))["metrics"]

    assert out["commitments.fulfillment"]["value"] == pytest.approx(2 / 3)
    assert out["delivery.success"]["value"] == pytest.approx(0.75)
    # actions.success excludes the delivery domain
    assert out["actions.success"]["value"] == 1.0
    assert out["actions.success"]["detail"]["domains"] == {
        "worker:research": {"success": 2, "n": 2}}
    assert out["journal.acted_share"]["value"] == pytest.approx(0.75)
    # 2 of 3 delivery successes answered within 24h
    assert out["initiative.acceptance"]["value"] == pytest.approx(2 / 3)
    # one of two fact probes covered
    assert out["recall.fact_coverage"]["value"] == pytest.approx(0.5)
    assert out["latency.jobs_p50_secs"]["detail"]["n"] == 4
    assert out["latency.voice_ttfb_ms"]["detail"]["p95"] == 90
    assert out["surface.wake_accuracy"]["value"] == pytest.approx(0.5)

    # rollups persisted; probes recorded as samples
    rolls = bench.store.rollups()
    assert WEEK in rolls and "recall.fact_coverage" in rolls[WEEK]
    import time as _time
    probes = bench.store.samples_in(0, _time.time() + 10,
                                    metric="recall.probe")
    assert len(probes) == 2


async def test_compute_week_honest_skips(tmp_path):
    """Missing sources omit metrics; nothing is zero-filled."""
    store = BenchmarkStore(db_path=str(tmp_path / "b2.db"))
    bench = SelfhoodBenchmark(store, commitments=None, competence=None,
                              journal=None, comms=None, graph=None,
                              facts=None, queue=None,
                              owner_contact_id="", probes=2)
    # Force lazy resolution to find nothing rather than the real host globals
    bench._host_attr = staticmethod(lambda name: None)  # type: ignore
    out = (await bench.compute_week(WEEK))["metrics"]
    assert out == {}


async def test_acceptance_skipped_without_owner(tmp_path):
    bench = make_bench(tmp_path, owner_contact_id="")
    bench._host_attr = staticmethod(lambda name: None)  # type: ignore
    out = (await bench.compute_week(WEEK))["metrics"]
    assert "initiative.acceptance" not in out
    assert "delivery.success" in out


def test_snapshot_trends(tmp_path):
    s = BenchmarkStore(db_path=str(tmp_path / "b3.db"))
    bench = SelfhoodBenchmark(s)
    s.write_rollup("2026-W25", "actions.success", 0.6)
    s.write_rollup("2026-W26", "actions.success", 0.9)
    snap = bench.snapshot()
    assert snap["latest"] == "2026-W26"
    assert snap["trends"]["actions.success"] == pytest.approx(0.3)


# --- API -------------------------------------------------------------------

@asynccontextmanager
async def _client(bench):
    orig = host_mod._benchmark
    host_mod._benchmark = bench
    app = FastAPI()
    app.include_router(host_mod.router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            yield c
    finally:
        host_mod._benchmark = orig


async def test_api_samples_and_snapshot(tmp_path):
    bench = make_bench(tmp_path)
    async with _client(bench) as c:
        r = await c.post("/v1/host/self/benchmark/samples", json={
            "samples": [
                {"metric": "latency.voice_ttfb_ms", "value": 33.0},
                {"metric": "NOT VALID", "value": 1.0},
            ],
            "source": "voice-gateway"})
        body = r.json()
        assert r.status_code == 200
        assert body["accepted"] == 1 and body["rejected"] == 1

        r = await c.get("/v1/host/self/benchmark")
        assert r.status_code == 200
        assert r.json()["available"] is True


async def test_api_unavailable():
    async with _client(None) as c:
        r = await c.get("/v1/host/self/benchmark")
        assert r.json() == {"available": False}
        r = await c.post("/v1/host/self/benchmark/samples",
                         json={"samples": []})
        assert r.json()["available"] is False


# --- on-demand recall probe (U0) --------------------------------------------

class ManyFakeFacts:
    """Enough facts that a seeded sample is a real subset."""

    def list_facts(self, min_confidence=0.0, limit=100, **kw):
        return {"facts": [
            {"id": f"f{i}", "fact": f"unique subject number{i} lives in "
                                    f"building{i} downtown"}
            for i in range(10)
        ]}


class HalfHitGraph:
    """Covers even-numbered facts only, and records every query."""

    def __init__(self):
        self.queries = []

    async def recall(self, query, limit=10, min_strength=0.1,
                     min_confidence=0.1):
        self.queries.append(query)
        import re
        m = re.search(r"number(\d+)", query)
        if m and int(m.group(1)) % 2 == 0:
            return [{"content": query + " extra context"}]
        return [{"content": "something entirely unrelated"}]


async def test_recall_probe_seeded_deterministic(tmp_path):
    g1, g2 = HalfHitGraph(), HalfHitGraph()
    b1 = make_bench(tmp_path, graph=g1, facts=ManyFakeFacts())
    b2 = make_bench(tmp_path, graph=g2, facts=ManyFakeFacts())
    r1 = await b1.run_recall_probe(probes=4, seed=42)
    r2 = await b2.run_recall_probe(probes=4, seed=42)
    assert r1 is not None and r2 is not None
    # Same seed -> identical fact picks and identical score
    assert g1.queries == g2.queries
    assert len(g1.queries) == 4
    assert r1["value"] == r2["value"]
    assert r1["denominator"] == 4
    assert r1["detail"]["seed"] == 42
    assert r1["detail"]["source"] == "manual-probe"


async def test_recall_probe_samples_excluded_from_rollups(tmp_path):
    bench = make_bench(tmp_path, graph=HalfHitGraph(), facts=ManyFakeFacts())
    await bench.run_recall_probe(probes=5, seed=7)
    import time as _time
    samples = bench.store.samples_in(0, _time.time() + 10,
                                     metric="recall.probe")
    assert len(samples) == 5
    assert all(s["source"] == "manual-probe" for s in samples)
    # The generic sample rollup never reads recall.probe samples, so a
    # manual probe run cannot leak into the weekly scorecard.
    submitted = bench._m_submitted(0, _time.time() + 10)
    assert "recall.probe" not in submitted


async def test_recall_probe_clamps_and_skips(tmp_path):
    bench = make_bench(tmp_path, graph=HalfHitGraph(), facts=ManyFakeFacts())
    r = await bench.run_recall_probe(probes=500, seed=1)
    assert r is not None and r["denominator"] == 10  # capped at 100, 10 facts
    # honest skip when a source is missing
    assert await make_bench(tmp_path, graph=None,
                            facts=ManyFakeFacts()).run_recall_probe() is None


async def test_weekly_recall_metric_unchanged_by_refactor(tmp_path):
    """Regression lock: the weekly recall.fact_coverage derivation still
    produces the pre-refactor result and source tag."""
    bench = make_bench(tmp_path)
    out = (await bench.compute_week(WEEK))["metrics"]
    assert out["recall.fact_coverage"]["value"] == pytest.approx(0.5)
    assert out["recall.fact_coverage"]["detail"] == {"probes": 2}
    import time as _time
    probes = bench.store.samples_in(0, _time.time() + 10,
                                    metric="recall.probe")
    assert {p["source"] for p in probes} == {"benchmark"}


async def test_api_recall_probe(tmp_path):
    bench = make_bench(tmp_path, graph=HalfHitGraph(), facts=ManyFakeFacts())
    async with _client(bench) as c:
        r = await c.post("/v1/host/self/benchmark/recall-probe",
                         json={"probes": 4, "seed": 42})
        body = r.json()
        assert r.status_code == 200
        assert body["available"] is True and body["ran"] is True
        assert body["denominator"] == 4
        assert body["detail"]["seed"] == 42
    async with _client(None) as c:
        r = await c.post("/v1/host/self/benchmark/recall-probe", json={})
        assert r.json() == {"available": False}
