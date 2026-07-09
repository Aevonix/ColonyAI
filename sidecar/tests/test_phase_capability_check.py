"""Dead-phase hygiene (U2): the graph-dispatched consolidation phase is
deleted (the consolidator runs via the memory_consolidate scheduler task),
and phases that dispatch on missing graph capabilities are surfaced at boot
(warn once) and counted in stats.phases_skipped instead of no-oping silently.
"""

from __future__ import annotations

import logging

from colony_sidecar.autonomy.loop import AutonomyLoop, LoopStats


class _Reg:
    def __init__(self, graph):
        self.graph = graph


def _bare_loop(graph):
    loop = AutonomyLoop.__new__(AutonomyLoop)
    loop._registry = _Reg(graph)
    loop._periodic_last = {}
    loop._phase_skip_warned = set()
    loop.stats = LoopStats()
    return loop


class _CapableGraph:
    async def decay_memories(self):
        pass

    async def prune_weak_memories(self, **kw):
        return {"matched": 0, "deleted": 0, "dry_run": True, "ids": []}

    async def archive_memories(self, max_age_days=30):
        return 0


class _EmptyGraph:
    pass


def test_consolidation_phase_deleted():
    assert not hasattr(AutonomyLoop, "_phase_memory_consolidation")


def test_boot_check_passes_on_capable_graph(caplog):
    loop = _bare_loop(_CapableGraph())
    with caplog.at_level(logging.WARNING):
        loop._check_phase_capabilities()
    assert loop.stats.phases_skipped == 0
    assert not caplog.records


def test_boot_check_warns_on_missing_capabilities(caplog):
    loop = _bare_loop(_EmptyGraph())
    with caplog.at_level(logging.WARNING):
        loop._check_phase_capabilities()
    warned = " ".join(r.getMessage() for r in caplog.records)
    for attr in ("decay_memories", "prune_weak_memories", "archive_memories"):
        assert attr in warned
    # boot check only warns; skips are counted when the phase actually runs
    assert loop.stats.phases_skipped == 0


def test_boot_check_noop_without_graph(caplog):
    loop = _bare_loop(None)
    with caplog.at_level(logging.WARNING):
        loop._check_phase_capabilities()
    assert not caplog.records


async def test_skips_counted_every_time_warned_once(caplog):
    loop = _bare_loop(_EmptyGraph())
    with caplog.at_level(logging.WARNING):
        await loop._phase_memory_decay()
        loop._periodic_last.clear()  # force the periodic gate open again
        await loop._phase_memory_decay()
    assert loop.stats.phases_skipped == 2
    decay_warnings = [r for r in caplog.records
                      if "memory_decay" in r.getMessage()]
    assert len(decay_warnings) == 1


async def test_archive_skip_counted(monkeypatch):
    loop = _bare_loop(_EmptyGraph())
    await loop._phase_memory_archive()
    assert loop.stats.phases_skipped == 1


def test_stats_dict_exposes_phases_skipped():
    stats = LoopStats()
    assert stats.as_dict()["phases_skipped"] == 0
