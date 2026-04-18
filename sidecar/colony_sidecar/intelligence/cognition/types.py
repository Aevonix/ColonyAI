"""Shared types for the cognition package.

Centralising enums and dataclasses here breaks the circular dependency between
metalearner and gap_detector (both previously importing from each other).
"""

from __future__ import annotations

from enum import Enum


class GapSeverity(str, Enum):
    """Severity levels for cognitive performance gaps."""
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"
