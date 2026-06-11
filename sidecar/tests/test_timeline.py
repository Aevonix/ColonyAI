"""Timeline endpoint over the event journal (v0.21.0)."""

import pytest


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("COLONY_EVENT_JOURNAL_DIR", raising=False)
    monkeypatch.setenv("COLONY_AGENT_TIMEZONE", "UTC")
    yield


@pytest.mark.asyncio
async def test_timeline_endpoint_filters_and_digest(tmp_path):
    from colony_sidecar.events.journal import append_event
    from colony_sidecar.api.routers import host

    append_event("conversation.turn", {"contact_id": "cid-a", "summary": "talked about the roadmap"})
    append_event("outreach.sent", {"contact_id": "cid-b", "reason": "checked in"})
    append_event("conversation.turn", {"contact_id": "cid-a", "summary": "second chat"})

    resp = await host.get_timeline(since="24h", types=None, contact_id=None, limit=100)
    assert resp.count == 3
    assert resp.digest and ("💬" in resp.digest or "talked" in resp.digest)
    # newest first
    assert resp.events[0].seq > resp.events[-1].seq
    # humanized + bucketed
    assert resp.events[0].when == "just now" or resp.events[0].when.endswith("ago")
    assert resp.events[0].bucket == "today"

    # contact filter
    r2 = await host.get_timeline(since="24h", types=None, contact_id="cid-a", limit=100)
    assert r2.count == 2 and all(e.contact_id == "cid-a" for e in r2.events)

    # type filter
    r3 = await host.get_timeline(since="24h", types="outreach.sent", contact_id=None, limit=100)
    assert r3.count == 1 and r3.events[0].type == "outreach.sent"

    # narrow window excludes everything just-written? (1s window — written "now")
    r4 = await host.get_timeline(since="2026-01-01T00:00:00+00:00", types=None, contact_id=None, limit=100)
    assert r4.count == 3  # all after Jan 1
