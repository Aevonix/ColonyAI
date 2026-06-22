"""Health staleness calibration (v0.21.25).

`prefetch` (last /context/assemble) is driven by INBOUND conversation turns, not an
internal schedule, so the /health endpoint must not read a normal quiet period as
`degraded`. These tests pin the threshold profile the endpoint uses.
"""

from datetime import datetime, timedelta, timezone

import pytest

from colony_sidecar.telemetry import TelemetryStore

# Mirror the defaults in api/routers/host.py::host_health.
HEALTH_THRESHOLDS = {"sync": 2.0, "tick": 24.0, "initiative": 48.0, "prefetch": 24.0}


def _store(**ages_hours):
    """Build a store whose last_*_at are `ages_hours` in the past (None = unset)."""
    now = datetime.now(timezone.utc)
    s = TelemetryStore(started_at=now)
    for key, hrs in ages_hours.items():
        setattr(s, key, None if hrs is None else now - timedelta(hours=hrs))
    return s


@pytest.mark.asyncio
async def test_quiet_conversation_period_is_not_degraded():
    """Healthy loop + multi-hour conversational idle must NOT flag stale."""
    s = _store(last_sync_at=0.02, last_tick_at=0.02,
               last_initiative_at=1.0, last_prefetch_at=2.3)
    flags = await s.stale_flags(HEALTH_THRESHOLDS)
    assert flags == []


@pytest.mark.asyncio
async def test_prefetch_dead_for_a_full_day_flags():
    """A real integration-down signal (no context requested in >24h) still flags."""
    s = _store(last_sync_at=0.02, last_tick_at=0.02, last_prefetch_at=25.0)
    flags = await s.stale_flags(HEALTH_THRESHOLDS)
    assert "prefetch" in flags


@pytest.mark.asyncio
async def test_stuck_internal_loop_is_still_caught():
    """Loosening prefetch must not mask genuine internal degradation (sync)."""
    s = _store(last_sync_at=3.0, last_tick_at=0.02, last_prefetch_at=0.1)
    flags = await s.stale_flags(HEALTH_THRESHOLDS)
    assert "sync" in flags and "prefetch" not in flags
