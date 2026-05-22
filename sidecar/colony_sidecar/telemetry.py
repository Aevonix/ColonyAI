"""Sidecar telemetry store for temporal health monitoring."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


@dataclass
class TelemetryStore:
    started_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    last_tick_at: Optional[datetime] = None
    last_initiative_at: Optional[datetime] = None
    last_prefetch_at: Optional[datetime] = None
    last_aeva_outreach_at: Optional[datetime] = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def touch(self, key: str) -> None:
        async with self._lock:
            setattr(self, key, datetime.now(timezone.utc))

    async def silence_hours(self, key: str) -> Optional[float]:
        """Return hours since the last event of the given type."""
        attr_map = {
            "sync": "last_sync_at",
            "tick": "last_tick_at",
            "initiative": "last_initiative_at",
            "prefetch": "last_prefetch_at",
        }
        attr = attr_map.get(key, key)
        async with self._lock:
            ts = getattr(self, attr, None)
            if ts is None:
                return None
            return (datetime.now(timezone.utc) - ts).total_seconds() / 3600

    async def stale_flags(self, thresholds: Dict[str, float]) -> List[str]:
        flags = []
        for key, threshold in thresholds.items():
            silence = await self.silence_hours(key)
            if silence is not None and silence > threshold:
                flags.append(key)
        return flags

    async def to_dict(self, thresholds: Dict[str, float]) -> dict:
        started = self.started_at.isoformat() if self.started_at else None
        sync_at = self.last_sync_at.isoformat() if self.last_sync_at else None
        tick_at = self.last_tick_at.isoformat() if self.last_tick_at else None
        init_at = self.last_initiative_at.isoformat() if self.last_initiative_at else None
        prefetch_at = self.last_prefetch_at.isoformat() if self.last_prefetch_at else None
        outreach_at = self.last_aeva_outreach_at.isoformat() if self.last_aeva_outreach_at else None
        silence = {}
        for key in thresholds:
            silence[key] = await self.silence_hours(key)
        flags = await self.stale_flags(thresholds)
        return {
            "started_at": started,
            "last_sync_at": sync_at,
            "last_tick_at": tick_at,
            "last_initiative_at": init_at,
            "last_prefetch_at": prefetch_at,
            "last_aeva_outreach_at": outreach_at,
            "silence_hours": silence,
            "stale_flags": flags,
        }
