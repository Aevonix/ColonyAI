"""U21 — respond_to_initiative closes the loop into TypeFeedbackStore.

The owner's response to an initiative (approved / actioned / dismissed /
snoozed / acknowledged) is recorded as a per-type outcome so the priority
multiplier actually learns. Recording is best-effort: a missing store, a
type-less initiative, or a store exception must never fail the respond.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from colony_sidecar.api.routers import host as host_router


class _FakeInitiativeStore:
    def __init__(self, initiative):
        self._initiative = initiative
        self.history = []

    def get(self, initiative_id):
        return self._initiative

    def update(self, initiative_id, **kwargs):
        for key, value in kwargs.items():
            setattr(self._initiative, key, value)

    def log_history(self, initiative_id, **kwargs):
        self.history.append((initiative_id, kwargs))


class _RecordingFeedbackStore:
    def __init__(self):
        self.records = []

    def record(self, itype, outcome):
        self.records.append((itype, outcome))
        return 1.0


class _ExplodingFeedbackStore:
    def record(self, itype, outcome):
        raise RuntimeError("feedback db is on fire")


def _initiative(itype="check_in"):
    ns = SimpleNamespace(id="init-fb", status="pending", job_id=None)
    if itype is not None:
        ns.type = itype
    return ns


@pytest.fixture
def _stores():
    """Swap in fake initiative + feedback stores, restore afterwards."""
    old_init = host_router._initiative_store
    old_fb = host_router._feedback_store
    yield
    host_router.set_initiative_store(old_init)
    host_router.set_feedback_store(old_fb)


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expected_outcome", [
    ("approved", "actioned"),
    ("actioned", "actioned"),
    ("dismissed", "dismissed"),
    ("snoozed", "snoozed"),
    ("acknowledged", "acknowledged"),
])
async def test_respond_records_type_feedback(_stores, action, expected_outcome):
    host_router.set_initiative_store(_FakeInitiativeStore(_initiative()))
    fb = _RecordingFeedbackStore()
    host_router.set_feedback_store(fb)

    resp = await host_router.respond_to_initiative(
        "init-fb", action=action, details=None,
    )
    assert resp["success"] is True
    assert fb.records == [("check_in", expected_outcome)]


@pytest.mark.asyncio
async def test_respond_skips_feedback_for_unmapped_action(_stores):
    host_router.set_initiative_store(_FakeInitiativeStore(_initiative()))
    fb = _RecordingFeedbackStore()
    host_router.set_feedback_store(fb)

    resp = await host_router.respond_to_initiative(
        "init-fb", action="something_else", details=None,
    )
    assert resp["success"] is True
    assert fb.records == []


@pytest.mark.asyncio
async def test_respond_skips_feedback_when_type_missing(_stores):
    host_router.set_initiative_store(_FakeInitiativeStore(_initiative(itype=None)))
    fb = _RecordingFeedbackStore()
    host_router.set_feedback_store(fb)

    resp = await host_router.respond_to_initiative(
        "init-fb", action="dismissed", details=None,
    )
    assert resp["success"] is True
    assert fb.records == []


@pytest.mark.asyncio
async def test_respond_succeeds_without_feedback_store(_stores):
    """Regression lock: legacy path (no feedback store) is unchanged."""
    store = _FakeInitiativeStore(_initiative())
    host_router.set_initiative_store(store)
    host_router.set_feedback_store(None)

    resp = await host_router.respond_to_initiative(
        "init-fb", action="dismissed", details=None,
    )
    assert resp["success"] is True
    assert resp["status"] == "cancelled"
    assert len(store.history) == 1


@pytest.mark.asyncio
async def test_respond_survives_feedback_store_exception(_stores):
    """Feedback recording can NEVER fail the respond."""
    store = _FakeInitiativeStore(_initiative())
    host_router.set_initiative_store(store)
    host_router.set_feedback_store(_ExplodingFeedbackStore())

    resp = await host_router.respond_to_initiative(
        "init-fb", action="approved", details=None,
    )
    assert resp["success"] is True
    assert resp["status"] == "acknowledged"
    assert len(store.history) == 1
