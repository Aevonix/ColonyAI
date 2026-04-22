"""Session TTL configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionTTLConfig:
    contact_session_ttl_hours: int = 24       # human-contact sessions
    api_session_ttl_hours: int = 1            # machine/API sessions
    websocket_idle_ttl_minutes: int = 30      # WebSocket idle timeout
    cleanup_interval_minutes: int = 15        # background sweep frequency
