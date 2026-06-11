"""Unit tests for colony_sidecar.util.temporal (v0.21.0)."""

import os
from datetime import datetime, timedelta, timezone

import pytest

from colony_sidecar.util import temporal as T


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    for k in ("COLONY_AGENT_TIMEZONE", "COLONY_DEFAULT_CONTACT_TIMEZONE", "TZ"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_valid_timezone():
    assert T.is_valid_timezone("America/New_York")
    assert T.is_valid_timezone("UTC")
    assert not T.is_valid_timezone("Mars/Phobos")
    assert not T.is_valid_timezone("")
    assert not T.is_valid_timezone(None)


def test_agent_timezone_default_and_set():
    # With nothing configured + isolated state, falls back to system or UTC.
    assert T.is_valid_timezone(T.agent_timezone())
    T.set_agent_timezone("America/New_York")
    assert T.agent_timezone() == "America/New_York"
    # env overrides stored
    os.environ["COLONY_AGENT_TIMEZONE"] = "Europe/London"
    try:
        assert T.agent_timezone() == "Europe/London"
    finally:
        del os.environ["COLONY_AGENT_TIMEZONE"]


def test_set_agent_timezone_invalid():
    with pytest.raises(ValueError):
        T.set_agent_timezone("Not/AZone")


def test_default_contact_tz_roundtrip():
    assert T.default_contact_timezone() is None
    T.set_default_contact_timezone("Asia/Tokyo")
    assert T.default_contact_timezone() == "Asia/Tokyo"
    T.set_default_contact_timezone(None)
    assert T.default_contact_timezone() is None


def test_resolve_communication_timezone_precedence():
    T.set_agent_timezone("America/New_York")
    # override wins
    assert T.resolve_communication_timezone("Asia/Tokyo", "Europe/Paris") == "Europe/Paris"
    # then contact
    assert T.resolve_communication_timezone("Asia/Tokyo", None) == "Asia/Tokyo"
    # then default
    T.set_default_contact_timezone("Europe/Berlin")
    assert T.resolve_communication_timezone(None, None) == "Europe/Berlin"
    # then agent
    T.set_default_contact_timezone(None)
    assert T.resolve_communication_timezone(None, None) == "America/New_York"
    # invalid override ignored
    assert T.resolve_communication_timezone("Asia/Tokyo", "bogus") == "Asia/Tokyo"


def test_parse_iso():
    assert T.parse_iso(None) is None
    a = T.parse_iso("2026-06-11T04:16:25Z")
    assert a.tzinfo is not None and a.year == 2026
    b = T.parse_iso("2026-06-11T04:16:25")  # naive → UTC
    assert b.tzinfo == timezone.utc
    c = T.parse_iso(datetime(2026, 6, 11, tzinfo=timezone.utc))
    assert c.year == 2026


def test_humanize_delta():
    now = datetime.now(timezone.utc)
    assert T.humanize_delta(now) == "just now"
    assert T.humanize_delta(now - timedelta(minutes=30)) == "30m ago"
    assert T.humanize_delta(now - timedelta(hours=6)) == "6h ago"
    assert T.humanize_delta(now - timedelta(days=3)) == "3d ago"
    assert T.humanize_delta(now - timedelta(days=21)) == "3w ago"
    assert T.humanize_delta(now + timedelta(hours=5)).startswith("in 5h")
    assert T.humanize_delta("not-a-date") == "unknown"


def test_bucket():
    T.set_agent_timezone("UTC")
    now = datetime.now(timezone.utc)
    assert T.bucket(now) == "today"
    assert T.bucket(now - timedelta(days=1, hours=2)) in ("yesterday", "earlier this week")
    assert T.bucket(now - timedelta(days=10)) == "last week"


def test_hours_since():
    now = datetime.now(timezone.utc)
    h = T.hours_since(now - timedelta(hours=2))
    assert 1.9 < h < 2.1
    assert T.hours_since(None) is None


def test_now_and_format():
    T.set_agent_timezone("America/New_York")
    n = T.now_in("America/New_York")
    assert n.tzinfo is not None
    s = T.format_clock(n)
    assert "," in s and (":" in s)
    assert T.part_of_day(n) in (
        "the middle of the night", "early morning", "morning",
        "midday", "afternoon", "evening", "night",
    )


def test_parse_relative_since():
    now = datetime.now(timezone.utc)
    # relative
    a = T.parse_iso(T.parse_relative_since("24h"))
    assert 23.9 < (now - a).total_seconds() / 3600 < 24.1
    b = T.parse_iso(T.parse_relative_since("30m"))
    assert 29.0 < (now - b).total_seconds() / 60 < 31.0
    c = T.parse_iso(T.parse_relative_since("7d"))
    assert 6.9 < (now - c).total_seconds() / 86400 < 7.1
    # empty -> default 24h
    d = T.parse_iso(T.parse_relative_since(""))
    assert 23.0 < (now - d).total_seconds() / 3600 < 25.0
    # absolute ISO passes through
    iso = "2026-06-01T00:00:00+00:00"
    assert T.parse_iso(T.parse_relative_since(iso)).date().isoformat() == "2026-06-01"
    # today -> midnight agent-local, in the past
    T.set_agent_timezone("UTC")
    assert T.parse_iso(T.parse_relative_since("today")) <= now


def test_describe_now_includes_both_zones():
    T.set_agent_timezone("America/New_York")
    out = T.describe_now(contact_tz="Asia/Tokyo", contact_label="Ingrid")
    assert "your local time" in out
    assert "Asia/Tokyo" in out
    # same tz → no second line
    out2 = T.describe_now(contact_tz="America/New_York")
    assert "your local time" in out2 and "side it is" not in out2
