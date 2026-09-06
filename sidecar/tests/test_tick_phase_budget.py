"""Tick budget attribution and periodic-phase livelock.

Locks the three pieces that keep one slow phase from eating every tick:

- the effective-confidence refresh writes one UNWIND statement per batch
  instead of one statement per memory, and pages in a stable order;
- a periodic phase cancelled by the tick budget counts as attempted for its
  period (so it costs one tick per period, not every tick until midnight);
- every tick phase is timed and named, the budget-cancel path records which
  phase was running, and the loop status exposes the timings.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pytest

from colony_sidecar.autonomy.loop import AutonomyLoop, LoopStats
from colony_sidecar.autonomy.config import AutonomyConfig
from colony_sidecar.intelligence.graph import client as client_mod


# --- one write per batch --------------------------------------------------------

class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._rows:
            raise StopAsyncIteration
        return {"mem": self._rows.pop(0)}


class _Session:
    def __init__(self, owner):
        self._owner = owner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def run(self, cypher, **params):
        self._owner.queries.append((cypher, params))
        if "RETURN m {" in cypher:
            page = self._owner.memories[params["offset"]:params["offset"] + params["limit"]]
            return _Rows(page)
        return _Rows([])


class _Driver:
    def __init__(self, owner):
        self._owner = owner

    def session(self, database=None):
        return _Session(self._owner)


class _ConfidenceFixture:
    def __init__(self, count):
        now = datetime.now(timezone.utc)
        self.memories = [
            {"id": f"m{i:05d}", "base_confidence": 0.8, "source_reliability": 0.6,
             "corroboration_count": i % 3, "contradiction_count": 0, "recalls": i % 5,
             "last_verified_at": None, "created_at": now, "epistemic_state": "inferred"}
            for i in range(count)
        ]
        self.queries = []
        g = client_mod.ColonyGraph.__new__(client_mod.ColonyGraph)
        g.driver = _Driver(self)
        g.database = "neo4j"
        self.graph = g

    @property
    def reads(self):
        return [q for q in self.queries if "RETURN m {" in q[0]]

    @property
    def writes(self):
        return [q for q in self.queries if "SET m.effective_confidence" in q[0]]


async def test_confidence_refresh_writes_once_per_batch():
    fx = _ConfidenceFixture(2500)
    await fx.graph._update_effective_confidence_batch(batch_size=1000)

    # 3 pages (1000, 1000, 500); the short last page ends the loop without an
    # extra empty read.
    assert len(fx.reads) == 3
    assert len(fx.writes) == 3
    assert all("UNWIND $updates" in cypher for cypher, _ in fx.writes)
    written = [u["id"] for _, params in fx.writes for u in params["updates"]]
    assert written == [m["id"] for m in fx.memories]
    for _, params in fx.writes:
        for u in params["updates"]:
            assert 0.0 <= u["effective_confidence"] <= 1.0


async def test_confidence_refresh_pages_in_a_stable_order():
    fx = _ConfidenceFixture(3)
    await fx.graph._update_effective_confidence_batch(batch_size=1000)
    cypher, params = fx.reads[0]
    assert "ORDER BY m.id" in cypher
    assert params == {"offset": 0, "limit": 1000}


async def test_confidence_refresh_exact_page_boundary():
    fx = _ConfidenceFixture(1000)
    await fx.graph._update_effective_confidence_batch(batch_size=1000)
    # a full page cannot know it was the last one: one more (empty) read, no
    # empty write
    assert len(fx.reads) == 2
    assert len(fx.writes) == 1
    assert len(fx.writes[0][1]["updates"]) == 1000


async def test_confidence_refresh_skips_rows_without_id():
    fx = _ConfidenceFixture(2)
    fx.memories[0]["id"] = None
    await fx.graph._update_effective_confidence_batch(batch_size=1000)
    assert [u["id"] for u in fx.writes[0][1]["updates"]] == ["m00001"]


async def test_confidence_refresh_empty_graph_writes_nothing():
    fx = _ConfidenceFixture(0)
    await fx.graph._update_effective_confidence_batch(batch_size=1000)
    assert len(fx.reads) == 1
    assert fx.writes == []


# --- cancelled periodic phase counts as attempted --------------------------------

class _Reg:
    def __init__(self, graph):
        self.graph = graph


def _bare_loop(graph=object()):
    loop = AutonomyLoop.__new__(AutonomyLoop)
    loop.config = AutonomyConfig()
    loop._registry = _Reg(graph)
    loop._periodic_last = {}
    loop._phase_skip_warned = set()
    loop._current_phase = None
    loop._phase_seconds = {}
    loop._phase_seconds_max = {}
    loop.stats = LoopStats()
    return loop


async def test_cancelled_periodic_phase_counts_as_attempted(caplog):
    loop = _bare_loop()
    started = asyncio.Event()
    calls = []

    async def slow(graph):
        calls.append(1)
        started.set()
        await asyncio.Event().wait()

    with caplog.at_level(logging.ERROR):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                loop._run_periodic_phase("memory_decay", "day", slow), timeout=0.05)
    assert started.is_set()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert loop._periodic_last["memory_decay"] == today
    assert any("memory_decay" in r.getMessage() and "cancelled" in r.getMessage()
               for r in caplog.records)

    # The next tick does not run it again today.
    await loop._run_periodic_phase("memory_decay", "day", slow)
    assert calls == [1]


async def test_periodic_phase_success_and_error_paths_unchanged():
    loop = _bare_loop()
    ran = []

    async def ok(graph):
        ran.append("ok")

    async def boom(graph):
        raise RuntimeError("boom")

    await loop._run_periodic_phase("a", "day", ok)
    assert ran == ["ok"]
    assert "a" in loop._periodic_last
    await loop._run_periodic_phase("b", "day", boom)
    assert loop.stats.errors == 1
    assert "b" not in loop._periodic_last  # a failed phase retries next tick


# --- phase timing and cancel attribution ---------------------------------------

class _NoneRegistry:
    def __getattr__(self, name):
        return None


async def test_run_phase_records_last_and_max_seconds():
    loop = _bare_loop()

    async def quick():
        await asyncio.sleep(0)

    async def slower():
        await asyncio.sleep(0.02)

    await loop._run_phase("p", quick())
    first = loop._phase_seconds["p"]
    await loop._run_phase("p", slower())
    assert loop._phase_seconds["p"] >= 0.02
    assert loop._phase_seconds_max["p"] == max(first, loop._phase_seconds["p"])
    assert loop.phase_timings()["seconds_max"]["p"] == round(loop._phase_seconds_max["p"], 3)


async def test_budget_cancel_names_the_running_phase(caplog):
    loop = AutonomyLoop(_NoneRegistry())
    started = asyncio.Event()

    async def stuck():
        started.set()
        await asyncio.Event().wait()

    loop._phase_memory_decay = stuck
    # generous timeout: the cancel must land in stuck(), the first real
    # suspension point, not in a slow synchronous phase before it
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(loop._tick(), timeout=2.0)
    assert started.is_set()
    assert loop._current_phase == "memory_decay"
    # phases before the stuck one completed and were timed
    assert "events" in loop._phase_seconds
    assert "memory_decay" in loop._phase_seconds

    with caplog.at_level(logging.ERROR):
        loop._note_tick_cancelled()
    assert loop.stats.errors == 1
    assert loop.stats.phases_cancelled == 1
    assert loop.stats.last_cancelled_phase == "memory_decay"
    assert loop._current_phase is None
    line = [r.getMessage() for r in caplog.records if "exceeded budget" in r.getMessage()]
    assert line and "in phase memory_decay" in line[0]

    status = loop.status()
    assert status["stats"]["phases_cancelled"] == 1
    assert status["stats"]["last_cancelled_phase"] == "memory_decay"
    assert status["phases"]["current"] is None
    assert "memory_decay" in status["phases"]["seconds_max"]


async def test_completed_tick_clears_the_phase_marker():
    loop = AutonomyLoop(_NoneRegistry())
    await loop._tick()
    assert loop._current_phase is None
    assert loop.stats.phases_cancelled == 0
    timings = loop.phase_timings()
    assert timings["current"] is None
    assert "memory_decay" in timings["seconds_last"]
    assert "agent_heartbeat" in timings["seconds_last"]
    # the closing telemetry stamp is a timed phase too, so a cancel landing
    # there is not blamed on whichever phase ran before it
    assert "telemetry" in timings["seconds_last"]


async def test_crashed_phase_does_not_leave_the_marker_set():
    loop = AutonomyLoop(_NoneRegistry())

    async def boom():
        raise RuntimeError("boom")

    loop._phase_memory_decay = boom
    with pytest.raises(RuntimeError):
        await loop._tick()
    # the manual tick endpoint swallows this exception; status must not keep
    # reporting the crashed phase as running
    assert loop._current_phase is None
    assert loop.phase_timings()["current"] is None


# --- inline periodic phases use the same marker --------------------------------

class _Engine:
    def __init__(self, hang):
        self.hang = hang
        self.calls = 0
        self.started = asyncio.Event()

    async def run(self):
        self.calls += 1
        self.started.set()
        if self.hang:
            await asyncio.Event().wait()


async def test_inline_daily_phase_cancelled_counts_as_attempted():
    loop = _bare_loop()
    engine = _Engine(hang=True)
    loop._registry.belief_engine = engine
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(loop._phase_belief_maintenance(), timeout=0.05)
    assert engine.started.is_set()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert loop._periodic_last["belief_maintenance"] == today

    engine.hang = False
    await loop._phase_belief_maintenance()
    assert engine.calls == 1  # not re-run today


async def test_inline_daily_phase_error_still_retries():
    loop = _bare_loop()

    class _Boom:
        async def run(self):
            raise RuntimeError("boom")

    loop._registry.belief_engine = _Boom()
    await loop._phase_belief_maintenance()
    assert loop.stats.errors == 1
    assert "belief_maintenance" not in loop._periodic_last


async def test_selfhood_benchmark_timeout_marks_the_week():
    loop = _bare_loop()
    loop._phase_budget_secs = lambda: 0.02

    class _Bench:
        def __init__(self):
            self.calls = 0

        async def compute_week(self):
            self.calls += 1
            await asyncio.Event().wait()

    bench = _Bench()
    loop._registry.benchmark = bench
    await loop._phase_selfhood_benchmark()
    assert bench.calls == 1
    week = datetime.now(timezone.utc).strftime("%Y-W%W")
    assert loop._periodic_last["selfhood_benchmark"] == week
    await loop._phase_selfhood_benchmark()
    assert bench.calls == 1  # one stall costs one phase budget per week


async def test_self_reflection_cancelled_counts_as_attempted():
    loop = _bare_loop()
    loop.config = type("Cfg", (), {"self_reflection_interval_days": 7})()
    started = asyncio.Event()
    calls = []

    class _Cog:
        async def self_reflect(self):
            calls.append(1)
            started.set()
            await asyncio.Event().wait()

    loop._registry.cognition = _Cog()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(loop._phase_self_reflection(), timeout=0.05)
    assert started.is_set()
    assert isinstance(loop._periodic_last["self_reflection"], datetime)
    await loop._phase_self_reflection()
    assert calls == [1]


def test_stats_dict_exposes_cancellations():
    stats = LoopStats()
    d = stats.as_dict()
    assert d["phases_cancelled"] == 0
    assert d["last_cancelled_phase"] is None
