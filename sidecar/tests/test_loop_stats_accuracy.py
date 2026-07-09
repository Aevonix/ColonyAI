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
