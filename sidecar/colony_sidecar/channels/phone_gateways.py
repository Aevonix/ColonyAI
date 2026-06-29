"""Shared phone gateway resolution.

Replaces the three hardcoded _PHONE_GATEWAYS tuples scattered across
contacts/store.py, identity/resolver.py, and contacts/world_bridge.py.

Reads from the channel store when available; falls back to a built-in
set so the system works before any channels are registered.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_BUILTIN_PHONE_GATEWAYS = frozenset({
    "imessage", "sms", "signal", "whatsapp",
})

_channel_store_ref: Optional[object] = None


def set_channel_store_ref(store) -> None:
    global _channel_store_ref
    _channel_store_ref = store


def get_phone_gateways() -> frozenset[str]:
    """Return the set of gateways that use phone-number identity.

    When a channel store is available, returns the union of registered
    channels with phone_identity_unification=True and the built-in set.
    Without a channel store, returns only the built-in set.
    """
    if _channel_store_ref is not None:
        try:
            registered = _channel_store_ref.get_phone_gateways()
            return frozenset(registered | _BUILTIN_PHONE_GATEWAYS)
        except Exception:
            logger.debug("Channel store query failed, using built-in set", exc_info=True)
    return _BUILTIN_PHONE_GATEWAYS
