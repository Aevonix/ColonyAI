"""AutonomyLoop configuration dataclass."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class AutonomyConfig:
    """Configuration for the Colony autonomy loop.

    All time values are in seconds unless noted.
    """

    # How long to sleep between ticks when no events wake the loop early.
    tick_interval_secs: float = 300.0

    # Minimum initiative priority score [0.0–1.0] to execute an action.
    initiative_confidence_threshold: float = 0.7

    # Maximum autonomous actions taken per hour (safety limit).
    max_actions_per_hour: int = 20

    # Suppress non-urgent surfacing during quiet hours ("HH:MM" 24-hour format).
    # Set both to "00:00" to disable quiet hours.
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"

    # Minimum anomaly severity [0.0–1.0] to surface to the initiative engine.
    anomaly_severity_threshold: float = 0.6

    # Minimum prediction confidence [0.0–1.0] to surface predictions.
    prediction_confidence_threshold: float = 0.75

    # Hours after which an active goal is considered stale.
    goal_stale_threshold_hours: float = 24.0

    # How often to run the identity bootstrap self-check (hours).
    bootstrap_check_interval_hours: int = 24

    # How often to run the self-reflection component (days).
    self_reflection_interval_days: int = 7

    # ── class methods ──────────────────────────────────────────────────

    @classmethod
    def from_colony_config(cls, colony_cfg: object) -> "AutonomyConfig":
        """Construct config from a Colony config object or dict.

        Looks for an ``autonomy`` sub-key/attribute. If not found, returns
        defaults. Supports both dict-like (``colony_cfg["autonomy"]``) and
        object-like (``colony_cfg.autonomy``) configs.

        Args:
            colony_cfg: Colony config object/dict, or None for defaults.
        """
        if colony_cfg is None:
            return cls()

        # Extract the autonomy sub-section
        autonomy_section = None
        if isinstance(colony_cfg, dict):
            autonomy_section = colony_cfg.get("autonomy")
        else:
            autonomy_section = getattr(colony_cfg, "autonomy", None)

        if autonomy_section is None:
            return cls()

        # Read values from the section
        def _get(key: str, default):
            if isinstance(autonomy_section, dict):
                return autonomy_section.get(key, default)
            return getattr(autonomy_section, key, default)

        defaults = cls()
        return cls(
            tick_interval_secs=float(_get("tick_interval_secs", defaults.tick_interval_secs)),
            initiative_confidence_threshold=float(_get(
                "initiative_confidence_threshold",
                defaults.initiative_confidence_threshold,
            )),
            max_actions_per_hour=int(_get("max_actions_per_hour", defaults.max_actions_per_hour)),
            quiet_hours_start=str(_get("quiet_hours_start", defaults.quiet_hours_start)),
            quiet_hours_end=str(_get("quiet_hours_end", defaults.quiet_hours_end)),
            anomaly_severity_threshold=float(_get(
                "anomaly_severity_threshold",
                defaults.anomaly_severity_threshold,
            )),
            prediction_confidence_threshold=float(_get(
                "prediction_confidence_threshold",
                defaults.prediction_confidence_threshold,
            )),
            goal_stale_threshold_hours=float(_get(
                "goal_stale_threshold_hours",
                defaults.goal_stale_threshold_hours,
            )),
            bootstrap_check_interval_hours=int(_get(
                "bootstrap_check_interval_hours",
                defaults.bootstrap_check_interval_hours,
            )),
            self_reflection_interval_days=int(_get(
                "self_reflection_interval_days",
                defaults.self_reflection_interval_days,
            )),
        )

    @classmethod
    def from_env(cls) -> "AutonomyConfig":
        """Construct config from environment variables.

        All env vars are optional; unset vars fall back to field defaults.

        Environment variables:
            COLONY_AUTONOMY_TICK_INTERVAL_SECS
            COLONY_AUTONOMY_INITIATIVE_CONFIDENCE_THRESHOLD
            COLONY_AUTONOMY_MAX_ACTIONS_PER_HOUR
            COLONY_AUTONOMY_QUIET_HOURS_START
            COLONY_AUTONOMY_QUIET_HOURS_END
            COLONY_AUTONOMY_ANOMALY_SEVERITY_THRESHOLD
            COLONY_AUTONOMY_GOAL_STALE_THRESHOLD_HOURS
        """
        def _float(key: str, default: float) -> float:
            v = os.environ.get(key)
            return float(v) if v is not None else default

        def _int(key: str, default: int) -> int:
            v = os.environ.get(key)
            return int(v) if v is not None else default

        def _str(key: str, default: str) -> str:
            return os.environ.get(key, default)

        defaults = cls()
        return cls(
            tick_interval_secs=_float(
                "COLONY_AUTONOMY_TICK_INTERVAL_SECS",
                defaults.tick_interval_secs,
            ),
            initiative_confidence_threshold=_float(
                "COLONY_AUTONOMY_INITIATIVE_CONFIDENCE_THRESHOLD",
                defaults.initiative_confidence_threshold,
            ),
            max_actions_per_hour=_int(
                "COLONY_AUTONOMY_MAX_ACTIONS_PER_HOUR",
                defaults.max_actions_per_hour,
            ),
            quiet_hours_start=_str(
                "COLONY_AUTONOMY_QUIET_HOURS_START",
                defaults.quiet_hours_start,
            ),
            quiet_hours_end=_str(
                "COLONY_AUTONOMY_QUIET_HOURS_END",
                defaults.quiet_hours_end,
            ),
            anomaly_severity_threshold=_float(
                "COLONY_AUTONOMY_ANOMALY_SEVERITY_THRESHOLD",
                defaults.anomaly_severity_threshold,
            ),
            prediction_confidence_threshold=_float(
                "COLONY_AUTONOMY_PREDICTION_CONFIDENCE_THRESHOLD",
                defaults.prediction_confidence_threshold,
            ),
            goal_stale_threshold_hours=_float(
                "COLONY_AUTONOMY_GOAL_STALE_THRESHOLD_HOURS",
                defaults.goal_stale_threshold_hours,
            ),
            bootstrap_check_interval_hours=_int(
                "COLONY_AUTONOMY_BOOTSTRAP_CHECK_INTERVAL_HOURS",
                defaults.bootstrap_check_interval_hours,
            ),
            self_reflection_interval_days=_int(
                "COLONY_AUTONOMY_SELF_REFLECTION_INTERVAL_DAYS",
                defaults.self_reflection_interval_days,
            ),
        )
