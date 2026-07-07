"""P2 wirings: connector-backed calendar briefing sections and
self-referential-query grounding in context assembly."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import colony_sidecar.api.routers.host as host_mod
from colony_sidecar.briefings.aggregators import ConnectorCalendarAggregator
from colony_sidecar.connectors.base import Observation


class _FakeCalendarConnector:
    def __init__(self, observations):
        self._obs = observations
        self.enabled = True

    def poll(self):
        return self._obs


def _obs(hours_from_now, summary, attendees=(), end_offset_min=None,
         location=""):
    # whole seconds only: ICS timestamps carry no sub-second precision
    start = (datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
             ).replace(microsecond=0)
    payload = {"summary": summary, "location": location,
               "attendees": list(attendees), "organizer": "",
               "start": start.strftime("%Y%m%dT%H%M%SZ"),
               "end": ((start + timedelta(minutes=end_offset_min))
                       .strftime("%Y%m%dT%H%M%SZ") if end_offset_min else "")}
    return Observation(domain="calendar", external_id=summary,
                       ts=start.timestamp(), payload=payload, entities=[],
                       text=summary)


def test_calendar_aggregator_today_and_week():
    conn = _FakeCalendarConnector([
        _obs(2, "standup", attendees=["Ann", "Bob"], end_offset_min=45),
        _obs(3, "solo focus block"),
        _obs(24 * 3, "vendor review", attendees=["Ann"]),
        _obs(24 * 30, "far future"),
    ])
    agg = ConnectorCalendarAggregator(conn)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    events = agg.get_today_events(today, "UTC")
    titles = [e.title for e in events]
    assert "standup" in titles and "solo focus block" in titles
    assert "vendor review" not in titles
    standup = next(e for e in events if e.title == "standup")
    assert standup.duration_minutes == 45
    assert standup.prep_needed is True             # >= 2 attendees
    assert agg.get_prep_needed(events) == [standup]

    week = agg.get_upcoming_week(today, "UTC")
    week_titles = [e.title for e in week]
    assert "vendor review" in week_titles
    assert "far future" not in week_titles
    # chronological across DAYS, not by time-of-day string: the day-3 event
    # must come after both day-0 events regardless of its wall-clock time
    assert week_titles.index("vendor review") > week_titles.index("standup")


def test_calendar_aggregator_disabled_or_broken_is_empty():
    agg = ConnectorCalendarAggregator(None)        # falls back to env-gated
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert agg.get_today_events(today, "UTC") == []   # connector disabled


@asynccontextmanager
async def _client():
    app = FastAPI()
    app.include_router(host_mod.router)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def test_context_assemble_self_knowledge_section(monkeypatch):
    import colony_sidecar.identity_bootstrap.self_query as sq
    monkeypatch.setattr(sq, "build_self_context_from_corpus",
                        lambda: "## Colony architecture\n7 layers")

    async with _client() as c:
        ctx = {"session_id": "s1", "contact_id": "c1"}
        r = await c.post("/v1/host/context/assemble", json={
            "identity": {"host_id": "test"}, "context": ctx,
            "incoming_message": {"role": "user",
                                 "content": "what are your capabilities and architecture?"},
        })
        assert r.status_code == 200
        ids = [s["id"] for s in r.json()["sections"]]
        assert "colony-self-knowledge" in ids

        r2 = await c.post("/v1/host/context/assemble", json={
            "identity": {"host_id": "test"}, "context": ctx,
            "incoming_message": {"role": "user",
                                 "content": "remind me to water the plants"},
        })
        assert "colony-self-knowledge" not in [s["id"] for s in r2.json()["sections"]]
