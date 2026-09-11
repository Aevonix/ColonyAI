"""Colony Briefing System — configuration dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class DailyBriefingConfig:
    enabled: bool = True
    time: str = "07:00"  # HH:MM in local timezone
    timezone: str = "UTC"
    max_sections: int = 8
    section_order: List[str] = field(
        default_factory=lambda: [
            "calendar",
            "tasks",
            "relationships",
            "anomalies",
            "health",
            "goals",
            "insights",
        ]
    )


@dataclass
class TacticalBriefingConfig:
    enabled: bool = True
    min_severity: str = "warning"  # "critical" | "warning" | "info"
    max_per_hour: int = 3
    max_per_day: int = 10
    always_fire_critical: bool = True
    cooldown_seconds: float = 300.0


@dataclass
class WeeklyBriefingConfig:
    enabled: bool = True
    day: str = "monday"
    time: str = "08:00"
    timezone: str = "UTC"


@dataclass
class BriefingConfig:
    enabled: bool = True
    daily: DailyBriefingConfig = field(default_factory=DailyBriefingConfig)
    tactical: TacticalBriefingConfig = field(default_factory=TacticalBriefingConfig)
    weekly: WeeklyBriefingConfig = field(default_factory=WeeklyBriefingConfig)
    lm_enhancement_enabled: bool = True
    delivery_gateway: str = "api"  # "imessage" | "telegram" | "api"
    max_narrative_tokens: int = 500
    suppressed_sections: List[str] = field(default_factory=list)
