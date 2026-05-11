"""WebSocket event subscriber for Colony sidecar.

Maintains a persistent WebSocket connection to Colony's /v1/host/events
and caches the most recent events for injection into Hermes turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import websockets

logger = logging.getLogger(__name__)


@dataclass
class CachedEvent:
    type: str
    payload: dict[str, Any]
    occurred_at: str
    seq: int


class EventCache:
    """Thread-safe cache of recent Colony events."""

    def __init__(self, max_per_type: int = 5):
        self._lock = asyncio.Lock()
        self._events: dict[str, list[CachedEvent]] = {}
        self._max_per_type = max_per_type
        self._last_seq = 0
        self._last_event_id = ""

    async def add(self, event: CachedEvent) -> None:
        async with self._lock:
            self._events.setdefault(event.type, [])
            self._events[event.type].insert(0, event)
            self._events[event.type] = self._events[event.type][: self._max_per_type]
            if event.seq > self._last_seq:
                self._last_seq = event.seq
                self._last_event_id = event.occurred_at

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

    @property
    def last_event_id(self) -> str:
        return self._last_event_id


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

        async with websockets.connect(ws_url, extra_headers=headers) as ws:
            # Auth handshake
            await ws.send(json.dumps({
                "type": "auth",
                "token": self._api_key,
                "contact_id": self._contact_id,
                "lastEventId": self._cache.last_event_id,
            }))

            # Wait for replay complete or timeout
            replay_done = False
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                data = json.loads(msg)
                if data.get("type") == "replay_complete":
                    replay_done = True
            except asyncio.TimeoutError:
                pass

            # Listen loop
            while not self._stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    data = json.loads(msg)
                    await self._handle_message(data)
                except asyncio.TimeoutError:
                    # Send ping to keepalive
                    await ws.send(json.dumps({"type": "ping"}))
                except websockets.exceptions.ConnectionClosed:
                    break

    async def _handle_message(self, data: dict[str, Any]) -> None:
        msg_type = data.get("type", "")
        if msg_type in ("ping", "pong", "replay_complete"):
            return

        payload = data.get("payload", data)
        seq = data.get("seq", 0)
        occurred_at = data.get("occurred_at", data.get("recordedAt", ""))

        event = CachedEvent(
            type=msg_type,
            payload=payload,
            occurred_at=occurred_at,
            seq=seq,
        )
        await self._cache.add(event)
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
