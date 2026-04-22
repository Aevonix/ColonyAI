"""Tests for Colony event bus and event types."""

import asyncio

import pytest
from datetime import datetime

from colony_sidecar.events.types import (
    CognitionEvent,
    Event,
    IntegrationEvent,
    MemoryEvent,
    MeshEvent,
    PersonEvent,
    SignalEvent,
)
from colony_sidecar.events.bus import EventBus, Subscription, TypedEventBus
from colony_sidecar.models.memory import Memory, MemoryType
from colony_sidecar.models.mesh import NodeRole
from colony_sidecar.models.signal import Signal, SignalType


# --- Event type tests ---


class TestEvent:
    def test_minimal_event(self):
        e = Event(id="e1")
        assert e.id == "e1"
        assert e.source == "colony"
        assert isinstance(e.timestamp, datetime)

    def test_custom_source(self):
        e = Event(id="e2", source="mesh")
        assert e.source == "mesh"

    def test_custom_timestamp(self):
        ts = datetime(2026, 1, 1, 12, 0, 0)
        e = Event(id="e3", timestamp=ts)
        assert e.timestamp == ts


class TestPersonEvent:
    def test_defaults(self):
        e = PersonEvent(id="pe1", person_id="p1", event_type="created")
        assert e.person_id == "p1"
        assert e.event_type == "created"
        assert e.old_value is None
        assert e.new_value is None
        assert e.context == {}

    def test_tier_change(self):
        e = PersonEvent(
            id="pe2",
            person_id="p1",
            event_type="tier_changed",
            old_value="peripheral",
            new_value="trusted",
            context={"score": 65.0},
        )
        assert e.old_value == "peripheral"
        assert e.new_value == "trusted"
        assert e.context["score"] == 65.0

    def test_mutable_defaults_are_independent(self):
        e1 = PersonEvent(id="a", person_id="p1", event_type="created")
        e2 = PersonEvent(id="b", person_id="p2", event_type="created")
        e1.context["key"] = "value"
        assert e2.context == {}

    def test_inherits_from_event(self):
        e = PersonEvent(id="pe3", person_id="p1", event_type="created")
        assert isinstance(e, Event)
        assert isinstance(e.timestamp, datetime)


class TestSignalEvent:
    def test_with_signal(self):
        sig = Signal(
            id="s1",
            person_id="p1",
            signal_type=SignalType.MESSAGE_FREQUENCY,
            value=0.8,
        )
        e = SignalEvent(id="se1", signal=sig)
        assert e.signal is sig
        assert e.signal.value == 0.8


class TestMemoryEvent:
    def test_with_memory(self):
        mem = Memory(id="m1", type=MemoryType.EPISODIC, content="test memory")
        e = MemoryEvent(id="me1", memory=mem, event_type="created")
        assert e.memory is mem
        assert e.event_type == "created"


class TestMeshEvent:
    def test_defaults(self):
        e = MeshEvent(id="mesh1", node_id="n1", event_type="registered")
        assert e.node_id == "n1"
        assert e.old_role is None
        assert e.new_role is None
        assert e.metadata == {}

    def test_role_change(self):
        e = MeshEvent(
            id="mesh2",
            node_id="n1",
            event_type="role_changed",
            old_role=NodeRole.VASSAL,
            new_role=NodeRole.REGENT,
        )
        assert e.old_role == NodeRole.VASSAL
        assert e.new_role == NodeRole.REGENT

    def test_mutable_defaults_are_independent(self):
        e1 = MeshEvent(id="a", node_id="n1", event_type="registered")
        e2 = MeshEvent(id="b", node_id="n2", event_type="registered")
        e1.metadata["key"] = "value"
        assert e2.metadata == {}


class TestCognitionEvent:
    def test_defaults(self):
        e = CognitionEvent(id="c1", component="metalearner", event_type="gap_detected")
        assert e.component == "metalearner"
        assert e.details == {}

    def test_with_details(self):
        e = CognitionEvent(
            id="c2",
            component="synthesis",
            event_type="cpi_computed",
            details={"cpi": 0.73, "dimension": "social"},
        )
        assert e.details["cpi"] == 0.73


class TestIntegrationEvent:
    def test_defaults(self):
        e = IntegrationEvent(id="i1", integration="health", event_type="sync_complete")
        assert e.integration == "health"
        assert e.data == {}

    def test_with_data(self):
        e = IntegrationEvent(
            id="i2",
            integration="meetings",
            event_type="data_received",
            data={"transcript_id": "t123", "duration_min": 45},
        )
        assert e.data["transcript_id"] == "t123"


# --- EventBus tests ---


