"""Layer 7 — Send delay with async cancellation window."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Optional


@dataclass
class DelayResult:
    cancelled: bool
    pending_id: Optional[str] = None
    cancel_reason: Optional[str] = None


class PendingDispatch:
    """Tracks a pending dispatch that can be cancelled during the delay window."""

    def __init__(self, dispatch_id: str, payload) -> None:
        self.dispatch_id = dispatch_id
        self.payload = payload
        self.cancelled = False
        self._cancel_reason: Optional[str] = None

    def cancel(self, reason: str = "manual_cancel") -> None:
        self.cancelled = True
        self._cancel_reason = reason

    @property
    def cancel_reason(self) -> Optional[str]:
        return self._cancel_reason


class PendingDispatchStore:
    """In-memory store for pending dispatches during the delay window."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingDispatch] = {}

    def register(self, dispatch_id: str, payload) -> "PendingDispatchContext":
        pending = PendingDispatch(dispatch_id, payload)
        self._pending[dispatch_id] = pending
        return PendingDispatchContext(self, pending)

    def get(self, dispatch_id: str) -> Optional[PendingDispatch]:
        return self._pending.get(dispatch_id)

    def unregister(self, dispatch_id: str) -> None:
        self._pending.pop(dispatch_id, None)

    def cancel(self, dispatch_id: str, reason: str = "external_cancel") -> bool:
        """Cancel a pending dispatch. Returns True if found and cancelled."""
        pending = self._pending.get(dispatch_id)
        if pending:
            pending.cancel(reason)
            return True
        return False


class PendingDispatchContext:
    """Async context manager for a pending dispatch registration."""

    def __init__(self, store: PendingDispatchStore, pending: PendingDispatch) -> None:
        self._store = store
        self._pending = pending

    async def __aenter__(self) -> PendingDispatch:
        return self._pending

    async def __aexit__(self, *_exc) -> None:
        self._store.unregister(self._pending.dispatch_id)


# Module-level shared store (can be replaced for testing)
_default_store = PendingDispatchStore()


class SendDelayGate:
    """Layer 7 — Send delay with cancellation window."""

    def __init__(self, config=None, dispatch_store: Optional[PendingDispatchStore] = None) -> None:
        self._config = config
        self._store = dispatch_store or _default_store

    async def hold(self, payload) -> DelayResult:
        delay = getattr(self._config, "send_delay_seconds", 0.0) if self._config else 0.0
        pending_id = str(uuid.uuid4())

        async with self._store.register(pending_id, payload) as pending:
            if delay > 0:
                await asyncio.sleep(delay)
            if pending.cancelled:
                return DelayResult(
                    cancelled=True,
                    pending_id=pending_id,
                    cancel_reason=pending.cancel_reason or "PENDING_DELAY_CANCEL",
                )
            return DelayResult(cancelled=False, pending_id=pending_id)
