"""Focused durability and backpressure tests for the host event stream."""

from __future__ import annotations

import asyncio
import threading

import pytest

from colony_sidecar.events.journal import replay_events
from colony_sidecar.events.stream import EventSubscriberBuffer


def test_subscriber_buffer_reports_overflow_and_resume_cursor():
    subscriber = EventSubscriberBuffer(maxsize=2)
    subscriber.mark_delivered({"seq": 40})

    subscriber.publish({"type": "one", "seq": 41})
    subscriber.publish({"type": "two", "seq": 42})
    subscriber.publish({"type": "three", "seq": 43})
    subscriber.publish({"type": "four", "seq": 44})

    marker = subscriber.queue.get_nowait()
    assert subscriber.is_overflow(marker)
    assert subscriber.queue.empty()
    assert subscriber.overflow_frame() == {
        "type": "stream_overflow",
        "droppedCount": 4,
        "firstDroppedSeq": 41,
        "lastDroppedSeq": 44,
        "resumeAfterSeq": 40,
    }


@pytest.mark.asyncio
async def test_cross_thread_publication_preserves_sequence_order():
    subscriber = EventSubscriberBuffer(
        maxsize=4, loop=asyncio.get_running_loop()
    )
    worker = threading.Thread(
        target=subscriber.publish,
        args=({"type": "first", "seq": 1},),
    )
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()

    # The worker callback is queued but cannot execute until this coroutine
    # yields. A same-loop publication must queue behind it, not jump ahead.
    subscriber.publish({"type": "second", "seq": 2})
    await asyncio.sleep(0)

    assert (await subscriber.get())["seq"] == 1
    assert (await subscriber.get())["seq"] == 2


def test_host_persists_before_publishing_live_frame(tmp_path, monkeypatch):
    from colony_sidecar.api.routers import host

    monkeypatch.setenv("COLONY_EVENT_JOURNAL_DIR", str(tmp_path / "events"))
    observed = []

    class _Subscriber:
        def publish(self, frame):
            durable = replay_events(after_seq=frame["seq"] - 1, limit=1)
            observed.append((frame, durable["events"]))

    subscriber = _Subscriber()
    with host._event_broadcast_lock:
        host._event_subscribers.append(subscriber)
    try:
        frame = host.broadcast_event({
            "type": "truth.changed",
            "occurred_at": "2026-07-09T12:00:00+00:00",
            "payload": {"value": "measured"},
        })
    finally:
        with host._event_broadcast_lock:
            host._event_subscribers.remove(subscriber)

    assert frame is not None
    assert frame["seq"] == 1
    assert frame["recordedAt"]
    assert frame["eventId"]
    assert observed and observed[0][0] == frame
    durable = observed[0][1][0]
    assert durable["seq"] == frame["seq"]
    assert durable["recordedAt"] == frame["recordedAt"]
    assert durable["data"] == frame["payload"]


def test_host_suppresses_live_frame_when_journal_fails(monkeypatch):
    from colony_sidecar.api.routers import host
    from colony_sidecar.events import journal

    published = []

    class _Subscriber:
        def publish(self, frame):
            published.append(frame)

    subscriber = _Subscriber()
    monkeypatch.setattr(journal, "append_event_record", lambda *a, **k: None)
    with host._event_broadcast_lock:
        host._event_subscribers.append(subscriber)
    try:
        result = host.broadcast_event({"type": "not.durable", "payload": {}})
    finally:
        with host._event_broadcast_lock:
            host._event_subscribers.remove(subscriber)

    assert result is None
    assert published == []


def test_websocket_reconnect_replays_from_exact_sequence(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from colony_sidecar.api.routers import host

    monkeypatch.setenv("COLONY_API_KEY", "test-event-key")
    monkeypatch.setenv("COLONY_EVENT_JOURNAL_DIR", str(tmp_path / "events"))
    monkeypatch.setenv("COLONY_EVENT_SUBSCRIBER_QUEUE_SIZE", "8")
    app = FastAPI()
    app.include_router(host.router)

    with TestClient(app) as client:
        with client.websocket_connect("/v1/host/events") as socket:
            socket.send_json({"type": "auth", "token": "test-event-key"})
            connected = socket.receive_json()
            replay_complete = socket.receive_json()
            assert connected["type"] == "connected"
            assert replay_complete["type"] == "replay_complete"

            first = host.broadcast_event({
                "type": "live.one",
                "payload": {"measured": 1},
            })
            assert first is not None
            assert socket.receive_json() == first

        second = host.broadcast_event({
            "type": "offline.two",
            "payload": {"measured": 2},
        })
        assert second is not None

        with client.websocket_connect("/v1/host/events") as socket:
            socket.send_json({
                "type": "auth",
                "token": "test-event-key",
                "lastEventSeq": first["seq"],
                "lastEventTime": first["recordedAt"],
            })
            assert socket.receive_json()["type"] == "connected"
            replayed = socket.receive_json()
            assert replayed["seq"] == second["seq"]
            assert replayed["recordedAt"] == second["recordedAt"]
            replay_complete = socket.receive_json()
            assert replay_complete["type"] == "replay_complete"
            assert replay_complete["replayedCount"] == 1

    assert host._event_subscribers == []


def test_websocket_cursor_ahead_of_journal_replays_new_epoch(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from colony_sidecar.api.routers import host

    monkeypatch.setenv("COLONY_API_KEY", "test-event-key")
    monkeypatch.setenv("COLONY_EVENT_JOURNAL_DIR", str(tmp_path / "events"))
    first = host.broadcast_event({"type": "epoch.one", "payload": {"n": 1}})
    second = host.broadcast_event({"type": "epoch.two", "payload": {"n": 2}})
    assert first is not None and second is not None

    app = FastAPI()
    app.include_router(host.router)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/host/events") as socket:
            socket.send_json({
                "type": "auth",
                "token": "test-event-key",
                "lastEventSeq": 99,
            })
            connected = socket.receive_json()
            reset = socket.receive_json()
            replayed = [socket.receive_json(), socket.receive_json()]
            complete = socket.receive_json()

    assert connected["cursorReset"] is True
    assert reset["type"] == "replay_reset"
    assert reset["journalHighWaterSeq"] == 2
    assert [frame["seq"] for frame in replayed] == [1, 2]
    assert complete["type"] == "replay_complete"
    assert complete["replayThroughSeq"] == 2
    assert host._event_subscribers == []
