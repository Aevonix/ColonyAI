"""Tests for real RelationshipAggregator and CalendarAggregator implementations."""

from __future__ import annotations

from datetime import datetime, date, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from colony_sidecar.briefings.aggregators import (
    CalendarAggregator,
    RelationshipAggregator,
    StubCalendarAggregator,
    StubRelationshipAggregator,
    _resolve_tz,
    _to_calendar_event,
)
from colony_sidecar.briefings.models import CalendarEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_event_data(
    *,
    id: str = "evt1",
    title: str = "Meeting",
    start: datetime,
    end: datetime | None = None,
    attendees: list | None = None,
    video_link: str | None = None,
    location: str | None = None,
):
    """Build a minimal EventData-like mock."""
    from datetime import timedelta

    ev = MagicMock()
    ev.id = id
    ev.title = title
    ev.start = start
    ev.end = end or (start + timedelta(hours=1))
    ev.attendees = attendees or []
    ev.video_link = video_link
    ev.location = location
    # duration_minutes is a property on EventData
    ev.duration_minutes = (ev.end - ev.start).total_seconds() / 60.0
    return ev


# ---------------------------------------------------------------------------
# RelationshipAggregator
# ---------------------------------------------------------------------------


class TestRelationshipAggregator:
    def _make_agg(self, score_rows=None, neglect_rows=None):
        """Return a RelationshipAggregator with patched query helpers."""
        graph = MagicMock()
        scorer = MagicMock()
        agg = RelationshipAggregator(scorer, graph)
        agg._query_score_changes = AsyncMock(return_value=score_rows or [])
        agg._query_neglected = AsyncMock(return_value=neglect_rows or [])
        return agg

    # --- get_notable_changes ---

    def test_get_notable_changes_empty(self):
        agg = self._make_agg(score_rows=[])
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        result = agg.get_notable_changes(since=since)
        assert result == []

    def test_get_notable_changes_positive_delta_mapped_as_tier_change(self):
        rows = [
            {
                "name": "Alice",
                "current_tier": "trusted",
                "delta": 20.0,
                "reason": "periodic_refresh",
                "new_tier": "inner_circle",
            }
        ]
        agg = self._make_agg(score_rows=rows)
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        result = agg.get_notable_changes(since=since, min_delta=0.15)

        assert len(result) == 1
        rc = result[0]
        assert rc.contact_name == "Alice"
        assert rc.trust_tier == "inner_circle"
        assert rc.change_type == "tier_change"
        assert "20.0" in rc.description

    def test_get_notable_changes_negative_delta_mapped_as_dormant(self):
        rows = [
            {
                "name": "Bob",
                "current_tier": "trusted",
                "delta": -18.0,
                "reason": "periodic_refresh",
                "new_tier": "regular",
            }
        ]
        agg = self._make_agg(score_rows=rows)
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        result = agg.get_notable_changes(since=since)

        assert len(result) == 1
        rc = result[0]
        assert rc.contact_name == "Bob"
        assert rc.change_type == "dormant"
        assert "18.0" in rc.description

    def test_get_notable_changes_new_contact(self):
        rows = [
            {
                "name": "Carol",
                "current_tier": "peripheral",
                "delta": 50.0,
                "reason": "new_contact",
                "new_tier": "regular",
            }
        ]
        agg = self._make_agg(score_rows=rows)
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        result = agg.get_notable_changes(since=since)

        assert result[0].change_type == "new"
        assert result[0].trust_tier == "regular"

    def test_get_notable_changes_falls_back_to_current_tier_when_new_tier_missing(self):
        rows = [
            {
                "name": "Dave",
                "current_tier": "trusted",
                "delta": 16.0,
                "reason": "periodic_refresh",
                "new_tier": None,
            }
        ]
        agg = self._make_agg(score_rows=rows)
        result = agg.get_notable_changes(since=datetime(2026, 3, 1, tzinfo=timezone.utc))
        assert result[0].trust_tier == "trusted"

    def test_get_notable_changes_returns_empty_list_on_exception(self):
        graph = MagicMock()
        scorer = MagicMock()
        agg = RelationshipAggregator(scorer, graph)
        agg._query_score_changes = AsyncMock(side_effect=RuntimeError("db error"))
        result = agg.get_notable_changes(since=datetime(2026, 3, 1, tzinfo=timezone.utc))
        assert result == []

    # --- get_neglected_contacts ---

    def test_get_neglected_contacts_empty(self):
        agg = self._make_agg(neglect_rows=[])
        result = agg.get_neglected_contacts()
        assert result == []

    def test_get_neglected_contacts_returns_names(self):
        rows = [{"name": "Eve"}, {"name": "Frank"}]
        agg = self._make_agg(neglect_rows=rows)
        result = agg.get_neglected_contacts(days_since_contact=14, limit=5)
        assert result == ["Eve", "Frank"]

    def test_get_neglected_contacts_skips_none_names(self):
        rows = [{"name": "Eve"}, {"name": None}, {"name": "Grace"}]
        agg = self._make_agg(neglect_rows=rows)
        result = agg.get_neglected_contacts()
        assert result == ["Eve", "Grace"]

    def test_get_neglected_contacts_returns_empty_on_exception(self):
        graph = MagicMock()
        scorer = MagicMock()
        agg = RelationshipAggregator(scorer, graph)
        agg._query_neglected = AsyncMock(side_effect=RuntimeError("neo4j down"))
        result = agg.get_neglected_contacts()
        assert result == []

    # --- protocol compliance ---

    def test_satisfies_protocol(self):
        from colony_sidecar.briefings.aggregators import RelationshipAggregatorProtocol

        graph = MagicMock()
        scorer = MagicMock()
        agg = RelationshipAggregator(scorer, graph)
        assert isinstance(agg, RelationshipAggregatorProtocol)


