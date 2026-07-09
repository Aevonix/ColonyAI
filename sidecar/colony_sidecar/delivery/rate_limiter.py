"""DeliveryRateLimiter — per-person rate limiting for proactive messages.

Rules:
  - Max 3 proactive messages per day per person
  - No proactive messages between 22:00–08:00 local time (unless critical)
  - At least 2 hours between proactive messages to the same person
  - User can disable proactive messaging entirely (checked externally)

Counts can optionally be persisted to a small SQLite file so restarts don't
let a crashloop bypass the daily cap. Persistence is opt-in via ``db_path``;
when ``db_path`` is None the limiter stays purely in-memory (used in tests).
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from colony_sidecar.util.quiet_hours import in_quiet_window

logger = logging.getLogger(__name__)

_MAX_PER_DAY = 3
_COOLDOWN_HOURS = 2
_QUIET_START_HOUR = 22
_QUIET_END_HOUR = 8


def _resolve_tz():
    """Owner-local timezone for quiet hours: COLONY_TIMEZONE, else system, else UTC."""
    import os
    name = os.environ.get("COLONY_TIMEZONE", "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            logger.warning("rate_limiter: invalid COLONY_TIMEZONE %r, using system tz", name)
    try:
        local = datetime.now().astimezone().tzinfo
        if local is not None:
            return local
    except Exception:
        pass
    return timezone.utc
# Deliveries older than this many days are pruned at startup; nothing in the
# rate-limit logic needs more than ~48h of history.
_HISTORY_RETENTION_DAYS = 3


class DeliveryRateLimiter:
    """Per-person rate limiter for proactive message delivery.

    Tracks delivery timestamps in memory for fast ``can_deliver`` checks, and
    (optionally) mirrors them to a SQLite log so counts survive restarts.
    """

    def __init__(
        self,
        max_per_day: int = _MAX_PER_DAY,
        cooldown_hours: int = _COOLDOWN_HOURS,
        quiet_start_hour: int = _QUIET_START_HOUR,
        quiet_end_hour: int = _QUIET_END_HOUR,
        metrics: Optional[Any] = None,
        db_path: Optional[Path] = None,
        cap_provider: Optional[Any] = None,
    ) -> None:
        self._max_per_day = max_per_day
        # Adaptive cap (Amendment 1.6): an optional callable(base:int)->int
        # (the trust engine) that EARNS the daily cap upward with a proven
        # delivery track record. Bounded by the provider; base is the floor
        # semantics owners already know. Failure falls back to base.
        self._cap_provider = cap_provider
        self._cooldown = timedelta(hours=cooldown_hours)
        self._quiet_start = quiet_start_hour
        self._quiet_end = quiet_end_hour
        self._metrics = metrics
        # Quiet hours + the daily bucket are evaluated in the OWNER's local
        # timezone (22:00-08:00 must mean their night, not a UTC window).
        self._tz = _resolve_tz()

        # person_id → list of delivery timestamps (UTC) today
        self._daily_counts: Dict[str, List[datetime]] = defaultdict(list)
        # person_id → last delivery timestamp (UTC)
        self._last_delivery: Dict[str, Optional[datetime]] = defaultdict(lambda: None)
        self._today = datetime.now(self._tz).date()

        # Frustration back-off: person_id → probability (updated by autonomy loop)
        self._frustration_cache: Dict[str, float] = {}

        # Optional persistence. Failing to init the DB must not take the
        # limiter offline — fall back to pure in-memory with a warning.
        self._db_path: Optional[Path] = Path(db_path) if db_path else None
        if self._db_path is not None:
            try:
                self._init_db()
                self._reload_from_db()
            except Exception as exc:
                logger.warning(
                    "rate_limiter: persistence disabled — %s: %s",
                    type(exc).__name__, exc,
                )
                self._db_path = None

    def can_deliver(self, person_id: str, urgency: float = 0.5) -> tuple[bool, str]:
        """Check if a proactive message can be delivered to person_id.

        Args:
            person_id: Recipient identifier
            urgency: Message urgency (0-1). Messages with urgency >= 0.9 bypass quiet hours.

        Returns:
            (allowed: bool, reason: str)
        """
        self._reset_if_new_day()

        # Quiet hours check (bypassed for critical messages)
        if urgency < 0.9 and self._in_quiet_hours():
            return False, "quiet_hours"

        # Frustration back-off (bypassed for critical messages)
        if urgency < 0.9:
            frust = self._frustration_cache.get(person_id, 0.0)
            if frust > 0.5:
                if self._metrics is not None:
                    try:
                        self._metrics.record_sentiment_suppression()
                    except Exception:
                        pass
                return False, f"person_frustrated (p={frust:.0%})"

        # Daily limit (adaptive when a trust cap provider is wired)
        effective_max = self._max_per_day
        if self._cap_provider is not None:
            try:
                effective_max = max(1, int(self._cap_provider(self._max_per_day)))
            except Exception:
                effective_max = self._max_per_day
        count = len(self._daily_counts[person_id])
        if count >= effective_max:
            return False, f"daily_limit_reached ({count}/{effective_max})"

        # Cooldown
        last = self._last_delivery[person_id]
        if last is not None:
            elapsed = datetime.now(timezone.utc) - last
            if elapsed < self._cooldown:
                remaining = int((self._cooldown - elapsed).total_seconds()) // 60
                return False, f"cooldown_active ({remaining}m remaining)"

        return True, "ok"

    def record_delivery(self, person_id: str) -> None:
        """Mark that a proactive message was successfully delivered."""
        self._reset_if_new_day()
        now = datetime.now(timezone.utc)
        self._daily_counts[person_id].append(now)
        self._last_delivery[person_id] = now
        if self._db_path is not None:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    conn.execute(
                        "INSERT INTO delivery_log (person_id, delivered_at) VALUES (?, ?)",
                        (person_id, now.isoformat()),
                    )
            except Exception as exc:
                logger.warning(
                    "rate_limiter: failed to persist delivery for %s: %s",
                    person_id, exc,
                )
        logger.debug(
            "Delivery recorded for %s (count today: %d)",
            person_id,
            len(self._daily_counts[person_id]),
        )

    def update_frustration(self, person_id: str, probability: float) -> None:
        """Update cached frustration probability for a person.

        Called by the autonomy loop after running the state estimator.
        Values above 0.5 will suppress non-critical deliveries.
        """
        self._frustration_cache[person_id] = probability

    def daily_count(self, person_id: str) -> int:
        """Return number of proactive deliveries today for a person."""
        self._reset_if_new_day()
        return len(self._daily_counts[person_id])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reset_if_new_day(self) -> None:
        """Reset daily counters when the UTC date rolls over."""
        today = datetime.now(self._tz).date()
        if today != self._today:
            self._daily_counts.clear()
            self._today = today

    def _in_quiet_hours(self) -> bool:
        """Return True if current time is within quiet hours (owner-local tz)."""
        now = datetime.now(self._tz)
        return in_quiet_window(
            now.hour * 60 + now.minute,
            self._quiet_start * 60,
            self._quiet_end * 60,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create the delivery-log table on first use and prune stale rows."""
        assert self._db_path is not None
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS delivery_log ("
                "  person_id TEXT NOT NULL,"
                "  delivered_at TEXT NOT NULL"
                ")"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_delivery_person_date "
                "ON delivery_log(person_id, delivered_at)"
            )
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=_HISTORY_RETENTION_DAYS)
            ).isoformat()
            conn.execute(
                "DELETE FROM delivery_log WHERE delivered_at < ?", (cutoff,),
            )

    def _reload_from_db(self) -> None:
        """Reload today's counts and the most-recent delivery per person."""
        assert self._db_path is not None
        # "Today" is the OWNER-LOCAL date (matching _reset_if_new_day), so
        # its start is local midnight converted to UTC — the log stores UTC
        # timestamps. Combining the local date with UTC midnight shifted the
        # reload window by the UTC offset, over- or under-counting restarts.
        today_start = datetime.combine(
            self._today, datetime.min.time(), tzinfo=self._tz
        ).astimezone(timezone.utc).isoformat()
        with sqlite3.connect(self._db_path) as conn:
            # Today's deliveries → daily count
            cur = conn.execute(
                "SELECT person_id, delivered_at FROM delivery_log "
                "WHERE delivered_at >= ? ORDER BY delivered_at",
                (today_start,),
            )
            for person_id, ts_str in cur:
                self._daily_counts[person_id].append(datetime.fromisoformat(ts_str))

            # Most recent delivery per person → cooldown state (spans day boundary)
            cur = conn.execute(
                "SELECT person_id, MAX(delivered_at) FROM delivery_log "
                "GROUP BY person_id"
            )
            for person_id, ts_str in cur:
                if ts_str:
                    self._last_delivery[person_id] = datetime.fromisoformat(ts_str)
