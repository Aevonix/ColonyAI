"""Hermes event subscriber replay and first-frame regression tests."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

_PLUGIN_DIR = (
    pathlib.Path(__file__).resolve().parents[2] / "plugins" / "hermes-plugin"
)


def _load_events_module():
    name = "colony_hermes_events_replay_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "events.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeSocket:
    def __init__(self, subscriber, first_frame):
        self.subscriber = subscriber
        self.first_frame = first_frame
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        # Stop after returning one frame so _connect_and_listen exits cleanly.
        self.subscriber._stop_event.set()
        return json.dumps(self.first_frame)

    async def ping(self):
        return None


class _ConnectContext:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_first_real_frame_is_cached_and_resume_uses_seq_and_record_time(monkeypatch):
    module = _load_events_module()
    subscriber = module.ColonyEventSubscriber(
        "http://colony.invalid", api_key="secret", reconnect_delay=0
    )
    await subscriber._handle_message({
        "type": "memory.created",
        "seq": 7,
        "occurred_at": "2026-07-09T11:59:00+00:00",
        "recordedAt": "2026-07-09T12:00:00+00:00",
        "payload": {"id": "m-7"},
    })

    socket = _FakeSocket(subscriber, {
        "type": "goal.updated",
        "seq": 8,
        "occurred_at": "2026-07-09T12:00:01+00:00",
        "recordedAt": "2026-07-09T12:00:02+00:00",
        "payload": {"id": "g-8"},
    })
    monkeypatch.setattr(
        module.websockets,
        "connect",
        lambda *_args, **_kwargs: _ConnectContext(socket),
    )

    await subscriber._connect_and_listen()

    auth = json.loads(socket.sent[0])
    assert auth["lastEventSeq"] == 7
    assert auth["lastEventTime"] == "2026-07-09T12:00:00+00:00"
    assert auth["lastEventId"] == auth["lastEventTime"]
    cached = await subscriber.cache.get_all()
    assert [event.seq for event in cached] == [8, 7]
    assert subscriber.cache.last_event_seq == 8
    assert subscriber.cache.last_event_time == "2026-07-09T12:00:02+00:00"


@pytest.mark.asyncio
async def test_replay_live_duplicate_is_idempotent():
    module = _load_events_module()
    cache = module.EventCache(max_per_type=5)
    event = module.CachedEvent(
        type="memory.created",
        payload={"id": "m-1"},
        occurred_at="2026-07-09T12:00:00+00:00",
        recorded_at="2026-07-09T12:00:01+00:00",
        seq=12,
    )

    assert await cache.add(event) is True
    assert await cache.add(event) is False
    assert len(await cache.get_all()) == 1


@pytest.mark.asyncio
async def test_replay_complete_advances_empty_stream_baseline():
    module = _load_events_module()
    subscriber = module.ColonyEventSubscriber("http://colony.invalid")

    await subscriber._handle_message({
        "type": "replay_complete",
        "replayedCount": 0,
        "lastSeq": 0,
        "replayThroughSeq": 73,
    })

    assert subscriber.cache.last_event_seq == 73
    assert await subscriber.cache.get_all() == []


@pytest.mark.asyncio
async def test_server_journal_reset_clears_stale_cursor_and_events():
    module = _load_events_module()
    subscriber = module.ColonyEventSubscriber("http://colony.invalid")
    await subscriber._handle_message({
        "type": "memory.created",
        "seq": 73,
        "recordedAt": "2026-07-09T12:00:00+00:00",
        "payload": {"id": "old-epoch"},
    })

    await subscriber._handle_message({
        "type": "connected",
        "journalHighWaterSeq": 0,
    })
    await subscriber._handle_message({
        "type": "memory.created",
        "seq": 1,
        "recordedAt": "2026-07-09T12:01:00+00:00",
        "payload": {"id": "new-epoch"},
    })

    cached = await subscriber.cache.get_all()
    assert [event.seq for event in cached] == [1]
    assert cached[0].payload["id"] == "new-epoch"
