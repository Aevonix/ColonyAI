"""Colony Briefing System — data models.

Defines the core Briefing, BriefingSection, and briefing content types
used throughout the briefing pipeline.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class BriefingType(str, Enum):
    DAILY = "daily"
    TACTICAL = "tactical"
    WEEKLY = "weekly"


class BriefingStatus(str, Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    READ = "read"
    ARCHIVED = "archived"


class BriefingPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


@dataclass
class BriefingSection:
    """A single named content block within a briefing."""

    section_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    content: Dict[str, Any] = field(default_factory=dict)
    narrative: str = ""
    priority: int = 50
    suppressed: bool = False
    engagement: Optional[str] = None  # "read" | "dismissed" | "acted"


@dataclass
class Briefing:
    """A delivered summary document."""

    briefing_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    briefing_type: BriefingType = BriefingType.DAILY
    status: BriefingStatus = BriefingStatus.DRAFT
    sections: List[BriefingSection] = field(default_factory=list)
    priority: BriefingPriority = BriefingPriority.NORMAL
    triggered_by: Optional[str] = None
    gateway: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    delivered_at: Optional[datetime] = None
    read_at: Optional[datetime] = None

    def is_delivered(self) -> bool:
        return self.status in {BriefingStatus.DELIVERED, BriefingStatus.READ}

    def active_sections(self) -> List[BriefingSection]:
        return [s for s in self.sections if not s.suppressed]


# ---------------------------------------------------------------------------
# Content types
# ---------------------------------------------------------------------------


@dataclass
class CalendarEvent:
    time: str  # HH:MM local time
    title: str
    participants: List[str]
    prep_needed: bool = False
    location: Optional[str] = None
    duration_minutes: int = 30


@dataclass
class DailyBriefingContent:
    date: str  # ISO-8601 date
    calendar: List[CalendarEvent]
    tasks: Dict[str, Any]  # overdue, due_today, blocked, highlights
    relationships: Dict[str, Any]  # changes list
    health: Optional[Dict[str, Any]] = None
    anomalies: Optional[Dict[str, Any]] = None
    initiatives: Optional[Dict[str, Any]] = None


@dataclass
class TacticalBriefingContent:
    trigger: str
    severity: str  # "critical" | "warning" | "info"
    summary: str
    details: str
    suggested_actions: List[str] = field(default_factory=list)
    related_entities: List[str] = field(default_factory=list)
    requires_response: bool = False


@dataclass
class WeeklyBriefingContent:
    period_start: str  # ISO-8601 date
    period_end: str  # ISO-8601 date
    task_completion_rate: float  # 0.0–1.0
    relationship_health: Dict[str, Any]  # improved, declined, neglected
    top_achievements: List[str]
    areas_of_focus: List[str]
    emerging_patterns: List[str]
    upcoming_week: Dict[str, Any]
    system_health: Dict[str, Any]  # uptime, errors, backup_status


# ---------------------------------------------------------------------------
# Intelligence integration models
# ---------------------------------------------------------------------------


@dataclass
class BriefingCompletionSignal:
    """Signal emitted to the synthesis engine after briefing delivery."""

    briefing_id: str
    briefing_type: BriefingType
    sections: List[str]
    engaged_sections: List[str]
    delivered_at: datetime
    total_anomalies: int
    active_goals: int


@dataclass
class BriefingTelemetryEvent:
    """Telemetry for MetaLearner briefing performance tracking."""

    briefing_id: str
    briefing_type: BriefingType
    time_to_generate_ms: float
    time_to_deliver_ms: float
    lm_tokens_used: int
    section_count: int
    engagement_score: float
    gateway: str
    delivered_at: datetime


# ---------------------------------------------------------------------------
# Engagement
# ---------------------------------------------------------------------------


@dataclass
class SectionEngagementRecord:
    """Record of user engagement with a briefing section."""

    section_name: str
    briefing_id: str
    signal: str  # "read" | "dismissed" | "acted" | "shared"
    recorded_at: datetime
    context: Optional[str] = None


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


@dataclass
class ScheduleEntry:
    """Persisted schedule state for daily/weekly briefings."""

    type: str  # "daily" | "weekly"
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
