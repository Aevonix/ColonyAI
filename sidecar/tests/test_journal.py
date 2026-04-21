"""Tests for the event journal subsystem."""

import json
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import pytest

from colony_sidecar.events.journal import (
    append_event,
    replay_events,
    _atomic_write,
    _format_seq,
)


@pytest.fixture
def journal_dir(tmp_path, monkeypatch):
    """Point the journal at a temp directory."""
    d = tmp_path / "events"
    d.mkdir()
    monkeypatch.setenv("COLONY_EVENT_JOURNAL_DIR", str(d))
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    return d


class TestAppendEvent:
    def test_append_creates_file(self, journal_dir):
        seq = append_event("memory.consolidated", {"entry_id": "abc123"})
        assert seq == 1

        files = list(journal_dir.glob("*.json"))
        assert len(files) == 1

        # Filename format: 000001.<ulid>.json
        name = files[0].name
        assert name.startswith("000001.")
        assert name.endswith(".json")

    def test_append_sequential_numbering(self, journal_dir):
        s1 = append_event("goal.activated", {"goal_id": "g1"})
        s2 = append_event("goal.activated", {"goal_id": "g2"})
        s3 = append_event("memory.created", {"content": "test"})

        assert s1 == 1
        assert s2 == 2
        assert s3 == 3

        files = sorted(journal_dir.glob("*.json"))
        assert len(files) == 3
        assert files[0].name.startswith("000001.")
        assert files[1].name.startswith("000002.")
        assert files[2].name.startswith("000003.")

    def test_append_event_contents(self, journal_dir):
        append_event("signal.ingested", {"signal_type": "engagement", "score": 0.8})

        files = list(journal_dir.glob("*.json"))
        raw = json.loads(files[0].read_text())

        assert raw["type"] == "signal.ingested"
        assert raw["data"]["signal_type"] == "engagement"
        assert raw["data"]["score"] == 0.8
        assert "recordedAt" in raw
        assert "checksum" in raw

    def test_append_prunes_old_events(self, journal_dir, monkeypatch):
        monkeypatch.setenv("COLONY_EVENT_JOURNAL_RETENTION", "5")

        for i in range(10):
            append_event("test.event", {"i": i})

        files = sorted(journal_dir.glob("*.json"))
        # Should only keep the last 5
        assert len(files) == 5
        # First remaining file should be seq 6
        assert files[0].name.startswith("000006.")

    def test_append_event_returns_minus_one_on_failure(self, tmp_path, monkeypatch):
        # Point at a non-existent directory that can't be created
        monkeypatch.setenv("COLONY_EVENT_JOURNAL_DIR", "/dev/null/impossible")
        seq = append_event("test.event", {})
        assert seq == -1


class TestReplayEvents:
    def test_replay_empty_journal(self, journal_dir):
        result = replay_events(since="2026-01-01T00:00:00Z")
        assert result["events"] == []
        assert result["lastSeq"] == 0
        assert result["hasMore"] is False

    def test_replay_returns_events_after_since(self, journal_dir):
        # Write events with known timestamps
        for i in range(5):
            append_event("test.event", {"i": i})

        # Read the first event's timestamp
        files = sorted(journal_dir.glob("*.json"))
        first_event = json.loads(files[0].read_text())
        first_ts = first_event["recordedAt"]

        result = replay_events(since=first_ts)
        # Should return events 2-5 (after first timestamp)
        assert len(result["events"]) == 4
        assert result["events"][0]["seq"] == 2

    def test_replay_respects_limit(self, journal_dir):
        for i in range(10):
            append_event("test.event", {"i": i})

        result = replay_events(since="2020-01-01T00:00:00Z", limit=3)
        assert len(result["events"]) == 3
        assert result["hasMore"] is True

    def test_replay_filters_by_type(self, journal_dir):
        append_event("memory.created", {"content": "hello"})
        append_event("goal.activated", {"goal_id": "g1"})
        append_event("memory.updated", {"content": "world"})

        result = replay_events(since="2020-01-01T00:00:00Z", types=["memory.created", "memory.updated"])
        assert len(result["events"]) == 2
        assert all(e["type"].startswith("memory.") for e in result["events"])

    def test_replay_event_structure(self, journal_dir):
        append_event("test.event", {"key": "value"})

        result = replay_events(since="2020-01-01T00:00:00Z")
        event = result["events"][0]

        assert "seq" in event
        assert "ulid" in event
        assert "type" in event
        assert "recordedAt" in event
        assert "data" in event
        assert event["type"] == "test.event"
        assert event["data"]["key"] == "value"


class TestAtomicWrite:
    def test_atomic_write_creates_file(self, tmp_path):
        target = tmp_path / "test.json"
        _atomic_write(target, '{"hello": "world"}\n')
        assert target.exists()
        assert target.read_text() == '{"hello": "world"}\n'

    def test_atomic_write_no_partial_files(self, tmp_path):
        target = tmp_path / "test.json"
        _atomic_write(target, "content")
        # No .tmp files left behind
        assert not list(tmp_path.glob("*.tmp"))


class TestFormatSeq:
    def test_zero_padded(self):
        assert _format_seq(1) == "000001"
        assert _format_seq(42) == "000042"
        assert _format_seq(999999) == "999999"
