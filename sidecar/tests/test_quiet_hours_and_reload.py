"""Delivery timekeeping (U4): the shared quiet-hours predicate and the
rate-limiter restart-reload timezone fix.

The reload bug: DeliveryRateLimiter tracks "today" as the OWNER-LOCAL date
but rebuilt the day window from that date combined with UTC midnight, so a
restart in any non-UTC deployment re-counted the wrong slice of the log
(daily caps bypassed or phantom-exhausted). The window is now local midnight
converted to UTC.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.delivery.rate_limiter import DeliveryRateLimiter
from colony_sidecar.util.quiet_hours import in_quiet_window


# --- shared predicate --------------------------------------------------------

def test_window_spanning_midnight():
    start, end = 22 * 60, 8 * 60
    assert in_quiet_window(23 * 60, start, end)
    assert in_quiet_window(0, start, end)
    assert in_quiet_window(7 * 60 + 59, start, end)
    assert not in_quiet_window(8 * 60, start, end)     # half-open end
    assert not in_quiet_window(12 * 60, start, end)
    assert in_quiet_window(22 * 60, start, end)        # closed start


def test_window_same_day():
    start, end = 13 * 60, 14 * 60
    assert in_quiet_window(13 * 60 + 30, start, end)
    assert not in_quiet_window(14 * 60, start, end)
    assert not in_quiet_window(12 * 60 + 59, start, end)


def test_zero_length_window_disabled():
    assert not in_quiet_window(0, 0, 0)          # 00:00-00:00 = disabled
    assert not in_quiet_window(600, 600, 600)    # any equal pair = disabled


# --- call sites preserve behavior ----------------------------------------------

def test_rate_limiter_quiet_hours_unchanged():
    rl = DeliveryRateLimiter()
    now = datetime.now(rl._tz)
    h = now.hour

    rl._quiet_start, rl._quiet_end = h, (h + 1) % 24        # now inside
    assert rl._in_quiet_hours() is True
    rl._quiet_start, rl._quiet_end = (h + 2) % 24, (h + 3) % 24  # now outside
    assert rl._in_quiet_hours() is False


def _bare_loop(config):
    loop = AutonomyLoop.__new__(AutonomyLoop)
    loop.config = config
    return loop


def test_loop_quiet_hours_unchanged():
    now = datetime.now(timezone.utc)
    inside = _bare_loop(SimpleNamespace(
        timezone="UTC",
        quiet_hours_start=(now - timedelta(minutes=5)).strftime("%H:%M"),
        quiet_hours_end=(now + timedelta(minutes=10)).strftime("%H:%M")))
    assert inside._in_quiet_hours() is True

    disabled = _bare_loop(SimpleNamespace(
        timezone="UTC", quiet_hours_start="00:00", quiet_hours_end="00:00"))
    assert disabled._in_quiet_hours() is False

    unparseable = _bare_loop(SimpleNamespace(
        timezone="UTC", quiet_hours_start="nope", quiet_hours_end="08:00"))
    assert unparseable._in_quiet_hours() is False


# --- reload window fix -----------------------------------------------------------

def test_reload_counts_owner_local_day(tmp_path, monkeypatch):
    """Deliveries after LOCAL midnight (but before UTC midnight) must
    survive a restart; deliveries before local midnight must not."""
    monkeypatch.setenv("COLONY_TIMEZONE", "Pacific/Kiritimati")  # UTC+14
    tz = ZoneInfo("Pacific/Kiritimati")
    db = tmp_path / "deliveries.db"

    local_midnight_utc = datetime.combine(
        datetime.now(tz).date(), datetime.min.time(), tzinfo=tz
    ).astimezone(timezone.utc)

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE delivery_log (person_id TEXT NOT NULL, "
            "delivered_at TEXT NOT NULL)")
        rows = [
            ("p1", (local_midnight_utc + timedelta(minutes=30)).isoformat()),
            ("p1", (local_midnight_utc + timedelta(hours=2)).isoformat()),
            ("p1", (local_midnight_utc - timedelta(minutes=30)).isoformat()),
        ]
        conn.executemany(
            "INSERT INTO delivery_log (person_id, delivered_at) "
            "VALUES (?, ?)", rows)

    rl = DeliveryRateLimiter(db_path=db)
    # Only the two deliveries inside the owner-local day count toward the
    # daily cap; the pre-midnight one still feeds cooldown state.
    assert rl.daily_count("p1") == 2
    assert rl._last_delivery["p1"] == local_midnight_utc + timedelta(hours=2)


def test_reload_utc_deployment_unchanged(tmp_path, monkeypatch):
    """Regression lock: with the default UTC-equivalent timezone the reload
    window is byte-identical to the old behavior."""
    monkeypatch.setenv("COLONY_TIMEZONE", "UTC")
    db = tmp_path / "deliveries.db"
    now = datetime.now(timezone.utc)
    utc_midnight = datetime.combine(
        now.date(), datetime.min.time(), tzinfo=timezone.utc)

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE delivery_log (person_id TEXT NOT NULL, "
            "delivered_at TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO delivery_log (person_id, delivered_at) "
            "VALUES (?, ?)",
            [("p1", (utc_midnight + timedelta(minutes=1)).isoformat()),
             ("p1", (utc_midnight - timedelta(minutes=1)).isoformat())])

    rl = DeliveryRateLimiter(db_path=db)
    assert rl.daily_count("p1") == 1
