"""DeliveryRateLimiter — per-person rate limiting for proactive messages.

Rules:
  - Max 3 proactive messages per day per person
  - No proactive messages between 22:00–08:00 local time (unless critical)
  - At least 2 hours between proactive messages to the same person
  - User can disable proactive messaging entirely (checked externally)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_PER_DAY = 3
_COOLDOWN_HOURS = 2
_QUIET_START_HOUR = 22
_QUIET_END_HOUR = 8


class DeliveryRateLimiter:
    """Per-person rate limiter for proactive message delivery.

    Tracks delivery timestamps in memory. Resets daily counters automatically.
    Not persistent across restarts — conservative design: losing counts on restart
    means we may send slightly more than the daily limit, which is acceptable.
    """

    def __init__(
        self,
        max_per_day: int = _MAX_PER_DAY,
        cooldown_hours: int = _COOLDOWN_HOURS,
        quiet_start_hour: int = _QUIET_START_HOUR,
        quiet_end_hour: int = _QUIET_END_HOUR,
        metrics: Optional[Any] = None,
    ) -> None:
        self._max_per_day = max_per_day
        self._cooldown = timedelta(hours=cooldown_hours)
        self._quiet_start = quiet_start_hour
        self._quiet_end = quiet_end_hour
        self._metrics = metrics

        # person_id → list of delivery timestamps (UTC) today
        self._daily_counts: Dict[str, List[datetime]] = defaultdict(list)
        # person_id → last delivery timestamp (UTC)
        self._last_delivery: Dict[str, Optional[datetime]] = defaultdict(lambda: None)
        self._today = datetime.now(timezone.utc).date()

        # Frustration back-off: person_id → probability (updated by autonomy loop)
        self._frustration_cache: Dict[str, float] = {}

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

        # Daily limit
        count = len(self._daily_counts[person_id])
        if count >= self._max_per_day:
            return False, f"daily_limit_reached ({count}/{self._max_per_day})"

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
        today = datetime.now(timezone.utc).date()
        if today != self._today:
            self._daily_counts.clear()
            self._today = today

    def _in_quiet_hours(self) -> bool:
        """Return True if current time is within quiet hours."""
        now = datetime.now(timezone.utc)
        h = now.hour
        if self._quiet_start > self._quiet_end:
            # Spans midnight (22:00–08:00)
            return h >= self._quiet_start or h < self._quiet_end
        return self._quiet_start <= h < self._quiet_end
