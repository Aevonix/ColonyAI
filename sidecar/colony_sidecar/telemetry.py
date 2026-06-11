"""Sidecar telemetry store for temporal health monitoring."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# v0.21.0 — persisted so last_*_at survive a restart (previously in-memory only,
# which reset silence/last-outreach reasoning to null on every restart).
_PERSIST_KEYS = (
    "started_at", "last_sync_at", "last_tick_at",
    "last_initiative_at", "last_prefetch_at", "last_agent_outreach_at",
)


def _telemetry_path() -> Path:
    return Path(os.environ.get("COLONY_STATE_DIR", os.path.expanduser("~/.colony"))) / "telemetry.json"


@dataclass
class TelemetryStore:
    started_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    last_tick_at: Optional[datetime] = None
    last_initiative_at: Optional[datetime] = None
    last_prefetch_at: Optional[datetime] = None
    last_agent_outreach_at: Optional[datetime] = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def load(self) -> None:
        """Restore persisted timestamps (except started_at) across restart."""
        try:
            data = json.loads(_telemetry_path().read_text())
        except Exception:
            return
        for key in _PERSIST_KEYS:
            if key == "started_at":
                continue  # started_at is set fresh per process
            val = data.get(key)
            if val:
                try:
                    setattr(self, key, datetime.fromisoformat(val))
                except (ValueError, TypeError):
                    pass

    def _persist(self) -> None:
        try:
            p = _telemetry_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {
                k: (getattr(self, k).isoformat() if getattr(self, k) else None)
                for k in _PERSIST_KEYS
            }
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, p)
        except Exception as exc:  # pragma: no cover
            logger.debug("telemetry persist failed: %s", exc)

    async def touch(self, key: str) -> None:
        async with self._lock:
            setattr(self, key, datetime.now(timezone.utc))
        self._persist()

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
        outreach_at = self.last_agent_outreach_at.isoformat() if self.last_agent_outreach_at else None
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
            "last_agent_outreach_at": outreach_at,
            "silence_hours": silence,
            "stale_flags": flags,
        }
