"""Colony Briefing System — scheduling engine.

Provides timezone-aware cron-like scheduling for daily and weekly
briefings, plus event-driven tactical briefing dispatch with rate limiting.
"""

from __future__ import annotations

import logging
import threading
import zoneinfo
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .config import BriefingConfig, TacticalBriefingConfig
from .models import (
    Briefing,
    BriefingPriority,
    BriefingStatus,
    BriefingType,
    ScheduleEntry,
)

logger = logging.getLogger(__name__)

_DAYS_OF_WEEK = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------


def _validate_timezone(tz_name: str) -> str:
    """Validate timezone name; fall back to UTC with a warning if invalid."""
    try:
        zoneinfo.ZoneInfo(tz_name)
        return tz_name
    except Exception:
        logger.warning("Invalid timezone '%s'; falling back to UTC.", tz_name)
        return "UTC"


def local_now(timezone_name: str) -> datetime:
    """Return the current time in the user's configured timezone."""
    tz = zoneinfo.ZoneInfo(timezone_name)
    return datetime.now(tz)


def briefing_is_due(
    scheduled_time: str,
    timezone_name: str,
    last_run: Optional[datetime],
) -> bool:
    """Return True if the briefing is due to run.

    Due conditions:
    - current local time >= scheduled_time
    - last_run is None OR last_run.date() < today's local date
    """
    now = local_now(timezone_name)
    hour, minute = map(int, scheduled_time.split(":"))
    scheduled_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if now < scheduled_today:
        return False

    if last_run is None:
        return True

    last_run_local = last_run.astimezone(zoneinfo.ZoneInfo(timezone_name))
    return last_run_local.date() < now.date()


def weekly_is_due(
    day_name: str,
    scheduled_time: str,
    timezone_name: str,
    last_run: Optional[datetime],
) -> bool:
    """Return True if the weekly briefing is due today at the scheduled time."""
    now = local_now(timezone_name)
    target_weekday = _DAYS_OF_WEEK.get(day_name.lower(), 0)
    if now.weekday() != target_weekday:
        return False
    return briefing_is_due(scheduled_time, timezone_name, last_run)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@dataclass
class BriefingRateLimits:
    """Track delivery counts for rate limiting."""

    delivered_this_hour: int = 0
    delivered_today: int = 0
    last_tactical_by_source: Dict[str, datetime] = field(default_factory=dict)
    _hour_reset_at: Optional[datetime] = field(default=None, init=False, repr=False)
    _day_reset_at: Optional[datetime] = field(default=None, init=False, repr=False)

    def _refresh(self) -> None:
        now = datetime.now(timezone.utc)
        if self._hour_reset_at is None:
            # Initialize sentinel without resetting any externally-set counter
            self._hour_reset_at = now
        elif (now - self._hour_reset_at).total_seconds() >= 3600:
            self.delivered_this_hour = 0
            self._hour_reset_at = now
        if self._day_reset_at is None:
            self._day_reset_at = now
        elif (now - self._day_reset_at).total_seconds() >= 86400:
            self.delivered_today = 0
            self._day_reset_at = now

    def can_deliver(
        self,
        briefing_type: BriefingType,
        source: Optional[str],
        config: TacticalBriefingConfig,
        priority: BriefingPriority,
    ) -> bool:
        self._refresh()
        if priority == BriefingPriority.URGENT:
            return True
        if briefing_type == BriefingType.TACTICAL:
            if self.delivered_this_hour >= config.max_per_hour:
                return False
            if self.delivered_today >= config.max_per_day:
                return False
            if source and source in self.last_tactical_by_source:
                elapsed = (
                    datetime.now(timezone.utc) - self.last_tactical_by_source[source]
                ).total_seconds()
                if elapsed < config.cooldown_seconds:
                    return False
        return True

    def record_delivery(
        self,
        briefing_type: BriefingType,
        source: Optional[str],
    ) -> None:
        self._refresh()
        if briefing_type == BriefingType.TACTICAL:
            self.delivered_this_hour += 1
            self.delivered_today += 1
            if source:
                self.last_tactical_by_source[source] = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Tactical trigger
# ---------------------------------------------------------------------------

TRIGGER_EVENTS: Set[str] = {
    "anomaly.detected",
    "goal.blocked",
    "goal.critical_failed",
    "system.worker_lost",
    "calendar.prep_needed",
    "relationship.trust_tier_changed",
}

_EVENT_SEVERITY: Dict[str, str] = {
    "anomaly.detected": "warning",
    "goal.blocked": "warning",
    "goal.critical_failed": "critical",
    "system.worker_lost": "critical",
    "calendar.prep_needed": "warning",
    "relationship.trust_tier_changed": "info",
}