class TestEventBusSubscribeEmit:
    def test_subscribe_and_emit(self):
        bus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        bus.subscribe(handler, [PersonEvent])
        bus.emit(PersonEvent(id="pe1", person_id="p1", event_type="created"))

        assert len(received) == 1
        assert received[0].id == "pe1"

    def test_multiple_subscribers(self):
        bus = EventBus()
        received_a = []
        received_b = []

        bus.subscribe(lambda e: received_a.append(e), [PersonEvent])
        bus.subscribe(lambda e: received_b.append(e), [PersonEvent])
        bus.emit(PersonEvent(id="pe1", person_id="p1", event_type="created"))

        assert len(received_a) == 1
        assert len(received_b) == 1

    def test_unsubscribe(self):
        bus = EventBus()
        received = []
        sub = bus.subscribe(lambda e: received.append(e), [PersonEvent])
        bus.unsubscribe(sub)
        bus.emit(PersonEvent(id="pe1", person_id="p1", event_type="created"))

        assert len(received) == 0

    def test_unsubscribe_nonexistent_is_safe(self):
        """Unsubscribing something not subscribed doesn't raise."""
        bus = EventBus()
        fake_sub = Subscription(handler=lambda e: None, event_types=[Event])
        bus.unsubscribe(fake_sub)  # Should not raise


class TestEventBusTypeFiltering:
    def test_only_receives_matching_types(self):
        bus = EventBus()
        person_events = []
        mesh_events = []

        bus.subscribe(lambda e: person_events.append(e), [PersonEvent])
        bus.subscribe(lambda e: mesh_events.append(e), [MeshEvent])

        bus.emit(PersonEvent(id="pe1", person_id="p1", event_type="created"))
        bus.emit(MeshEvent(id="me1", node_id="n1", event_type="registered"))

        assert len(person_events) == 1
        assert len(mesh_events) == 1
        assert person_events[0].id == "pe1"
        assert mesh_events[0].id == "me1"

    def test_subscribe_to_multiple_types(self):
        bus = EventBus()
        received = []

        bus.subscribe(lambda e: received.append(e), [PersonEvent, MeshEvent])

        bus.emit(PersonEvent(id="pe1", person_id="p1", event_type="created"))
        bus.emit(MeshEvent(id="me1", node_id="n1", event_type="registered"))
        bus.emit(CognitionEvent(id="ce1", component="metalearner", event_type="gap_detected"))

        assert len(received) == 2

    def test_subscribe_to_base_event_catches_all(self):
        """Subscribing to Event catches all event subtypes."""
        bus = EventBus()
        received = []

        bus.subscribe(lambda e: received.append(e), [Event])

        bus.emit(PersonEvent(id="pe1", person_id="p1", event_type="created"))
        bus.emit(MeshEvent(id="me1", node_id="n1", event_type="registered"))
        bus.emit(Event(id="e1"))

        assert len(received) == 3


class TestEventBusFilter:
    def test_custom_filter(self):
        bus = EventBus()
        tier_changes = []

        bus.subscribe(
            lambda e: tier_changes.append(e),
            [PersonEvent],
            filter_fn=lambda e: e.event_type == "tier_changed",
        )

        bus.emit(PersonEvent(id="pe1", person_id="p1", event_type="created"))
        bus.emit(PersonEvent(id="pe2", person_id="p1", event_type="tier_changed"))

        assert len(tier_changes) == 1
        assert tier_changes[0].id == "pe2"

    def test_filter_receives_nothing_when_none_match(self):
        bus = EventBus()
        received = []

        bus.subscribe(
            lambda e: received.append(e),
            [PersonEvent],
            filter_fn=lambda e: False,
        )

        bus.emit(PersonEvent(id="pe1", person_id="p1", event_type="created"))
        assert len(received) == 0


class TestEventBusErrorIsolation:
    def test_handler_error_does_not_break_others(self):
        bus = EventBus()
        received = []

        def bad_handler(event):
            raise ValueError("intentional test error")

        def good_handler(event):
            received.append(event)

        bus.subscribe(bad_handler, [PersonEvent])
        bus.subscribe(good_handler, [PersonEvent])

        bus.emit(PersonEvent(id="pe1", person_id="p1", event_type="created"))

        assert len(received) == 1
        assert received[0].id == "pe1"

    def test_error_is_logged(self, caplog):
        bus = EventBus()

        def bad_handler(event):
            raise RuntimeError("boom")

        bus.subscribe(bad_handler, [PersonEvent])

        with caplog.at_level("ERROR", logger="colony.events.bus"):
            bus.emit(PersonEvent(id="pe1", person_id="p1", event_type="created"))

        assert "Event handler error" in caplog.text
        assert "boom" in caplog.text


