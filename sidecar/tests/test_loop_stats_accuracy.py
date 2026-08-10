"""Stats truthfulness (U3): _phase_events counts each event exactly once
(no cumulative window re-count), and SubsystemRegistry.queue returns the
task-queue manager, not the memory consolidator.
"""

from __future__ import annotations

from colony_sidecar.autonomy.loop import AutonomyLoop, LoopStats
from colony_sidecar.autonomy.registry import SubsystemRegistry
from colony_sidecar.events.bus import EventBus
from colony_sidecar.events.types import Event


def _bare_loop(bus):
    loop = AutonomyLoop.__new__(AutonomyLoop)
    loop.events = bus
    loop.stats = LoopStats()
    loop._last_event_seen_id = None
    return loop


async def test_events_counted_once_across_ticks():
    bus = EventBus()
    loop = _bare_loop(bus)
    for i in range(3):
        bus.emit(Event(id=f"e{i}"))

    await loop._phase_events()
    assert loop.stats.events_processed == 3

    # No new events: an idle colony must not keep inflating the counter.
    await loop._phase_events()
    assert loop.stats.events_processed == 3

    bus.emit(Event(id="e3"))
    bus.emit(Event(id="e4"))
    await loop._phase_events()
    assert loop.stats.events_processed == 5


async def test_events_marker_aged_out_counts_window():
    """If the last-seen event fell out of the history window, the whole
    window is counted (bounded over-count, matching the old worst case)."""
    bus = EventBus(max_history=5)
    loop = _bare_loop(bus)
    bus.emit(Event(id="old"))
    await loop._phase_events()
    assert loop.stats.events_processed == 1

    for i in range(10):  # pushes "old" out of history
        bus.emit(Event(id=f"n{i}"))
    await loop._phase_events()
    assert loop.stats.events_processed == 1 + 5  # bounded by max_history


async def test_events_error_isolated():
    class _BrokenBus:
        def get_history(self, limit=100):
            raise RuntimeError("boom")

    loop = _bare_loop(_BrokenBus())
    await loop._phase_events()
    assert loop.stats.errors == 1
    assert loop.stats.events_processed == 0


async def test_tick_liveness_stamp_requires_a_completed_tick(monkeypatch):
    """last_tick_at is stamped at the END of _tick: a tick that dies in a
    phase (or is cancelled on budget) must not report fresh liveness."""
    import pytest
    import colony_sidecar.api.routers.host as host_mod
    from colony_sidecar.telemetry import TelemetryStore

    telemetry = TelemetryStore()
    monkeypatch.setattr(host_mod, "_telemetry", telemetry)

    class _NoneRegistry:
        def __getattr__(self, name):
            return None

    loop = AutonomyLoop(_NoneRegistry())

    async def boom(_event_text=None):
        raise RuntimeError("phase exploded")

    loop._phase_skill_triggers = boom
    with pytest.raises(RuntimeError):
        await loop._tick()
    assert telemetry.last_tick_at is None  # dead tick must not look alive

    # A tick that completes all phases does stamp liveness.
    async def fine(_event_text=None):
        return None

    loop._phase_skill_triggers = fine
    await loop._tick()
    assert telemetry.last_tick_at is not None


def test_registry_queue_is_task_queue(monkeypatch):
    import colony_sidecar.api.routers.host as host_mod

    sentinel_queue = object()
    sentinel_consolidator = object()
    monkeypatch.setattr(host_mod, "_task_queue", sentinel_queue,
                        raising=False)
    monkeypatch.setattr(host_mod, "_consolidator", sentinel_consolidator,
                        raising=False)

    reg = SubsystemRegistry()
    assert reg.queue is sentinel_queue
    assert reg.task_queue is sentinel_queue