class TacticalBriefingTrigger:
    """Subscribe to Colony events and fire tactical briefings as needed."""

    def __init__(
        self,
        engine: Optional[Any] = None,  # BriefingEngine, avoided circular import
        config: Optional[TacticalBriefingConfig] = None,
        rate_limits: Optional[BriefingRateLimits] = None,
    ) -> None:
        self._engine = engine
        self._config = config or TacticalBriefingConfig()
        self._rate_limits = rate_limits or BriefingRateLimits()

    def on_event(self, event_type: str, payload: Dict[str, Any]) -> Optional[Briefing]:
        """Handle a Colony event and fire a tactical briefing if warranted."""
        if event_type not in TRIGGER_EVENTS:
            return None
        if not self._config.enabled:
            return None

        severity = _EVENT_SEVERITY.get(event_type, "info")

        # Check min severity filter
        severity_order = {"info": 0, "warning": 1, "critical": 2}
        if severity_order.get(severity, 0) < severity_order.get(
            self._config.min_severity, 1
        ):
            if not (severity == "critical" and self._config.always_fire_critical):
                return None

        source = payload.get("source", event_type)
        priority = (
            BriefingPriority.URGENT
            if severity == "critical"
            else BriefingPriority.HIGH
            if severity == "warning"
            else BriefingPriority.NORMAL
        )

        if not self._rate_limits.can_deliver(
            BriefingType.TACTICAL, source, self._config, priority
        ):
            logger.info(
                "Tactical briefing rate-limited for event '%s' from source '%s'",
                event_type,
                source,
            )
            return None

        if self._engine is None:
            return None

        briefing = self._engine.fire_tactical(
            trigger=event_type,
            severity=severity,
            summary=payload.get("summary", event_type),
            details=payload.get("details", ""),
            suggested_actions=payload.get("suggested_actions"),
        )
        if briefing:
            self._rate_limits.record_delivery(BriefingType.TACTICAL, source)
        return briefing


# ---------------------------------------------------------------------------
# BriefingScheduler
# ---------------------------------------------------------------------------


class BriefingScheduler:
    """Schedule and dispatch daily and weekly briefings."""

    POLL_INTERVAL_SECONDS = 60

    def __init__(
        self,
        config: BriefingConfig,
        engine: Any,  # BriefingEngine — avoid circular import
        store: Any,  # BriefingStore
    ) -> None:
        self._config = config
        self._engine = engine
        self._store = store
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Validate timezones
        self._config.daily.timezone = _validate_timezone(self._config.daily.timezone)
        self._config.weekly.timezone = _validate_timezone(self._config.weekly.timezone)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the polling loop in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="BriefingScheduler")
        self._thread.start()
        logger.info("BriefingScheduler started.")

    def stop(self) -> None:
        """Stop the polling loop gracefully."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("BriefingScheduler stopped.")

    def check_due(self) -> None:
        """Check whether scheduled briefings are due and deliver pending ones.

        State machine:
            draft → queued → delivering → delivered

        Covers two cases:
        1. Existing DRAFT/QUEUED briefings from previous runs (stuck delivery).
        2. Newly due daily/weekly briefings (per schedule).

        Safe to call from external callers (e.g. AutonomyLoop).
        """
        # Process any briefings already in draft/queued state.
        try:
            self._engine.deliver_pending()
        except Exception as exc:
            logger.error("check_due: deliver_pending failed: %s", exc)

        # Generate new briefings if the schedule says it's time.
        self._check_and_run_daily()
        self._check_and_run_weekly()

    def force_generate(
        self,
        briefing_type: BriefingType,
        options: Optional[Dict[str, Any]] = None,
    ) -> Briefing:
        """Generate and deliver a briefing immediately, bypassing schedule."""
        return self._engine.generate_and_deliver(briefing_type)

    def get_schedule(self) -> List[ScheduleEntry]:
        return self._store.get_schedule()

    def update_schedule_last_run(self, briefing_type: BriefingType) -> None:
        self._store.update_schedule_last_run(briefing_type)

    def count_today(self) -> int:
        return self._store.count_today()

    # ------------------------------------------------------------------
    # Internal polling loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        import time

        while self._running:
            try:
                self.check_due()
            except Exception as exc:
                logger.error("BriefingScheduler poll error: %s", exc)
            time.sleep(self.POLL_INTERVAL_SECONDS)

    def _check_and_run_daily(self) -> None:
        if not self._config.daily.enabled:
            return
        entry = self._store.get_schedule_entry(BriefingType.DAILY)
        last_run = entry.last_run if entry else None
        if briefing_is_due(
            self._config.daily.time,
            self._config.daily.timezone,
            last_run,
        ):
            logger.info("Daily briefing is due — generating.")
            try:
                self._engine.generate_and_deliver(BriefingType.DAILY)
                self._store.update_schedule_last_run(BriefingType.DAILY)
            except Exception as exc:
                logger.error("Failed to generate daily briefing: %s", exc)

    def _check_and_run_weekly(self) -> None:
        if not self._config.weekly.enabled:
            return
        entry = self._store.get_schedule_entry(BriefingType.WEEKLY)
        last_run = entry.last_run if entry else None
        if weekly_is_due(
            self._config.weekly.day,
            self._config.weekly.time,
            self._config.weekly.timezone,
            last_run,
        ):
            logger.info("Weekly briefing is due — generating.")
            try:
                self._engine.generate_and_deliver(BriefingType.WEEKLY)
                self._store.update_schedule_last_run(BriefingType.WEEKLY)
            except Exception as exc:
                logger.error("Failed to generate weekly briefing: %s", exc)
