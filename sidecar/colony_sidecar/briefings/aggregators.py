"""Colony Briefing System — data aggregators.

Thin adapter layer that pulls structured data from Colony intelligence
modules and formats it for the BriefingComposer. Each aggregator uses
stub/default behaviour when the underlying subsystem is unavailable,
so briefings always generate even in partially-configured environments.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import zoneinfo
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta as _timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from .models import CalendarEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain value objects
# ---------------------------------------------------------------------------


@dataclass
class RelationshipChange:
    contact_name: str
    change_type: str  # "new" | "dormant" | "sentiment_shift" | "tier_change"
    description: str
    trust_tier: str  # "info" | "casual" | "close" | "inner_circle"


@dataclass
class GoalSummary:
    goal_id: str
    title: str
    status: str  # "overdue" | "blocked" | "completing_soon"
    due_at: Optional[datetime] = None


@dataclass
class GoalCompletionStats:
    total_initiated: int
    total_completed: int
    completion_rate: float  # 0.0–1.0


@dataclass
class AnomalySummary:
    anomaly_id: str
    severity: str  # "critical" | "warning" | "info"
    description: str
    detected_at: datetime
    source: str


@dataclass
class HealthSnapshot:
    sleep_score: Optional[float] = None  # 0.0–1.0
    readiness: Optional[float] = None
    notable: Optional[str] = None


@dataclass
class CrossDomainInsight:
    insight_id: str
    description: str
    confidence: float
    domains: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Aggregator protocols (allow injection of real or fake implementations)
# ---------------------------------------------------------------------------


@runtime_checkable
class RelationshipAggregatorProtocol(Protocol):
    def get_notable_changes(
        self, since: datetime, min_delta: float = 0.15
    ) -> List[RelationshipChange]: ...

    def get_neglected_contacts(
        self, days_since_contact: int = 14, limit: int = 5
    ) -> List[str]: ...


@runtime_checkable
class CalendarAggregatorProtocol(Protocol):
    def get_today_events(self, date: str, timezone: str) -> List[CalendarEvent]: ...

    def get_prep_needed(self, events: List[CalendarEvent]) -> List[CalendarEvent]: ...

    def get_upcoming_week(self, start_date: str, timezone: str) -> List[CalendarEvent]: ...


@runtime_checkable
class GoalAggregatorProtocol(Protocol):
    def get_overdue_goals(self) -> List[GoalSummary]: ...

    def get_blocked_goals(self) -> List[GoalSummary]: ...

    def get_completing_soon(self, hours: float = 4.0) -> List[GoalSummary]: ...

    def get_week_completion_stats(
        self, period_start: datetime, period_end: datetime
    ) -> GoalCompletionStats: ...


@runtime_checkable
class AnomalyAggregatorProtocol(Protocol):
    def get_active_anomalies(self, min_severity: str = "warning") -> List[AnomalySummary]: ...

    def get_new_since(
        self, since: datetime, min_severity: str = "warning"
    ) -> List[AnomalySummary]: ...


@runtime_checkable
class MindModelAggregatorProtocol(Protocol):
    def get_health_snapshot(self) -> Optional[HealthSnapshot]: ...

    def get_predicted_load(self, date: str) -> Optional[float]: ...


@runtime_checkable
class SynthesisAggregatorProtocol(Protocol):
    def get_high_confidence_insights(
        self,
        min_confidence: float = 0.80,
        since: Optional[datetime] = None,
        limit: int = 3,
    ) -> List[CrossDomainInsight]: ...

    def get_weekly_patterns(
        self, period_start: datetime, period_end: datetime
    ) -> List[str]: ...


# ---------------------------------------------------------------------------
# Default stub implementations (used when subsystems are not configured)
# ---------------------------------------------------------------------------


class StubRelationshipAggregator:
    def get_notable_changes(
        self, since: datetime, min_delta: float = 0.15
    ) -> List[RelationshipChange]:
        return []

    def get_neglected_contacts(
        self, days_since_contact: int = 14, limit: int = 5
    ) -> List[str]:
        return []


class StubCalendarAggregator:
    def get_today_events(self, date: str, timezone: str) -> List[CalendarEvent]:
        return []

    def get_prep_needed(self, events: List[CalendarEvent]) -> List[CalendarEvent]:
        return []

    def get_upcoming_week(self, start_date: str, timezone: str) -> List[CalendarEvent]:
        return []


class StubGoalAggregator:
    def get_overdue_goals(self) -> List[GoalSummary]:
        return []

    def get_blocked_goals(self) -> List[GoalSummary]:
        return []

    def get_completing_soon(self, hours: float = 4.0) -> List[GoalSummary]:
        return []

    def get_week_completion_stats(
        self, period_start: datetime, period_end: datetime
    ) -> GoalCompletionStats:
        return GoalCompletionStats(total_initiated=0, total_completed=0, completion_rate=0.0)


class StubAnomalyAggregator:
    def get_active_anomalies(self, min_severity: str = "warning") -> List[AnomalySummary]:
        return []

    def get_new_since(
        self, since: datetime, min_severity: str = "warning"
    ) -> List[AnomalySummary]:
        return []


class StubMindModelAggregator:
    def get_health_snapshot(self) -> Optional[HealthSnapshot]:
        return None

    def get_predicted_load(self, date: str) -> Optional[float]:
        return None


class StubSynthesisAggregator:
    def get_high_confidence_insights(
        self,
        min_confidence: float = 0.80,
        since: Optional[datetime] = None,
        limit: int = 3,
    ) -> List[CrossDomainInsight]:
        return []

    def get_weekly_patterns(
        self, period_start: datetime, period_end: datetime
    ) -> List[str]:
        return []


# ---------------------------------------------------------------------------
# Async → sync bridge
# ---------------------------------------------------------------------------


_ASYNC_BRIDGE_POOL: Optional[concurrent.futures.ThreadPoolExecutor] = None
_ASYNC_BRIDGE_LOCK = threading.Lock()


def _async_bridge_pool() -> concurrent.futures.ThreadPoolExecutor:
    """Lazily create one shared bridge pool, instead of a new executor per call.

    A fresh ``ThreadPoolExecutor`` per ``_run_async`` thrashed thread+loop
    create/destroy under tactical-briefing bursts (a composer makes 5–8 of these
    calls per briefing). One small bounded pool is reused for the whole process.
    """
    global _ASYNC_BRIDGE_POOL
    if _ASYNC_BRIDGE_POOL is None:
        with _ASYNC_BRIDGE_LOCK:
            if _ASYNC_BRIDGE_POOL is None:
                _ASYNC_BRIDGE_POOL = concurrent.futures.ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="briefing-async"
                )
    return _ASYNC_BRIDGE_POOL


def _run_async(coro: Any) -> Any:
    """Run an async coroutine from a synchronous call-site.

    Uses ``asyncio.run()`` when no event loop is active (the normal production
    path where briefing generation is synchronous).  Falls back to a shared
    worker pool when a loop is already running (e.g. inside pytest-asyncio
    tests), so we never deadlock by trying to nest ``asyncio.run()``.
    """
    try:
        asyncio.get_running_loop()
        # Already inside a running loop — offload to the shared bridge pool.
        return _async_bridge_pool().submit(asyncio.run, coro).result()
    except RuntimeError:
        # No running loop — safe to call asyncio.run directly.
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Real implementations
# ---------------------------------------------------------------------------


class RelationshipAggregator:
    """Real implementation: wraps RelationshipScorer and queries Neo4j graph data.

    Score-change history is read from ``ScoreEvent`` nodes written by
    ``RelationshipScorer.record_score_change()``.  Neglected contacts are
    found via a direct graph query so that the aggregator is usable without
    running a full scorer refresh cycle.
    """

    # Scores live on a 0-100 scale; min_delta is expressed as a 0-1 fraction,
    # so we multiply by 100 to get the point-delta threshold.
    _SCORE_CHANGES_CYPHER = """
    MATCH (p:Person)-[:SCORE_CHANGED]->(se:ScoreEvent)
    WHERE se.createdAt >= datetime($since_iso)
    AND abs(se.delta) >= $delta_threshold
    RETURN p.name AS name, p.tier AS current_tier,
           se.delta AS delta, se.reason AS reason, se.tier AS new_tier
    ORDER BY abs(se.delta) DESC
    """

    _NEGLECTED_CYPHER = """
    MATCH (p:Person)
    WHERE p.tier IN ['inner_circle', 'trusted', 'regular']
    AND (
        p.lastInteraction IS NULL
        OR p.lastInteraction < datetime() - duration({days: $days_threshold})
    )
    RETURN p.name AS name
    ORDER BY p.lastInteraction ASC
    LIMIT $limit
    """

    def __init__(self, scorer: Any, graph: Any) -> None:
        self._scorer = scorer
        self._graph = graph

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def get_notable_changes(
        self, since: datetime, min_delta: float = 0.15
    ) -> List[RelationshipChange]:
        try:
            return _run_async(self._notable_changes_async(since, min_delta))
        except Exception:
            logger.exception("RelationshipAggregator.get_notable_changes failed")
            return []

    def get_neglected_contacts(
        self, days_since_contact: int = 14, limit: int = 5
    ) -> List[str]:
        try:
            return _run_async(self._neglected_contacts_async(days_since_contact, limit))
        except Exception:
            logger.exception("RelationshipAggregator.get_neglected_contacts failed")
            return []

    # ------------------------------------------------------------------
    # Async helpers (isolated for testability)
    # ------------------------------------------------------------------

    async def _notable_changes_async(
        self, since: datetime, min_delta: float
    ) -> List[RelationshipChange]:
        delta_threshold = min_delta * 100.0
        rows = await self._query_score_changes(since.isoformat(), delta_threshold)
        result: List[RelationshipChange] = []
        for row in rows:
            name: str = row.get("name") or "Unknown"
            tier: str = row.get("new_tier") or row.get("current_tier") or "peripheral"
            delta: float = float(row.get("delta") or 0.0)
            reason: str = row.get("reason") or ""

            if reason == "new_contact":
                change_type = "new"
            elif delta < 0:
                change_type = "dormant"
            else:
                change_type = "tier_change"

            description = (
                f"Score increased by {delta:.1f} points"
                if delta >= 0
                else f"Score decreased by {abs(delta):.1f} points"
            )
            result.append(RelationshipChange(
                contact_name=name,
                change_type=change_type,
                description=description,
                trust_tier=tier,
            ))
        return result

    async def _neglected_contacts_async(
        self, days_since_contact: int, limit: int
    ) -> List[str]:
        rows = await self._query_neglected(days_since_contact, limit)
        return [r["name"] for r in rows if r.get("name")]

    async def _query_score_changes(
        self, since_iso: str, delta_threshold: float
    ) -> List[Dict[str, Any]]:
        async with self._graph.driver.session(database=self._graph.database) as session:
            result = await session.run(
                self._SCORE_CHANGES_CYPHER,
                since_iso=since_iso,
                delta_threshold=delta_threshold,
            )
            return [dict(r) async for r in result]

    async def _query_neglected(
        self, days_threshold: int, limit: int
    ) -> List[Dict[str, Any]]:
        async with self._graph.driver.session(database=self._graph.database) as session:
            result = await session.run(
                self._NEGLECTED_CYPHER,
                days_threshold=days_threshold,
                limit=limit,
            )
            return [dict(r) async for r in result]


class CalendarAggregator:
    """Real implementation: wraps CalendarIntegration for the briefing pipeline.

    ``CalendarIntegration`` is async; this class bridges the sync protocol
    interface using ``_run_async()``.  Events are converted from the
    ``EventData`` representation used by the integration layer to the
    ``CalendarEvent`` model used by briefings.

    Prep-needed heuristic: an event needs prep when it has a video link *or*
    has two or more attendees.
    """

    _PREP_ATTENDEE_THRESHOLD = 2

    def __init__(self, calendar: Any) -> None:
        self._calendar = calendar

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def get_today_events(self, date: str, timezone: str) -> List[CalendarEvent]:
        try:
            return _run_async(self._today_events_async(date, timezone))
        except Exception:
            logger.exception("CalendarAggregator.get_today_events failed")
            return []

    def get_prep_needed(self, events: List[CalendarEvent]) -> List[CalendarEvent]:
        return [e for e in events if e.prep_needed]

    def get_upcoming_week(self, start_date: str, timezone: str) -> List[CalendarEvent]:
        try:
            return _run_async(self._upcoming_week_async(start_date, timezone))
        except Exception:
            logger.exception("CalendarAggregator.get_upcoming_week failed")
            return []

    # ------------------------------------------------------------------
    # Async helpers (isolated for testability)
    # ------------------------------------------------------------------

    async def _today_events_async(
        self, date_str: str, tz_name: str
    ) -> List[CalendarEvent]:
        target_date = _date.fromisoformat(date_str)
        tz_info = _resolve_tz(tz_name)
        # Fetch 2 days to safely cover timezone-offset edge cases.
        raw_events = await self._calendar.list_events(days=2, max_results=100)
        items: List[tuple] = []
        for ev in raw_events:
            ev_local = ev.start.astimezone(tz_info)
            if ev_local.date() != target_date:
                continue
            items.append((ev_local, _to_calendar_event(ev, tz_info)))
        items.sort(key=lambda x: x[0])
        return [item[1] for item in items]

    async def _upcoming_week_async(
        self, start_date: str, tz_name: str
    ) -> List[CalendarEvent]:
        target_start = _date.fromisoformat(start_date)
        target_end = target_start + _timedelta(days=7)
        tz_info = _resolve_tz(tz_name)
        raw_events = await self._calendar.list_events(days=8, max_results=100)
        items: List[tuple] = []
        for ev in raw_events:
            ev_local = ev.start.astimezone(tz_info)
            if not (target_start <= ev_local.date() < target_end):
                continue
            items.append((ev_local, _to_calendar_event(ev, tz_info)))
        items.sort(key=lambda x: x[0])
        return [item[1] for item in items]


# ---------------------------------------------------------------------------
# Module-level helpers shared by CalendarAggregator
# ---------------------------------------------------------------------------


def _resolve_tz(tz_name: str) -> Any:
    try:
        return zoneinfo.ZoneInfo(tz_name)
    except Exception:
        logger.warning("Unknown timezone %r, falling back to UTC", tz_name)
        return timezone.utc


def _to_calendar_event(ev: Any, tz_info: Any) -> CalendarEvent:
    prep_needed = bool(ev.video_link or len(ev.attendees) >= 2)
    ev_local = ev.start.astimezone(tz_info)
    return CalendarEvent(
        time=ev_local.strftime("%H:%M"),
        title=ev.title,
        participants=list(ev.attendees),
        prep_needed=prep_needed,
        location=ev.location,
        duration_minutes=int(ev.duration_minutes),
    )
