"""Bounded buffering primitives for the host event WebSocket stream."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_OVERFLOW = object()


class EventSubscriberBuffer:
    """A bounded per-subscriber queue with explicit overflow state.

    Once a subscriber falls behind, queued frames and subsequent frames are no
    longer offered piecemeal.  A single overflow sentinel wakes the socket
    writer, which reports the exact dropped sequence range and closes the
    connection.  The client can then replay from its last processed sequence.
    """

    def __init__(
        self,
        maxsize: int,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        if maxsize < 1:
            raise ValueError("event subscriber queue size must be positive")
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self.loop = loop
        self.active = True
        self.overflowed = False
        self.dropped_count = 0
        self.first_dropped_seq: Optional[int] = None
        self.last_dropped_seq: Optional[int] = None
        self.last_delivered_seq = 0

    def publish(self, event: dict[str, Any]) -> None:
        """Offer an event on the subscriber's owning event loop."""
        if not self.active:
            return
        if self.loop is None:
            self._publish_on_loop(event)
            return
        if not self.loop.is_running():
            self.close()
            logger.warning("Discarding stale event subscriber with stopped loop")
            return
        try:
            # Schedule every publication, including same-loop calls. This
            # preserves the host's sequence order when a worker thread has
            # already scheduled an earlier frame on this loop.
            self.loop.call_soon_threadsafe(self._publish_on_loop, event)
        except RuntimeError:
            self.close()
            logger.warning("Discarding stale event subscriber with closed loop")

    def _note_dropped(self, event: dict[str, Any]) -> None:
        self.dropped_count += 1
        try:
            seq = int(event.get("seq", 0))
        except (TypeError, ValueError):
            seq = 0
        if seq > 0:
            if self.first_dropped_seq is None:
                self.first_dropped_seq = seq
            else:
                self.first_dropped_seq = min(self.first_dropped_seq, seq)
            if self.last_dropped_seq is None:
                self.last_dropped_seq = seq
            else:
                self.last_dropped_seq = max(self.last_dropped_seq, seq)

    def _publish_on_loop(self, event: dict[str, Any]) -> None:
        if not self.active:
            return
        if self.overflowed:
            self._note_dropped(event)
            return
        try:
            self.queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            self.overflowed = True

        # Everything not yet delivered must be replayed, including the frame
        # which first exceeded the bound.
        while True:
            try:
                queued = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(queued, dict):
                self._note_dropped(queued)
        self._note_dropped(event)
        self.queue.put_nowait(_OVERFLOW)
        logger.warning(
            "Event subscriber overflowed: dropped=%d first_seq=%s last_seq=%s",
            self.dropped_count,
            self.first_dropped_seq,
            self.last_dropped_seq,
        )

    async def get(self) -> Any:
        return await self.queue.get()

    @staticmethod
    def is_overflow(item: Any) -> bool:
        return item is _OVERFLOW

    def mark_delivered(self, event: dict[str, Any]) -> None:
        try:
            seq = int(event.get("seq", 0))
        except (TypeError, ValueError):
            return
        if seq > self.last_delivered_seq:
            self.last_delivered_seq = seq

    def overflow_frame(self) -> dict[str, Any]:
        return {
            "type": "stream_overflow",
            "droppedCount": self.dropped_count,
            "firstDroppedSeq": self.first_dropped_seq,
            "lastDroppedSeq": self.last_dropped_seq,
            "resumeAfterSeq": self.last_delivered_seq,
        }

    def close(self) -> None:
        self.active = False
