"""Quiet-hours window arithmetic, shared by every delivery-gating call site.

One predicate, minute precision, half-open ``[start, end)`` semantics:

  - a window spanning midnight (``start > end``, e.g. 22:00-08:00) wraps;
  - ``start == end`` (including 00:00-00:00) means quiet hours are disabled.

Callers keep their own configuration surface (HH:MM strings on the autonomy
loop, integer hours on the delivery rate limiter) and their own timezone
resolution; they reduce "now" to minutes-since-midnight in the owner's local
timezone and ask here. Duplicated implementations had already drifted once
(hour vs minute precision); this is the single copy.
"""

from __future__ import annotations


def in_quiet_window(current_minutes: int, start_minutes: int,
                    end_minutes: int) -> bool:
    """True when ``current_minutes`` (since local midnight) falls inside the
    quiet window ``[start_minutes, end_minutes)``."""
    if start_minutes == end_minutes:
        return False  # zero-length window: quiet hours disabled
    if start_minutes > end_minutes:  # spans midnight (e.g. 22:00-08:00)
        return (current_minutes >= start_minutes
                or current_minutes < end_minutes)
    return start_minutes <= current_minutes < end_minutes
