"""Tests for AffectStore."""

import os
import tempfile

import pytest

from colony_sidecar.tom.affect import AffectStore


@pytest.fixture
def store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    s = AffectStore(path)
    yield s
    s.close()
    os.unlink(path)


class TestAffectCreateEvent:
    def test_create_basic(self, store):
        result = store.create_event(
            contact_id="owner",
            valence=0.5,
            arousal=0.7,
            source="explicit",
            trigger="good news",
        )
        assert result["contact_id"] == "owner"
        assert result["valence"] == 0.5
        assert result["arousal"] == 0.7
        assert result["source"] == "explicit"
        assert result["trigger"] == "good news"
        assert result["id"]
        assert result["timestamp"]

    def test_valence_clamped(self, store):
        result = store.create_event(contact_id="owner", valence=2.0, source="explicit")
        assert result["valence"] == 1.0

    def test_valence_negative_clamped(self, store):
        result = store.create_event(contact_id="owner", valence=-3.0, source="explicit")
        assert result["valence"] == -1.0

    def test_arousal_clamped(self, store):
        result = store.create_event(contact_id="owner", valence=0.0, arousal=1.5, source="explicit")
        assert result["arousal"] == 1.0

    def test_custom_timestamp(self, store):
        ts = "2026-01-01T00:00:00+00:00"
        result = store.create_event(contact_id="owner", valence=0.3, source="explicit", timestamp=ts)
        assert result["timestamp"] == ts


class TestAffectGetEvent:
    def test_get_existing(self, store):
        created = store.create_event(contact_id="owner", valence=0.5, source="explicit")
        result = store.get_event(created["id"])
        assert result is not None
        assert result["id"] == created["id"]

    def test_get_nonexistent(self, store):
        assert store.get_event("nope") is None


class TestAffectListEvents:
    def test_list_all(self, store):
        store.create_event(contact_id="owner", valence=0.5, source="explicit")
        store.create_event(contact_id="alice", valence=-0.3, source="inferred")
        events = store.list_events()
        assert len(events) == 2

    def test_list_by_contact(self, store):
        store.create_event(contact_id="owner", valence=0.5, source="explicit")
        store.create_event(contact_id="alice", valence=-0.3, source="inferred")
        events = store.list_events(contact_id="owner")
        assert len(events) == 1
        assert events[0]["contact_id"] == "owner"

    def test_list_by_source(self, store):
        store.create_event(contact_id="owner", valence=0.5, source="explicit")
        store.create_event(contact_id="owner", valence=0.3, source="inferred")
        events = store.list_events(source="inferred")
        assert len(events) == 1
        assert events[0]["source"] == "inferred"

    def test_list_pagination(self, store):
        for i in range(5):
            store.create_event(contact_id="owner", valence=0.1 * i, source="explicit")
        events = store.list_events(limit=2, offset=0)
        assert len(events) == 2


class TestAffectDeleteEvent:
    def test_delete_existing(self, store):
        created = store.create_event(contact_id="owner", valence=0.5, source="explicit")
        assert store.delete_event(created["id"]) is True
        assert store.get_event(created["id"]) is None

    def test_delete_nonexistent(self, store):
        assert store.delete_event("nope") is False

    def test_delete_recomputes_state(self, store):
        e1 = store.create_event(contact_id="owner", valence=0.8, source="explicit")
        store.create_event(contact_id="owner", valence=0.2, source="explicit")
        store.delete_event(e1["id"])
        state = store.get_state("owner")
        assert state["event_count"] == 1


class TestAffectState:
    def test_state_no_events(self, store):
        state = store.get_state("nobody")
        assert state["current_valence"] == 0.0
        assert state["trend"] == "stable"
        assert state["event_count"] == 0

    def test_state_single_event(self, store):
        store.create_event(contact_id="owner", valence=0.5, source="explicit")
        state = store.get_state("owner")
        assert state["current_valence"] == 0.5
        assert state["event_count"] == 1
        assert state["trend"] == "stable"

    def test_state_multiple_events(self, store):
        store.create_event(contact_id="owner", valence=-0.3, source="explicit")
        store.create_event(contact_id="owner", valence=0.5, source="explicit")
        store.create_event(contact_id="owner", valence=0.8, source="explicit")
        state = store.get_state("owner")
        assert state["current_valence"] > 0
        assert state["event_count"] == 3
        assert state["trend"] == "improving"

    def test_declining_trend(self, store):
        store.create_event(contact_id="owner", valence=0.8, source="explicit")
        store.create_event(contact_id="owner", valence=0.3, source="explicit")
        store.create_event(contact_id="owner", valence=-0.2, source="explicit")
        state = store.get_state("owner")
        assert state["trend"] == "declining"

    def test_stable_trend(self, store):
        store.create_event(contact_id="owner", valence=0.3, source="explicit")
        store.create_event(contact_id="owner", valence=0.35, source="explicit")
        state = store.get_state("owner")
        assert state["trend"] == "stable"


class TestAffectDetection:
    def test_negative_spike_detected(self, store):
        store.create_event(contact_id="owner", valence=-0.7, source="explicit")
        assert store.detect_negative_spike("owner") is True

    def test_no_negative_spike(self, store):
        store.create_event(contact_id="owner", valence=0.3, source="explicit")
        assert store.detect_negative_spike("owner") is False

    def test_negative_spike_no_events(self, store):
        assert store.detect_negative_spike("nobody") is False

    def test_sustained_decline(self, store):
        store.create_event(contact_id="owner", valence=0.8, source="explicit")
        store.create_event(contact_id="owner", valence=0.3, source="explicit")
        store.create_event(contact_id="owner", valence=-0.2, source="explicit")
        assert store.detect_sustained_decline("owner") is True

    def test_no_sustained_decline(self, store):
        store.create_event(contact_id="owner", valence=0.5, source="explicit")
        assert store.detect_sustained_decline("owner") is False