# ---------------------------------------------------------------------------
# CalendarAggregator
# ---------------------------------------------------------------------------


class TestCalendarAggregator:
    def _make_agg(self, events=None):
        cal = MagicMock()
        cal.list_events = AsyncMock(return_value=events or [])
        return CalendarAggregator(cal)

    # --- get_today_events ---

    def test_get_today_events_empty(self):
        agg = self._make_agg(events=[])
        result = agg.get_today_events("2026-03-25", "UTC")
        assert result == []

    def test_get_today_events_returns_matching_day(self):
        ev = _fake_event_data(
            title="Standup",
            start=datetime(2026, 3, 25, 9, 0, tzinfo=timezone.utc),
        )
        agg = self._make_agg(events=[ev])
        result = agg.get_today_events("2026-03-25", "UTC")
        assert len(result) == 1
        assert result[0].title == "Standup"
        assert result[0].time == "09:00"

    def test_get_today_events_excludes_other_days(self):
        ev_today = _fake_event_data(
            title="Today",
            start=datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc),
        )
        ev_tomorrow = _fake_event_data(
            title="Tomorrow",
            start=datetime(2026, 3, 26, 10, 0, tzinfo=timezone.utc),
        )
        agg = self._make_agg(events=[ev_today, ev_tomorrow])
        result = agg.get_today_events("2026-03-25", "UTC")
        assert len(result) == 1
        assert result[0].title == "Today"

    def test_get_today_events_sorted_by_time(self):
        ev_late = _fake_event_data(
            id="e2",
            title="Late",
            start=datetime(2026, 3, 25, 14, 0, tzinfo=timezone.utc),
        )
        ev_early = _fake_event_data(
            id="e1",
            title="Early",
            start=datetime(2026, 3, 25, 8, 0, tzinfo=timezone.utc),
        )
        agg = self._make_agg(events=[ev_late, ev_early])
        result = agg.get_today_events("2026-03-25", "UTC")
        assert result[0].title == "Early"
        assert result[1].title == "Late"

    def test_get_today_events_prep_needed_with_video_link(self):
        ev = _fake_event_data(
            start=datetime(2026, 3, 25, 9, 0, tzinfo=timezone.utc),
            video_link="https://meet.google.com/abc",
        )
        agg = self._make_agg(events=[ev])
        result = agg.get_today_events("2026-03-25", "UTC")
        assert result[0].prep_needed is True

    def test_get_today_events_prep_needed_with_many_attendees(self):
        ev = _fake_event_data(
            start=datetime(2026, 3, 25, 9, 0, tzinfo=timezone.utc),
            attendees=["a@example.com", "b@example.com"],
        )
        agg = self._make_agg(events=[ev])
        result = agg.get_today_events("2026-03-25", "UTC")
        assert result[0].prep_needed is True

    def test_get_today_events_no_prep_for_solo_no_video(self):
        ev = _fake_event_data(
            start=datetime(2026, 3, 25, 9, 0, tzinfo=timezone.utc),
            attendees=["only@example.com"],
        )
        agg = self._make_agg(events=[ev])
        result = agg.get_today_events("2026-03-25", "UTC")
        assert result[0].prep_needed is False

    def test_get_today_events_timezone_conversion(self):
        # 2026-03-25 23:00 UTC = 2026-03-26 00:00 CET+1 (approx)
        # but for simplicity use US/Eastern (UTC-4 in March DST)
        # 2026-03-25 05:00 UTC = 2026-03-25 01:00 America/New_York (EDT=-4)
        ev = _fake_event_data(
            title="Night owl",
            start=datetime(2026, 3, 25, 5, 0, tzinfo=timezone.utc),
        )
        agg = self._make_agg(events=[ev])
        result = agg.get_today_events("2026-03-25", "America/New_York")
        assert len(result) == 1
        assert result[0].title == "Night owl"
        assert result[0].time == "01:00"

    def test_get_today_events_returns_empty_on_exception(self):
        cal = MagicMock()
        cal.list_events = AsyncMock(side_effect=RuntimeError("cal error"))
        agg = CalendarAggregator(cal)
        result = agg.get_today_events("2026-03-25", "UTC")
        assert result == []

    # --- get_prep_needed ---

    def test_get_prep_needed_filters_correctly(self):
        ev_prep = CalendarEvent(
            time="09:00",
            title="Big meeting",
            participants=["a@b.com"],
            prep_needed=True,
        )
        ev_no_prep = CalendarEvent(
            time="10:00",
            title="Quick sync",
            participants=[],
            prep_needed=False,
        )
        agg = self._make_agg()
        result = agg.get_prep_needed([ev_prep, ev_no_prep])
        assert result == [ev_prep]

    def test_get_prep_needed_empty_input(self):
        agg = self._make_agg()
        assert agg.get_prep_needed([]) == []

    # --- get_upcoming_week ---

    def test_get_upcoming_week_empty(self):
        agg = self._make_agg(events=[])
        result = agg.get_upcoming_week("2026-03-25", "UTC")
        assert result == []

    def test_get_upcoming_week_returns_events_in_window(self):
        ev_in = _fake_event_data(
            title="In window",
            start=datetime(2026, 3, 26, 10, 0, tzinfo=timezone.utc),
        )
        ev_out = _fake_event_data(
            id="e2",
            title="Out of window",
            start=datetime(2026, 4, 5, 10, 0, tzinfo=timezone.utc),
        )
        agg = self._make_agg(events=[ev_in, ev_out])
        result = agg.get_upcoming_week("2026-03-25", "UTC")
        titles = [r.title for r in result]
        assert "In window" in titles
        assert "Out of window" not in titles

    def test_get_upcoming_week_sorted_chronologically(self):
        ev_wed = _fake_event_data(
            id="e2",
            title="Wednesday",
            start=datetime(2026, 3, 27, 10, 0, tzinfo=timezone.utc),
        )
        ev_mon = _fake_event_data(
            id="e1",
            title="Monday",
            start=datetime(2026, 3, 25, 9, 0, tzinfo=timezone.utc),
        )
        agg = self._make_agg(events=[ev_wed, ev_mon])
        result = agg.get_upcoming_week("2026-03-25", "UTC")
        assert result[0].title == "Monday"
        assert result[1].title == "Wednesday"

    def test_get_upcoming_week_returns_empty_on_exception(self):
        cal = MagicMock()
        cal.list_events = AsyncMock(side_effect=RuntimeError("cal down"))
        agg = CalendarAggregator(cal)
        result = agg.get_upcoming_week("2026-03-25", "UTC")
        assert result == []

    # --- protocol compliance ---

    def test_satisfies_protocol(self):
        from colony_sidecar.briefings.aggregators import CalendarAggregatorProtocol

        agg = self._make_agg()
        assert isinstance(agg, CalendarAggregatorProtocol)


