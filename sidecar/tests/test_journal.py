"""Tests for the event journal subsystem."""

import json
import multiprocessing
import os
import tempfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timezone

import pytest

from colony_sidecar.events.journal import (
    append_event,
    append_event_record,
    current_sequence,
    replay_events,
    _atomic_write,
    _format_seq,
)


def _append_from_process(i):
    """Pickleable worker for the cross-process sequence-allocation test."""
    return append_event("process.event", {"i": i})


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

    def test_concurrent_appenders_allocate_unique_monotonic_sequences(self, journal_dir):
        count = 80
        with ThreadPoolExecutor(max_workers=8) as pool:
            sequences = list(pool.map(
                lambda i: append_event("concurrent.event", {"i": i}),
                range(count),
            ))

        assert sorted(sequences) == list(range(1, count + 1))
        assert current_sequence() == count
        assert len(list(journal_dir.glob("*.json"))) == count
        assert not list(journal_dir.rglob("*.tmp"))

    @pytest.mark.skipif(os.name != "posix", reason="cross-process flock is POSIX-only")
    def test_processes_share_sequence_allocator(self, journal_dir):
        count = 40
        with ProcessPoolExecutor(
            max_workers=4,
            mp_context=multiprocessing.get_context("spawn"),
        ) as pool:
            sequences = list(pool.map(_append_from_process, range(count)))

        assert sorted(sequences) == list(range(1, count + 1))
        assert current_sequence() == count

    def test_steady_state_append_uses_cursor_not_directory_scan(
        self, journal_dir, monkeypatch
    ):
        from colony_sidecar.events import journal

        assert append_event("test.event", {"i": 1}) == 1

        def _unexpected_scan(_directory):
            raise AssertionError("steady-state append rescanned the journal")

        monkeypatch.setattr(journal, "_event_files", _unexpected_scan)
        assert append_event("test.event", {"i": 2}) == 2

    def test_append_record_exposes_exact_durable_metadata(self, journal_dir):
        occurred_at = "2026-07-09T12:00:00+00:00"
        record = append_event_record(
            "test.event", {"truth": True}, occurred_at=occurred_at
        )

        assert record is not None
        assert record["seq"] == 1
        assert record["occurredAt"] == occurred_at
        assert record["recordedAt"]
        persisted = json.loads(next(journal_dir.glob("*.json")).read_text())
        assert persisted["seq"] == record["seq"]
        assert persisted["recordedAt"] == record["recordedAt"]
        assert persisted["ulid"] == record["ulid"]


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

    def test_replay_by_exact_sequence_and_high_water(self, journal_dir):
        for i in range(6):
            append_event("test.event", {"i": i})

        result = replay_events(after_seq=2, until_seq=5, limit=10)

        assert [event["seq"] for event in result["events"]] == [3, 4, 5]
        assert result["firstAvailableSeq"] == 1
        assert result["journalLastSeq"] == 6

    def test_replay_rejects_valid_json_with_bad_checksum(self, journal_dir):
        append_event("test.event", {"value": "original"})
        path = next(journal_dir.glob("*.json"))
        tampered = json.loads(path.read_text())
        tampered["data"]["value"] = "tampered"
        path.write_text(json.dumps(tampered) + "\n")

        result = replay_events(after_seq=0)

        assert result["events"] == []
        assert result["corruptCount"] == 1


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
