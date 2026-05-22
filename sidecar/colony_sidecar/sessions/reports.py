"""Session report store — lightweight in-memory cross-session bridge.

Keeps the last N session summaries per contact. Older reports are evicted.
This is NOT long-term memory — for that, use the graph store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List


@dataclass
class SessionReport:
    """A summary of a single agent session, written at session end."""

    report_id: str
    session_id: str
    contact_id: str
    started_at: datetime
    ended_at: datetime | None
    summary: str
    topics: List[str]
    resolutions: List[str]
    pending: List[str]
    notified_user: bool
    metadata: Dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SessionReportStore:
    """In-memory store for recent agent session summaries.

    Keeps last *max_per_contact* reports per contact_id. Older reports
    are evicted on FIFO basis.
    """

    def __init__(self, max_per_contact: int = 20) -> None:
        self._reports: Dict[str, List[SessionReport]] = {}
        self._max_per_contact = max_per_contact

    async def add_report(self, report: SessionReport) -> str:
        """Store a report. Returns the report_id."""
        if report.contact_id not in self._reports:
            self._reports[report.contact_id] = []
        self._reports[report.contact_id].append(report)
        # Evict oldest if over limit
        if len(self._reports[report.contact_id]) > self._max_per_contact:
            self._reports[report.contact_id] = self._reports[
                report.contact_id
            ][-self._max_per_contact :]
        return report.report_id

    async def get_recent(
        self,
        contact_id: str,
        hours: int = 24,
        limit: int = 10,
    ) -> List[SessionReport]:
        """Return recent reports for a contact, filtered by age and capped."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        reports = self._reports.get(contact_id, [])
        recent = []
        for r in reports:
            # Defensive: ensure timezone-aware comparison
            ended = r.ended_at
            started = r.started_at
            if ended is not None and ended.tzinfo is None:
                ended = ended.replace(tzinfo=timezone.utc)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if (
                (ended is not None and ended > cutoff)
                or (ended is None and started > cutoff)
            ):
                recent.append(r)
        return recent[-limit:]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contacts": len(self._reports),
            "total_reports": sum(len(v) for v in self._reports.values()),
        }