# ---------------------------------------------------------------------------
# Module-level helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_resolve_tz_utc(self):
        import zoneinfo

        tz = _resolve_tz("UTC")
        assert tz == zoneinfo.ZoneInfo("UTC")

    def test_resolve_tz_invalid_falls_back_to_utc(self):
        tz = _resolve_tz("Not/A/Timezone")
        assert tz is timezone.utc

    def test_to_calendar_event_basic(self):
        ev = _fake_event_data(
            title="Demo",
            start=datetime(2026, 3, 25, 14, 30, tzinfo=timezone.utc),
            attendees=["a@b.com", "c@d.com"],
        )
        import zoneinfo

        tz_info = zoneinfo.ZoneInfo("UTC")
        cal_ev = _to_calendar_event(ev, tz_info)
        assert cal_ev.title == "Demo"
        assert cal_ev.time == "14:30"
        assert cal_ev.participants == ["a@b.com", "c@d.com"]
        assert cal_ev.prep_needed is True  # 2 attendees

    def test_to_calendar_event_no_prep_single_attendee(self):
        ev = _fake_event_data(
            start=datetime(2026, 3, 25, 14, 0, tzinfo=timezone.utc),
            attendees=["solo@example.com"],
        )
        import zoneinfo

        tz_info = zoneinfo.ZoneInfo("UTC")
        cal_ev = _to_calendar_event(ev, tz_info)
        assert cal_ev.prep_needed is False


# ---------------------------------------------------------------------------
# Stub implementations (regression guard)
# ---------------------------------------------------------------------------


class TestStubs:
    def test_stub_relationship_returns_empty(self):
        stub = StubRelationshipAggregator()
        assert stub.get_notable_changes(since=datetime.now(timezone.utc)) == []
        assert stub.get_neglected_contacts() == []

    def test_stub_calendar_returns_empty(self):
        stub = StubCalendarAggregator()
        assert stub.get_today_events("2026-03-25", "UTC") == []
        assert stub.get_prep_needed([]) == []
        assert stub.get_upcoming_week("2026-03-25", "UTC") == []
