"""AutonomyLoop configuration dataclass."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional
from enum import Enum
from zoneinfo import ZoneInfo


class AutonomyMode(str, Enum):
    """Autonomy loop operating mode."""
    REACTIVE = "reactive"    # On-demand only (default)
    PROACTIVE = "proactive"  # Timer-based


@dataclass
class InitiativeConfig:
    """Configuration for initiative deduplication and feedback (v0.7.10)."""
    cooldown_hours: float = 24.0
    cooldown_tasks: float = 12.0
    cooldown_contacts: float = 72.0
    max_snooze_hours: int = 168
    feedback_enabled: bool = True


@dataclass
class AutonomyConfig:
    """Configuration for the Colony autonomy loop.

    All time values are in seconds unless noted.
    """

    # Operating mode: reactive (on-demand) or proactive (timer-based)
    mode: AutonomyMode = AutonomyMode.REACTIVE

    # IANA timezone for quiet hours (e.g., "America/El_Salvador")
    timezone: str = "UTC"

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

    # ── Owner check-in (silence-triggered proactive outreach) ──────────
    # When no initiatives fire for this many hours, reach out to the owner.
    owner_check_in_enabled: bool = True
    owner_check_in_silent_hours: float = 1.0
    owner_check_in_cooldown_hours: float = 4.0
    owner_contact_id: Optional[str] = None  # None = resolve from identity

    # ── Conversation synthesis (periodic memory scan for goals) ─────────
    # Scans stored conversation memories for implicit goals and commitments.
    conversation_synthesis_enabled: bool = True
    conversation_synthesis_interval_secs: float = 1800.0  # 30 min
    conversation_synthesis_lookback_hours: float = 2.0
    conversation_synthesis_min_confidence: float = 0.35

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

        # Mode and timezone
        mode_str = str(_get("mode", "reactive")).lower()
        mode = AutonomyMode(mode_str) if mode_str in [m.value for m in AutonomyMode] else AutonomyMode.REACTIVE
        timezone = str(_get("timezone", "UTC"))

        # Validate timezone
        try:
            ZoneInfo(timezone)
        except Exception:
            timezone = "UTC"

        return cls(
            mode=mode,
            timezone=timezone,
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
            owner_check_in_enabled=bool(_get("owner_check_in_enabled", defaults.owner_check_in_enabled)),
            owner_check_in_silent_hours=float(_get(
                "owner_check_in_silent_hours",
                defaults.owner_check_in_silent_hours,
            )),
            owner_check_in_cooldown_hours=float(_get(
                "owner_check_in_cooldown_hours",
                defaults.owner_check_in_cooldown_hours,
            )),
            owner_contact_id=_get("owner_contact_id", defaults.owner_contact_id),
            conversation_synthesis_enabled=bool(_get(
                "conversation_synthesis_enabled",
                defaults.conversation_synthesis_enabled,
            )),
            conversation_synthesis_interval_secs=float(_get(
                "conversation_synthesis_interval_secs",
                defaults.conversation_synthesis_interval_secs,
            )),
            conversation_synthesis_lookback_hours=float(_get(
                "conversation_synthesis_lookback_hours",
                defaults.conversation_synthesis_lookback_hours,
            )),
            conversation_synthesis_min_confidence=float(_get(
                "conversation_synthesis_min_confidence",
                defaults.conversation_synthesis_min_confidence,
            )),
        )

    @classmethod
    def from_env(cls) -> "AutonomyConfig":
        """Construct config from environment variables.

        All env vars are optional; unset vars fall back to field defaults.

        Environment variables:
            COLONY_AUTONOMY_MODE
            COLONY_TIMEZONE
            COLONY_AUTONOMY_TICK_INTERVAL_SECS
            COLONY_AUTONOMY_INITIATIVE_CONFIDENCE_THRESHOLD
            COLONY_AUTONOMY_MAX_ACTIONS_PER_HOUR
            COLONY_AUTONOMY_QUIET_HOURS_START
            COLONY_AUTONOMY_QUIET_HOURS_END
            COLONY_AUTONOMY_ANOMALY_SEVERITY_THRESHOLD
            COLONY_AUTONOMY_PREDICTION_CONFIDENCE_THRESHOLD
            COLONY_AUTONOMY_GOAL_STALE_THRESHOLD_HOURS
            COLONY_OWNER_CHECK_IN_ENABLED
            COLONY_OWNER_CHECK_IN_SILENT_HOURS
            COLONY_OWNER_CHECK_IN_COOLDOWN_HOURS
            COLONY_OWNER_CONTACT_ID
            COLONY_CONVERSATION_SYNTHESIS_ENABLED
            COLONY_CONVERSATION_SYNTHESIS_INTERVAL_SECS
            COLONY_CONVERSATION_SYNTHESIS_LOOKBACK_HOURS
            COLONY_CONVERSATION_SYNTHESIS_MIN_CONFIDENCE
        """
        logger = logging.getLogger(__name__)

        # Mode selection
        mode_str = os.environ.get("COLONY_AUTONOMY_MODE", "reactive").lower()
        mode = AutonomyMode(mode_str) if mode_str in [m.value for m in AutonomyMode] else AutonomyMode.REACTIVE

        # Timezone
        timezone = os.environ.get("COLONY_TIMEZONE", "UTC")
        try:
            ZoneInfo(timezone)
        except Exception:
            logger.warning("Invalid COLONY_TIMEZONE '%s', falling back to UTC", timezone)
            timezone = "UTC"

        # Legacy migration: if tick interval set without mode, assume proactive
        legacy_tick = os.environ.get("COLONY_AUTONOMY_TICK_INTERVAL_SECS")
        if legacy_tick and not os.environ.get("COLONY_AUTONOMY_MODE"):
            logger.warning(
                "COLONY_AUTONOMY_TICK_INTERVAL_SECS set without COLONY_AUTONOMY_MODE. "
                "Defaulting to PROACTIVE mode to preserve existing behavior. "
                "Add COLONY_AUTONOMY_MODE=proactive to make this explicit."
            )
            mode = AutonomyMode.PROACTIVE

        def _float(key: str, default: float) -> float:
            v = os.environ.get(key)
            return float(v) if v is not None else default

        def _int(key: str, default: int) -> int:
            v = os.environ.get(key)
            return int(v) if v is not None else default

        def _str(key: str, default: str) -> str:
            return os.environ.get(key, default)

        def _bool(key: str, default: bool) -> bool:
            v = os.environ.get(key, "").lower()
            if v == "true":
                return True
            if v == "false":
                return False
            return default

        defaults = cls()
        return cls(
            mode=mode,
            timezone=timezone,
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
            owner_check_in_enabled=_bool(
                "COLONY_OWNER_CHECK_IN_ENABLED",
                defaults.owner_check_in_enabled,
            ),
            owner_check_in_silent_hours=_float(
                "COLONY_OWNER_CHECK_IN_SILENT_HOURS",
                defaults.owner_check_in_silent_hours,
            ),
            owner_check_in_cooldown_hours=_float(
                "COLONY_OWNER_CHECK_IN_COOLDOWN_HOURS",
                defaults.owner_check_in_cooldown_hours,
            ),
            owner_contact_id=os.environ.get(
                "COLONY_OWNER_CONTACT_ID",
                defaults.owner_contact_id,
            ),
            conversation_synthesis_enabled=_bool(
                "COLONY_CONVERSATION_SYNTHESIS_ENABLED",
                defaults.conversation_synthesis_enabled,
            ),
            conversation_synthesis_interval_secs=_float(
                "COLONY_CONVERSATION_SYNTHESIS_INTERVAL_SECS",
                defaults.conversation_synthesis_interval_secs,
            ),
            conversation_synthesis_lookback_hours=_float(
                "COLONY_CONVERSATION_SYNTHESIS_LOOKBACK_HOURS",
                defaults.conversation_synthesis_lookback_hours,
            ),
            conversation_synthesis_min_confidence=_float(
                "COLONY_CONVERSATION_SYNTHESIS_MIN_CONFIDENCE",
                defaults.conversation_synthesis_min_confidence,
            ),
        )
