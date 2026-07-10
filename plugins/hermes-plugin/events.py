"""WebSocket event subscriber for Colony sidecar.

Maintains a persistent WebSocket connection to Colony's /v1/host/events
and caches the most recent events for injection into Hermes turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import websockets

logger = logging.getLogger(__name__)


@dataclass
class CachedEvent:
    type: str
    payload: dict[str, Any]
    occurred_at: str
    seq: int
    recorded_at: str = ""


class EventCache:
    """Thread-safe cache of recent Colony events."""

    def __init__(self, max_per_type: int = 5):
        self._lock = asyncio.Lock()
        self._events: dict[str, list[CachedEvent]] = {}
        self._max_per_type = max_per_type
        self._last_seq = 0
        self._last_recorded_at = ""

    async def add(self, event: CachedEvent) -> bool:
        async with self._lock:
            # Replay/live overlap and reconnect retries can legitimately offer
            # the same durable record twice. Sequence is the authoritative
            # idempotency key for modern sidecars.
            if event.seq > 0 and event.seq <= self._last_seq:
                return False
            self._events.setdefault(event.type, [])
            self._events[event.type].insert(0, event)
            self._events[event.type] = self._events[event.type][: self._max_per_type]
            if event.seq > self._last_seq:
                self._last_seq = event.seq
            recorded_at = event.recorded_at or event.occurred_at
            if recorded_at:
                self._last_recorded_at = recorded_at
            return True

    async def get(self, event_type: str) -> list[CachedEvent]:
        async with self._lock:
            return list(self._events.get(event_type, []))

    async def get_all(self) -> list[CachedEvent]:
        async with self._lock:
            all_events: list[CachedEvent] = []
            for evts in self._events.values():
                all_events.extend(evts)
            return sorted(all_events, key=lambda e: e.seq, reverse=True)

    async def clear_type(self, event_type: str) -> None:
        async with self._lock:
            self._events.pop(event_type, None)

    async def advance_cursor(self, seq: int) -> None:
        """Acknowledge a replay high-water mark with no intervening event."""
        async with self._lock:
            if seq > self._last_seq:
                self._last_seq = seq

    async def reset_cursor(self, seq: int = 0) -> None:
        """Reset cached epoch state when the server journal moves backwards."""
        async with self._lock:
            self._events.clear()
            self._last_seq = max(0, seq)
            self._last_recorded_at = ""

    @property
    def last_event_id(self) -> str:
        """Legacy timestamp cursor retained for older Colony sidecars."""
        return self._last_recorded_at

    @property
    def last_event_seq(self) -> int:
        return self._last_seq

    @property
    def last_event_time(self) -> str:
        return self._last_recorded_at


class ColonyEventSubscriber:
    """Subscribes to Colony WebSocket events and maintains a cache."""

    def __init__(
        self,
        url: str,
        api_key: str = "",
        contact_id: str = "default",
        reconnect_delay: float = 5.0,
    ):
        self._url = url.replace("http://", "ws://").replace("https://", "wss://")
        self._api_key = api_key
        self._contact_id = contact_id
        self._reconnect_delay = reconnect_delay
        self._cache = EventCache()
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    @property
    def cache(self) -> EventCache:
        return self._cache

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._run())
        except RuntimeError:
            # No running loop — caller must await start_async()
            pass

    async def start_async(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._connect_and_listen()
            except Exception as exc:
                logger.debug("Colony event subscriber error: %s", exc)
            if not self._stop_event.is_set():
                await asyncio.sleep(self._reconnect_delay)

    async def _connect_and_listen(self) -> None:
        ws_url = f"{self._url}/v1/host/events"
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        async with websockets.connect(ws_url, additional_headers=headers) as ws:
            # Auth handshake
            auth = {
                "type": "auth",
                "token": self._api_key,
                "contact_id": self._contact_id,
            }
            if self._cache.last_event_seq > 0:
                auth["lastEventSeq"] = self._cache.last_event_seq
            if self._cache.last_event_time:
                # Send both the explicit field and the legacy alias so either
                # side of a rolling upgrade can resume correctly.
                auth["lastEventTime"] = self._cache.last_event_time
                auth["lastEventId"] = self._cache.last_event_time
            await ws.send(json.dumps(auth))

            # Every received frame goes through one handler. The old handshake
            # consumed the first frame while looking for replay_complete, which
            # silently dropped a real event when there was nothing to replay.
            while not self._stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    data = json.loads(msg)
                    await self._handle_message(data)
                    if data.get("type") in ("stream_overflow", "replay_error"):
                        break
                except asyncio.TimeoutError:
                    # Use a protocol-level ping; the host stream is write-only
                    # after auth and does not consume application ping frames.
                    await ws.ping()
                except websockets.exceptions.ConnectionClosed:
                    break

    async def _handle_message(self, data: dict[str, Any]) -> None:
        msg_type = data.get("type", "")
        if msg_type == "connected":
            if "journalHighWaterSeq" not in data:
                return
            try:
                server_high_water = int(data.get("journalHighWaterSeq", 0) or 0)
            except (TypeError, ValueError):
                server_high_water = 0
            if server_high_water < self._cache.last_event_seq:
                logger.error(
                    "Colony event journal moved backwards (%s -> %s); "
                    "resetting the local replay cursor",
                    self._cache.last_event_seq,
                    server_high_water,
                )
                await self._cache.reset_cursor(server_high_water)
            return
        if msg_type in (
            "ping",
            "pong",
            "auth_ok",
        ):
            return
        if msg_type == "replay_complete":
            try:
                replay_through = int(
                    data.get("replayThroughSeq", data.get("lastSeq", 0)) or 0
                )
            except (TypeError, ValueError):
                replay_through = 0
            if replay_through > 0:
                await self._cache.advance_cursor(replay_through)
            return
        if msg_type == "replay_gap":
            logger.error(
                "Colony event replay has a retention gap: requested after "
                "seq=%s, first available seq=%s",
                data.get("requestedAfterSeq"),
                data.get("firstAvailableSeq"),
            )
            return
        if msg_type == "replay_reset":
            logger.warning(
                "Colony event replay cursor reset: requested seq=%s, "
                "journal high-water seq=%s",
                data.get("requestedAfterSeq"),
                data.get("journalHighWaterSeq"),
            )
            return
        if msg_type == "replay_integrity_warning":
            logger.error(
                "Colony event replay skipped %s corrupt journal records",
                data.get("corruptRecordCount"),
            )
            return
        if msg_type == "replay_error":
            logger.error("Colony event replay failed: %s", data.get("reason"))
            return
        if msg_type == "stream_overflow":
            logger.warning(
                "Colony event stream overflowed; reconnecting from seq=%s "
                "(server dropped %s frames, seq %s..%s)",
                self._cache.last_event_seq,
                data.get("droppedCount"),
                data.get("firstDroppedSeq"),
                data.get("lastDroppedSeq"),
            )
            return

        payload = data.get("payload", data)
        try:
            seq = int(data.get("seq", 0))
        except (TypeError, ValueError):
            seq = 0
        occurred_at = data.get("occurred_at", data.get("recordedAt", ""))
        recorded_at = data.get("recordedAt", occurred_at)

        event = CachedEvent(
            type=msg_type,
            payload=payload,
            occurred_at=occurred_at,
            seq=seq,
            recorded_at=recorded_at,
        )
        if await self._cache.add(event):
            logger.debug("Colony event cached: %s (seq=%d)", msg_type, seq)

    def get_proactive_events(self) -> list[CachedEvent]:
        """Return cached proactive events suitable for LLM injection.

        Runs the sync portion of the cache lookup.
        """
        # Best-effort synchronous read: if an event loop is running,
        # schedule the coroutine and return empty (next turn will pick it up).
        try:
            loop = asyncio.get_running_loop()
            # We can't block; return empty and the async pre_llm_call will catch it.
            return []
        except RuntimeError:
            pass

        # No running loop — safe to create one for a quick read
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self._cache.get_all())
        finally:
            loop.close()
