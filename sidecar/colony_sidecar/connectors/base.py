"""Connector framework base types (cognition item 2, Phase C).

A connector is a READ-ONLY sense: it polls an external source on its own
cadence and normalizes what it sees into Observations. Everything downstream
(the observation store that feeds the initiative engine, the world-model
populator, belief maintenance) consumes the same normalized shape, so a new
source is just a new poll+normalize -- no bespoke ingest path.

All configuration comes from the environment (per-connector
COLONY_CONNECTOR_<NAME>_<KEY>); no credentials ever live in code or tests.
Push-style ingress is intentionally NOT built here: the host framework's
webhook adapter already handles HMAC-authenticated inbound POSTs. These are the
pull-style connectors (IMAP, calendar, filesystem, metrics pull).
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EntityHint:
    """A structured entity a connector recognized in an observation."""
    kind: str                       # person | company | event | document | project | ...
    name: str
    external_ids: Dict[str, str] = field(default_factory=dict)


@dataclass
class Observation:
    """The normalized unit every connector emits."""
    domain: str                     # email | calendar | document | metrics | ...
    external_id: str                # stable id within the domain (dedup key)
    ts: float                       # source event time (epoch seconds)
    payload: Dict[str, Any] = field(default_factory=dict)
    entities: List[EntityHint] = field(default_factory=list)
    text: str = ""                  # natural-language render for world extraction

    def to_store_row(self) -> Dict[str, Any]:
        """Shape for ObservationStore.record_batch (one snapshot per entity)."""
        return {"entity_id": self.external_id,
                "payload": {**self.payload, "_entities": [
                    {"kind": e.kind, "name": e.name, "external_ids": e.external_ids}
                    for e in self.entities], "_text": self.text},
                "observed_at": _iso(self.ts)}


def _iso(ts: float):
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return datetime.now(timezone.utc).isoformat()


class ConnectorConfig:
    """Config for one connector, resolved env-first then secrets-store.

    Precedence: COLONY_CONNECTOR_<NAME>_<KEY> env (an explicit deployment
    override always wins) → the Colony secrets store at
    ``connector/<name>/<key>`` → default. The secrets fallback means a
    credential (IMAP password, ICS URL) is entered ONCE via the secrets
    API/CLI, lives encrypted in the store, and survives service redeploys
    without ever touching a plist or unit file."""

    def __init__(self, name: str) -> None:
        self._name = name.lower()
        self._prefix = f"COLONY_CONNECTOR_{name.upper()}_"

    def get(self, key: str, default: str = "") -> str:
        env = os.environ.get(self._prefix + key.upper())
        if env:
            return env
        try:
            from colony_sidecar.api.routers import host as _h
            mgr = getattr(_h, "_secrets_manager", None)
            if mgr is not None:
                v = mgr.get(f"connector/{self._name}/{key.lower()}")
                if v is not None:
                    return v
        except Exception:
            pass
        return default

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get(key, str(default)))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key, "1" if default else "0").strip().lower()
        return v in ("1", "true", "yes", "on")


class Connector(ABC):
    """Read-only pull connector. Subclasses implement fetch + normalize."""

    name: str = "connector"
    domain: str = "generic"
    default_poll_secs: int = 900

    def __init__(self) -> None:
        self.config = ConnectorConfig(self.name)
        self._last_poll: float = 0.0

    # -- lifecycle knobs (env-driven) ------------------------------------
    @property
    def enabled(self) -> bool:
        return self.config.get_bool("ENABLED", False)

    @property
    def poll_secs(self) -> int:
        return self.config.get_int("POLL_SECS", self.default_poll_secs)

    def due(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self._last_poll) >= self.poll_secs

    def mark_polled(self, now: Optional[float] = None) -> None:
        self._last_poll = now if now is not None else time.time()

    # -- the contract -----------------------------------------------------
    @abstractmethod
    def poll(self) -> List[Observation]:
        """Fetch from the source and return normalized Observations.

        Must be side-effect free beyond reading. Never raises past its own
        try/except; returns [] on any failure so one bad source cannot stall
        the manager.
        """
        ...