class TestEventBusHistory:
    def test_events_recorded_in_history(self):
        bus = EventBus()
        bus.emit(PersonEvent(id="pe1", person_id="p1", event_type="created"))
        bus.emit(MeshEvent(id="me1", node_id="n1", event_type="registered"))

        history = bus.get_history()
        assert len(history) == 2

    def test_history_filtered_by_type(self):
        bus = EventBus()
        bus.emit(PersonEvent(id="pe1", person_id="p1", event_type="created"))
        bus.emit(MeshEvent(id="me1", node_id="n1", event_type="registered"))

        person_history = bus.get_history(event_types=[PersonEvent])
        assert len(person_history) == 1
        assert person_history[0].id == "pe1"

    def test_history_respects_limit(self):
        bus = EventBus()
        for i in range(10):
            bus.emit(Event(id=f"e{i}"))

        history = bus.get_history(limit=3)
        assert len(history) == 3
        assert history[0].id == "e7"
        assert history[2].id == "e9"

    def test_history_bounded_at_max(self):
        bus = EventBus(max_history=5)
        for i in range(10):
            bus.emit(Event(id=f"e{i}"))

        history = bus.get_history()
        assert len(history) == 5
        assert history[0].id == "e5"  # Oldest kept
        assert history[4].id == "e9"  # Most recent

    def test_clear_history(self):
        bus = EventBus()
        bus.emit(Event(id="e1"))
        bus.clear_history()
        assert bus.get_history() == []


# --- TypedEventBus tests ---


class TestTypedEventBus:
    def test_emit_person_event(self):
        bus = TypedEventBus()
        received = []
        bus.subscribe(lambda e: received.append(e), [PersonEvent])

        bus.emit_person_event("p1", "tier_changed", old_value="peripheral", new_value="trusted")

        assert len(received) == 1
        assert received[0].person_id == "p1"
        assert received[0].event_type == "tier_changed"
        assert received[0].old_value == "peripheral"
        assert received[0].new_value == "trusted"
        assert received[0].id == "person-p1-tier_changed"

    def test_emit_memory_event(self):
        bus = TypedEventBus()
        received = []
        bus.subscribe(lambda e: received.append(e), [MemoryEvent])

        mem = Memory(id="m1", type=MemoryType.EPISODIC, content="test")
        bus.emit_memory_event(mem, "created")

        assert len(received) == 1
        assert received[0].memory is mem
        assert received[0].event_type == "created"
        assert received[0].id == "memory-m1-created"

    def test_emit_mesh_event(self):
        bus = TypedEventBus()
        received = []
        bus.subscribe(lambda e: received.append(e), [MeshEvent])

        bus.emit_mesh_event(
            "n1",
            "role_changed",
            old_role=NodeRole.VASSAL,
            new_role=NodeRole.REGENT,
        )

        assert len(received) == 1
        assert received[0].node_id == "n1"
        assert received[0].old_role == NodeRole.VASSAL
        assert received[0].new_role == NodeRole.REGENT
        assert received[0].id == "mesh-n1-role_changed"


# --- Async tests ---


class TestAsyncEmit:
    def test_async_emit_calls_sync_handler(self):
        bus = EventBus()
        received = []

        bus.subscribe(lambda e: received.append(e), [PersonEvent])

        async def run():
            await bus.emit_async(PersonEvent(id="pe1", person_id="p1", event_type="created"))

        asyncio.run(run())
        assert len(received) == 1

    def test_async_emit_calls_async_handler(self):
        bus = EventBus()
        received = []

        async def async_handler(event):
            received.append(event)

        bus.subscribe(async_handler, [PersonEvent])

        async def run():
            await bus.emit_async(PersonEvent(id="pe1", person_id="p1", event_type="created"))

        asyncio.run(run())
        assert len(received) == 1

    def test_async_emit_error_isolation(self):
        bus = EventBus()
        received = []

        async def bad_async_handler(event):
            raise ValueError("async boom")

        def good_handler(event):
            received.append(event)

        bus.subscribe(bad_async_handler, [PersonEvent])
        bus.subscribe(good_handler, [PersonEvent])

        async def run():
            await bus.emit_async(PersonEvent(id="pe1", person_id="p1", event_type="created"))

        asyncio.run(run())
        assert len(received) == 1

    def test_async_emit_records_history(self):
        bus = EventBus()

        async def run():
            await bus.emit_async(PersonEvent(id="pe1", person_id="p1", event_type="created"))

        asyncio.run(run())
        assert len(bus.get_history()) == 1


# --- Package import tests ---


class TestPackageImports:
    """Verify all public names are importable from the package."""

    def test_import_all_from_package(self):
        from colony_sidecar.events import (
            CognitionEvent,
            Event,
            EventBus,
            IntegrationEvent,
            MemoryEvent,
            MeshEvent,
            PersonEvent,
            SignalEvent,
            Subscription,
            TypedEventBus,
        )

        assert Event is not None
        assert EventBus is not None
        assert TypedEventBus is not None
        assert Subscription is not None
        assert PersonEvent is not None
        assert SignalEvent is not None
        assert MemoryEvent is not None
        assert MeshEvent is not None
        assert CognitionEvent is not None
        assert IntegrationEvent is not None
