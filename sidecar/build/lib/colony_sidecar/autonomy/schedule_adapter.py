"""ScheduleAdapter — wire MetaLearner patterns to cron job schedules.

Reads pending behavioral patterns from MetaLearner and applies schedule
adjustments to cron jobs. Each adjustment is bounded and logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class PatternSignal:
    """A learned behavioral pattern that may warrant a schedule change.

    Attributes:
        signal_type: What was observed (e.g., "briefing_open", "response_spike")
        peak_time: Local time of observed peak (HH:MM format)
        job_id: Which cron job to adjust
        confidence: How confident the learner is (0–1)
        direction: "earlier" or "later" relative to current schedule
        delta_minutes: How many minutes to shift
    """

    signal_type: str
    peak_time: str
    job_id: str
    confidence: float
    direction: str  # "earlier" | "later"
    delta_minutes: int


class ScheduleAdapter:
    """Wire MetaLearner patterns to cron job schedules.

    Reads patterns above a confidence threshold from the MetaLearner
    and applies schedule adjustments to cron jobs. Each adjustment is
    logged and bounded — the adapter won't shift a briefing by more
    than max_shift_minutes in a single adjustment.

    Args:
        meta_learner: Colony MetaLearner instance
        cron_store: cron.jobs module (or compatible interface)
        min_confidence: Minimum confidence to apply an adjustment
        max_shift_minutes: Maximum shift per adjustment (default 30)
    """

    def __init__(
        self,
        meta_learner: Any,
        cron_store: Any,
        min_confidence: float = 0.75,
        max_shift_minutes: int = 30,
    ) -> None:
        self._learner = meta_learner
        self._cron = cron_store
        self.min_confidence = min_confidence
        self.max_shift_minutes = max_shift_minutes

    def apply_patterns(self) -> int:
        """Read pending patterns and apply schedule adjustments.

        Returns the number of adjustments made.
        """
        if not hasattr(self._learner, "get_pending_patterns"):
            return 0

        patterns = self._learner.get_pending_patterns(min_confidence=self.min_confidence)
        applied = 0

        for pattern in patterns:
            try:
                adjusted = self._apply_single(pattern)
                if adjusted:
                    self._learner.mark_pattern_applied(pattern.signal_type, pattern.job_id)
                    applied += 1
            except Exception as exc:
                logger.warning("Failed to apply pattern %r: %s", pattern.signal_type, exc)

        return applied

    def _apply_single(self, pattern: PatternSignal) -> bool:
        """Apply a single pattern to a cron job schedule.

        Parses the job's current cron expression, applies the delta
        (bounded by max_shift_minutes), and writes the new expression.
        Returns True if a change was made.
        """
        job = self._cron.get_job(pattern.job_id)
        if not job:
            logger.debug("Pattern references unknown job %r — skipping", pattern.job_id)
            return False

        delta = min(abs(pattern.delta_minutes), self.max_shift_minutes)
        if pattern.direction == "earlier":
            delta = -delta

        current_cron = job.get("schedule", "")
        new_cron = shift_cron_minutes(current_cron, delta)

        if new_cron == current_cron:
            return False

        self._cron.update_job(pattern.job_id, {"schedule": new_cron})
        logger.info(
            "Schedule adjusted: job=%r %r → %r (confidence=%.2f, delta=%+dm)",
            pattern.job_id,
            current_cron,
            new_cron,
            pattern.confidence,
            delta,
        )
        return True


def shift_cron_minutes(cron_expr: str, delta_minutes: int) -> str:
    """Shift the minute field of a cron expression by delta_minutes.

    Only modifies simple expressions where minute and hour are integers.
    Returns the original expression unchanged if it can't be parsed safely.

    Example: "30 7 * * *" shifted by -10 → "20 7 * * *"

    Clamped to [0, 1439] — does not cross day boundaries.
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return cron_expr

    try:
        minute = int(parts[0])
        hour = int(parts[1])
    except ValueError:
        return cron_expr  # Has wildcards or ranges — don't touch

    total_minutes = hour * 60 + minute + delta_minutes
    # Clamp to [0, 1439] — don't cross day boundaries
    total_minutes = max(0, min(1439, total_minutes))

    new_hour, new_minute = divmod(total_minutes, 60)
    parts[0] = str(new_minute)
    parts[1] = str(new_hour)
    return " ".join(parts)
