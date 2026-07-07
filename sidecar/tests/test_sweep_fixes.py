"""Regression tests for the bug/stub sweep: timeline recency, affect totals,
goal aggregator + goal tool shape, and the hourly condition-check phase."""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from colony_sidecar.events.journal import replay_events
from colony_sidecar.tom.affect import AffectStore


# --- journal: newest_first drops the OLD end of an over-cap window -----------

def _write_event(d, seq, recorded_at, etype="test.event"):
    (d / f"{seq:08d}.ulid{seq}.json").write_text(json.dumps({
        "type": etype, "recordedAt": recorded_at, "data": {"n": seq}}))


def test_replay_newest_first_keeps_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_EVENT_JOURNAL_DIR", str(tmp_path))
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(10):
        _write_event(tmp_path, i, (base + timedelta(minutes=i)).isoformat())

    old = replay_events("2000-01-01T00:00:00+00:00", limit=4)
    assert [e["seq"] for e in old["events"]] == [0, 1, 2, 3]      # oldest-first walk
    assert old["hasMore"] is True

    new = replay_events("2000-01-01T00:00:00+00:00", limit=4, newest_first=True)
    assert [e["seq"] for e in new["events"]] == [9, 8, 7, 6]      # the RECENT end
    assert new["hasMore"] is True

    all_new = replay_events("2000-01-01T00:00:00+00:00", limit=50, newest_first=True)
    assert len(all_new["events"]) == 10
    assert all_new["hasMore"] is False


def test_replay_has_more_boundary_and_types(tmp_path, monkeypatch):
    """cap == N-1: the single unprocessed boundary file must count toward
    hasMore (remaining[1:] used to drop it); and hasMore must honor the
    types filter."""
    monkeypatch.setenv("COLONY_EVENT_JOURNAL_DIR", str(tmp_path))
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        _write_event(tmp_path, i, (base + timedelta(minutes=i)).isoformat(),
                     etype="a.type" if i < 4 else "b.type")

    r = replay_events("2000-01-01T00:00:00+00:00", limit=4)
    assert len(r["events"]) == 4
    assert r["hasMore"] is True          # exactly one unread file remains

    # types filter: 4 matching 'a.type' events, cap at 4 — the only remaining
    # file is 'b.type', so hasMore must be False
    r2 = replay_events("2000-01-01T00:00:00+00:00", limit=4, types=["a.type"])
    assert len(r2["events"]) == 4
    assert r2["hasMore"] is False


# --- affect: paginated total is the real count, not the page size ------------

def test_affect_count_events(tmp_path):
    store = AffectStore(db_path=tmp_path / "affect.db")
    for i in range(7):
        store.create_event(contact_id="c1", valence=0.1, source="test")
    store.create_event(contact_id="c2", valence=0.1, source="other")
    page = store.list_events(contact_id="c1", limit=3)
    assert len(page) == 3
    assert store.count_events(contact_id="c1") == 7
    assert store.count_events() == 8
    assert store.count_events(contact_id="c1", source="test") == 7


# --- briefings: GoalEngineAggregator reads real goal state -------------------

@dataclass
class _FakeGoal:
    goal_id: str
    title: str
    deadline: Optional[datetime] = None
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None

    def is_overdue(self):
        return (self.deadline is not None
                and self.deadline < datetime.now(timezone.utc))


class _FakeGoalEngine:
    def __init__(self, by_status):
        self._by_status = by_status

    def list_goals(self, status=None, limit=50, offset=0):
        if status is None:
            out = []
            for v in self._by_status.values():
                out.extend(v)
            return out
        return list(self._by_status.get(status, []))


def test_goal_engine_aggregator(tmp_path):
    from colony_sidecar.briefings.aggregators import GoalEngineAggregator
    now = datetime.now(timezone.utc)
    created = now - timedelta(hours=1)   # explicit: default-now would land after period_end
    overdue = _FakeGoal("g1", "ship the report", deadline=now - timedelta(hours=2),
                        created_at=created)
    soon = _FakeGoal("g2", "prep the demo", deadline=now + timedelta(hours=2),
                     created_at=created)
    later = _FakeGoal("g3", "long-range", deadline=now + timedelta(days=3),
                      created_at=created)
    blocked = _FakeGoal("g4", "waiting on vendor", created_at=created)
    done = _FakeGoal("g5", "already done",
                     created_at=now - timedelta(days=2),
                     completed_at=now - timedelta(days=1))
    agg = GoalEngineAggregator(_FakeGoalEngine({
        "active": [overdue, soon, later], "blocked": [blocked],
        "completed": [done]}))

    assert [g.goal_id for g in agg.get_overdue_goals()] == ["g1"]
    assert [g.goal_id for g in agg.get_blocked_goals()] == ["g4"]
    assert [g.goal_id for g in agg.get_completing_soon(hours=4.0)] == ["g2"]
    stats = agg.get_week_completion_stats(now - timedelta(days=7), now)
    assert stats.total_completed == 1
    assert stats.total_initiated == 5          # all created inside the window
    assert 0.0 < stats.completion_rate <= 1.0


# --- tools: colony_list_goals handler uses the real engine API ---------------

class _Registry:
    def __init__(self, goals):
        self.goals = goals


async def test_handle_list_goals_returns_goals():
    from colony_sidecar.tools.handlers import handle_list_goals
    from enum import Enum

    class _St(str, Enum):
        ACTIVE = "active"

    @dataclass
    class _G:
        goal_id: str
        title: str
        status: _St
        progress_pct: float

    class _Eng:
        def list_goals(self, status=None, limit=50, offset=0):
            return [_G("g1", "test goal", _St.ACTIVE, 0.5)]

    out = json.loads(await handle_list_goals({}, _Registry(_Eng())))
    assert out["count"] == 1
    assert out["goals"][0] == {"id": "g1", "title": "test goal",
                               "status": "active", "progress": 0.5}


# --- autonomy: hourly condition-check phase exists and dedups ----------------

async def test_phase_condition_checks_runs_and_dedups(monkeypatch):
    from colony_sidecar.autonomy.loop import AutonomyLoop
    calls = {"n": 0}

    async def fake_check(params):
        calls["n"] += 1
        return {"condition_met": False}

    import colony_sidecar.autonomy.condition_worker as cw
    monkeypatch.setattr(cw, "_check_commitment_overdue", fake_check)
    monkeypatch.setattr(cw, "_check_affect_decline", fake_check)
    monkeypatch.setattr(cw, "_check_surprise_accumulation", fake_check)

    fake_self = type("S", (), {"_periodic_last": {}})()
    await AutonomyLoop._phase_condition_checks(fake_self)
    assert calls["n"] == 3
    await AutonomyLoop._phase_condition_checks(fake_self)   # same hour → dedup
    assert calls["n"] == 3
