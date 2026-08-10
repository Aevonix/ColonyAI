"""Critical WorkOrder result projection runs before slow cognition phases."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from colony_sidecar.autonomy.loop import AutonomyLoop


@pytest.mark.asyncio
async def test_terminal_reconciliation_precedes_a_slow_llm_phase():
    calls = []

    class Engine:
        async def reconcile_terminal_results(self, *, limit):
            calls.append(("reconciled", limit))
            return {
                "checked": 1,
                "terminal": 1,
                "projected": 1,
                "errors": 0,
            }

    loop = AutonomyLoop(SimpleNamespace(project_engine=Engine()))
    slow_started = asyncio.Event()
    never = asyncio.Event()

    async def slow_skill_phase(_event_text):
        calls.append(("slow_llm", None))
        slow_started.set()
        await never.wait()

    loop._phase_skill_triggers = slow_skill_phase
    tick = asyncio.create_task(loop._tick())
    await asyncio.wait_for(slow_started.wait(), timeout=0.5)
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick

    assert calls == [("reconciled", 25), ("slow_llm", None)]


@pytest.mark.asyncio
async def test_terminal_reconciliation_has_its_own_wall_budget(monkeypatch):
    started = asyncio.Event()
    never = asyncio.Event()

    class Engine:
        async def reconcile_terminal_results(self, *, limit):
            started.set()
            await never.wait()

    monkeypatch.setenv("COLONY_PROJECT_RECONCILIATION_BUDGET_SECS", "0.05")
    loop = AutonomyLoop(SimpleNamespace(project_engine=Engine()))

    await asyncio.wait_for(
        loop._phase_project_result_reconciliation(), timeout=0.5,
    )

    assert started.is_set()
    assert loop.stats.errors == 1
