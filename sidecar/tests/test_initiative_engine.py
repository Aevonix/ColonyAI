"""InitiativeEngine active-set accessors: get_active (filters expired) + dismiss.

These live under tests/ so they run in CI, protecting the behaviour against
regression (the equivalent checks in the in-package test_components.py are not
on CI's collection path).
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from colony_sidecar.intelligence.components.initiative_engine import (
    Initiative,
    InitiativeEngine,
    InitiativeType,
)


def _engine(store=None):
    return InitiativeEngine(MagicMock(), MagicMock(), MagicMock(), store=store)


def _initiative(iid, *, priority=0.9, expires_at=None):
    return Initiative(
        id=iid,
        type=InitiativeType.HEALTH,
        description=iid,
        priority=priority,
        rationale="test",
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_get_active_filters_expired():
    ie = _engine()
    ie._initiatives.append(_initiative("expired", expires_at=datetime.now() - timedelta(hours=1)))
    ie._initiatives.append(_initiative("active", expires_at=datetime.now() + timedelta(hours=1)))
    ie._initiatives.append(_initiative("no_expiry"))  # None never expires

    ids = [i.id for i in await ie.get_active()]
    assert "active" in ids
    assert "no_expiry" in ids
    assert "expired" not in ids


@pytest.mark.asyncio
async def test_get_active_sorted_by_priority_desc():
    ie = _engine()
    ie._initiatives.append(_initiative("low", priority=0.2))
    ie._initiatives.append(_initiative("high", priority=0.95))
    ie._initiatives.append(_initiative("mid", priority=0.5))

    ids = [i.id for i in await ie.get_active()]
    assert ids == ["high", "mid", "low"]


@pytest.mark.asyncio
async def test_dismiss_removes_from_active():
    ie = _engine()
    ie._initiatives.append(_initiative("x"))

    assert await ie.dismiss("x") is True
    assert all(i.id != "x" for i in await ie.get_active())
    # Dismissing an unknown id is a no-op.
    assert await ie.dismiss("nope") is False


@pytest.mark.asyncio
async def test_dismiss_records_to_store_when_present():
    store = MagicMock()
    ie = _engine(store=store)
    ie._initiatives.append(_initiative("y"))

    await ie.dismiss("y")
    store.cancel.assert_called_once_with("y")
