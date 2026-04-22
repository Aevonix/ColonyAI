"""Colony Briefing System — BriefingComposer.

Assembles structured briefing content from data aggregators.
LLM enhancement is handled separately by BriefingLMEnhancer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .aggregators import (
    AnomalyAggregatorProtocol,
    CalendarAggregatorProtocol,
    GoalAggregatorProtocol,
    MindModelAggregatorProtocol,
    RelationshipAggregatorProtocol,
    StubAnomalyAggregator,
    StubCalendarAggregator,
    StubGoalAggregator,
    StubMindModelAggregator,
    StubRelationshipAggregator,
    StubSynthesisAggregator,
    SynthesisAggregatorProtocol,
)
from .models import (
    Briefing,
    BriefingPriority,
    BriefingSection,
    BriefingType,
    DailyBriefingContent,
    TacticalBriefingContent,
    WeeklyBriefingContent,
)

logger = logging.getLogger(__name__)

_SEVERITY_PRIORITY = {
    "critical": BriefingPriority.URGENT,
    "warning": BriefingPriority.HIGH,
    "info": BriefingPriority.NORMAL,
}


class BriefingComposer:
    """Assemble briefing content from structured data sources."""

    def __init__(
        self,
        relationship_aggregator: Optional[RelationshipAggregatorProtocol] = None,
        calendar_aggregator: Optional[CalendarAggregatorProtocol] = None,
        goal_aggregator: Optional[GoalAggregatorProtocol] = None,
        anomaly_aggregator: Optional[AnomalyAggregatorProtocol] = None,
        mind_model_aggregator: Optional[MindModelAggregatorProtocol] = None,
        synthesis_aggregator: Optional[SynthesisAggregatorProtocol] = None,
        suppressed_sections: Optional[List[str]] = None,
    ) -> None:
        self._rel = relationship_aggregator or StubRelationshipAggregator()
        self._cal = calendar_aggregator or StubCalendarAggregator()
        self._goal = goal_aggregator or StubGoalAggregator()
        self._anomaly = anomaly_aggregator or StubAnomalyAggregator()
        self._mind = mind_model_aggregator or StubMindModelAggregator()
        self._synthesis = synthesis_aggregator or StubSynthesisAggregator()
        self._suppressed: List[str] = suppressed_sections or []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compose_daily(self, date: str, tz: str) -> DailyBriefingContent:
        """Assemble a daily briefing from all active data sources."""
        events = self._cal.get_today_events(date, tz)
        prep_events = self._cal.get_prep_needed(events)

        overdue = self._goal.get_overdue_goals()
        blocked = self._goal.get_blocked_goals()
        completing_soon = self._goal.get_completing_soon()

        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        rel_changes = self._rel.get_notable_changes(since=since)

        anomalies = self._anomaly.get_active_anomalies()
        health = self._mind.get_health_snapshot()

        return DailyBriefingContent(
            date=date,
            calendar=[e for e in events],
            tasks={
                "overdue": [{"id": g.goal_id, "title": g.title} for g in overdue],
                "due_today": [{"id": g.goal_id, "title": g.title} for g in completing_soon],
                "blocked": [{"id": g.goal_id, "title": g.title} for g in blocked],
                "highlights": [],
            },
            relationships={
                "changes": [
                    {
                        "contact": c.contact_name,
                        "type": c.change_type,
                        "description": c.description,
                    }
                    for c in rel_changes
                ]
            },
            health=(
                {
                    "sleep_score": health.sleep_score,
                    "readiness": health.readiness,
                    "notable": health.notable,
                }
                if health
                else None
            ),
            anomalies=(
                {
                    "active_count": len(anomalies),
                    "summary": [a.description for a in anomalies[:3]],
                }
                if anomalies
                else None
            ),
            initiatives=None,
        )

    def compose_tactical(
        self,
        trigger: str,
        severity: str,
        summary: str,
        details: str,
        suggested_actions: Optional[List[str]] = None,
    ) -> TacticalBriefingContent:
        """Assemble a tactical alert from event data."""
        return TacticalBriefingContent(
            trigger=trigger,
            severity=severity,
            summary=summary,
            details=details,
            suggested_actions=suggested_actions or [],
            requires_response=severity == "critical",
        )

    def compose_weekly(
        self,
        period_start: datetime,
        period_end: datetime,
    ) -> WeeklyBriefingContent:
        """Assemble a weekly retrospective from all data sources."""
        stats = self._goal.get_week_completion_stats(period_start, period_end)
        rel_changes = self._rel.get_notable_changes(since=period_start, min_delta=0.10)
        neglected = self._rel.get_neglected_contacts()
        anomalies = self._anomaly.get_active_anomalies()
        patterns = self._synthesis.get_weekly_patterns(period_start, period_end)
        insights = self._synthesis.get_high_confidence_insights(since=period_start)

        upcoming_events = self._cal.get_upcoming_week(
            period_end.date().isoformat(), "UTC"
        )

        improved = [c.contact_name for c in rel_changes if c.change_type == "sentiment_shift"]
        declined = [c.contact_name for c in rel_changes if c.change_type == "engagement_drop"]

        return WeeklyBriefingContent(
            period_start=period_start.date().isoformat(),
            period_end=period_end.date().isoformat(),
            task_completion_rate=stats.completion_rate,
            relationship_health={
                "improved": improved,
                "declined": declined,
                "neglected": neglected,
            },
            top_achievements=[],
            areas_of_focus=[i.description for i in insights[:3]],
            emerging_patterns=patterns[:5],
            upcoming_week={
                "events": [
                    {"time": e.time, "title": e.title} for e in upcoming_events[:10]
                ]
            },
            system_health={
                "anomaly_count": len(anomalies),
                "backup_status": "unknown",
                "uptime": "unknown",
            },
        )

    # ------------------------------------------------------------------
    # Briefing object assembly
    # ------------------------------------------------------------------

    def build_daily_briefing(
        self,
        date: str,
        tz: str,
        max_sections: int = 8,
        section_order: Optional[List[str]] = None,
        engagement_history: Optional[Dict[str, float]] = None,
    ) -> Briefing:
        """Build a full Briefing object for daily type."""
        content = self.compose_daily(date, tz)
        sections = self._daily_sections(content, section_order or [])
        sections = _apply_suppression(sections, self._suppressed)
        sections = _rank_sections(sections, engagement_history or {}, BriefingType.DAILY)
        sections = sections[:max_sections]
        return Briefing(
            briefing_type=BriefingType.DAILY,
            sections=sections,
            triggered_by="schedule",
        )

    def build_tactical_briefing(
        self,
        trigger: str,
        severity: str,
        summary: str,
        details: str,
        suggested_actions: Optional[List[str]] = None,
    ) -> Briefing:
        """Build a full Briefing object for tactical type."""
        content = self.compose_tactical(trigger, severity, summary, details, suggested_actions)
        priority = _SEVERITY_PRIORITY.get(severity, BriefingPriority.NORMAL)
        section = BriefingSection(
            name="alert",
            content={
                "trigger": content.trigger,
                "severity": content.severity,
                "summary": content.summary,
                "details": content.details,
                "suggested_actions": content.suggested_actions,
                "requires_response": content.requires_response,
            },
            priority=100 if severity == "critical" else 80,
        )
        return Briefing(
            briefing_type=BriefingType.TACTICAL,
            sections=[section],
            priority=priority,
            triggered_by=trigger,
        )

    def build_weekly_briefing(
        self,
        period_start: datetime,
        period_end: datetime,
        engagement_history: Optional[Dict[str, float]] = None,
    ) -> Briefing:
        """Build a full Briefing object for weekly type."""
        content = self.compose_weekly(period_start, period_end)
        sections = self._weekly_sections(content)
        sections = _apply_suppression(sections, self._suppressed)
        sections = _rank_sections(sections, engagement_history or {}, BriefingType.WEEKLY)
        return Briefing(
            briefing_type=BriefingType.WEEKLY,
            sections=sections,
            triggered_by="schedule",
        )

    # ------------------------------------------------------------------
    # Internal section builders
    # ------------------------------------------------------------------

    def _daily_sections(
        self,
        content: DailyBriefingContent,
        section_order: List[str],
    ) -> List[BriefingSection]:
        builders: Dict[str, Any] = {
            "calendar": lambda: BriefingSection(
                name="calendar",
                content={
                    "date": content.date,
                    "events": [
                        {
                            "time": e.time,
                            "title": e.title,
                            "participants": e.participants,
                            "prep_needed": e.prep_needed,
                            "location": e.location,
                            "duration_minutes": e.duration_minutes,
                        }
                        for e in content.calendar
                    ],
                    "no_events": len(content.calendar) == 0,
                },
                priority=90,
            ),
            "tasks": lambda: BriefingSection(
                name="tasks",
                content=content.tasks,
                priority=85,
            ),
            "relationships": lambda: BriefingSection(
                name="relationships",
                content=content.relationships,
                priority=70,
            ),
            "anomalies": lambda: BriefingSection(
                name="anomalies",
                content=content.anomalies or {"active_count": 0, "summary": []},
                priority=95,
            ),
            "health": lambda: BriefingSection(
                name="health",
                content=content.health or {},
                priority=60,
            ),
            "goals": lambda: BriefingSection(
                name="goals",
                content={
                    "blocked": content.tasks.get("blocked", []),
                    "overdue": content.tasks.get("overdue", []),
                },
                priority=80,
            ),
            "insights": lambda: BriefingSection(
                name="insights",
                content={},
                priority=50,
            ),
        }

        order = section_order if section_order else list(builders.keys())
        sections: List[BriefingSection] = []
        for name in order:
            if name in builders:
                s = builders[name]()
                # Skip empty optional sections
                if name == "anomalies" and (content.anomalies is None or content.anomalies.get("active_count", 0) == 0):
                    continue
                if name == "health" and content.health is None:
                    continue
                if name == "relationships" and not content.relationships.get("changes"):
                    continue
                sections.append(s)
        # Always include calendar and tasks
        present = {s.name for s in sections}
        for required in ("calendar", "tasks"):
            if required not in present and required in builders:
                sections.insert(0 if required == "calendar" else 1, builders[required]())
        return sections

    def _weekly_sections(self, content: WeeklyBriefingContent) -> List[BriefingSection]:
        return [
            BriefingSection(
                name="task_completion",
                content={
                    "completion_rate": content.task_completion_rate,
                    "period_start": content.period_start,
                    "period_end": content.period_end,
                },
                priority=85,
            ),
            BriefingSection(
                name="relationship_health",
                content=content.relationship_health,
                priority=80,
            ),
            BriefingSection(
                name="achievements",
                content={"highlights": content.top_achievements},
                priority=75,
            ),
            BriefingSection(
                name="areas_of_focus",
                content={"areas": content.areas_of_focus},
                priority=70,
            ),
            BriefingSection(
                name="emerging_patterns",
                content={"patterns": content.emerging_patterns},
                priority=65,
            ),
            BriefingSection(
                name="upcoming_week",
                content=content.upcoming_week,
                priority=90,
            ),
            BriefingSection(
                name="system_health",
                content=content.system_health,
                priority=60,
            ),
        ]


# ---------------------------------------------------------------------------
# Section utilities
# ---------------------------------------------------------------------------


def _apply_suppression(
    sections: List[BriefingSection],
    suppressed: List[str],
) -> List[BriefingSection]:
    for s in sections:
        if s.name in suppressed:
            s.suppressed = True
    return sections


def _rank_sections(
    sections: List[BriefingSection],
    engagement_history: Dict[str, float],
    briefing_type: BriefingType,
) -> List[BriefingSection]:
    """Sort sections by composite priority score (higher = first)."""

    def score(s: BriefingSection) -> float:
        base = float(s.priority)
        engagement_bonus = engagement_history.get(s.name, 0.5) * 20.0
        # Urgency bonus for anomaly / alert sections
        urgency = 0.0
        if s.name == "anomalies":
            count = s.content.get("active_count", 0)
            urgency = min(count * 5, 30)
        if s.name == "alert":
            urgency = 30.0
        return base + engagement_bonus + urgency

    return sorted(sections, key=score, reverse=True)
