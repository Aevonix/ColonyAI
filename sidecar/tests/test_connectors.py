"""Read-only connector framework (cognition item 2, Phase C).

Each connector's normalize is pure and tested against a canned fixture (no
live credentials). The manager's mode gate, boundary suppression, and wiring
into the observation store + populator are tested with mocks.
"""

from __future__ import annotations

import pytest

from colony_sidecar.directives import Verdict
from colony_sidecar.connectors import ConnectorManager, Observation, EntityHint
from colony_sidecar.connectors.base import Connector
from colony_sidecar.connectors.imap_email import IMAPEmailConnector
from colony_sidecar.connectors.caldav_calendar import CalendarConnector, _parse_ics
from colony_sidecar.connectors.fs_documents import FSDocumentsConnector
from colony_sidecar.connectors.webhook_pull import WebhookPullConnector, _dig


# -- IMAP email -----------------------------------------------------------

def test_imap_normalize_extracts_person_and_company():
    obs = IMAPEmailConnector().normalize([{
        "message_id": "<a@x>", "from": "Alice Smith <alice@acme.com>",
        "to": "me@x.com", "subject": "Q3 roadmap", "date": "", "snippet": "hi"}])
    assert len(obs) == 1 and obs[0].domain == "email"
    kinds = {(e.kind, e.name) for e in obs[0].entities}
    assert ("person", "Alice Smith") in kinds
    assert ("company", "Acme") in kinds
    assert "Q3 roadmap" in obs[0].text


def test_imap_generic_domain_is_not_a_company():
    obs = IMAPEmailConnector().normalize([{
        "message_id": "<b@x>", "from": "Bob <bob@gmail.com>",
        "subject": "hi", "date": "", "snippet": ""}])
    assert all(e.kind != "company" for e in obs[0].entities)


# -- calendar -------------------------------------------------------------

_ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:evt-1
SUMMARY:Design review
LOCATION:Room 4
DTSTART:20260701T150000Z
ATTENDEE;CN=Carol Jones:mailto:carol@x.com
ORGANIZER;CN=Dave Lee:mailto:dave@x.com
END:VEVENT
END:VCALENDAR"""


def test_ics_parse_and_normalize():
    events = _parse_ics(_ICS)
    assert len(events) == 1 and events[0]["uid"] == "evt-1"
    obs = CalendarConnector().normalize(events)
    assert obs[0].domain == "calendar" and obs[0].external_id == "evt-1"
    kinds = {(e.kind, e.name) for e in obs[0].entities}
    assert ("event", "Design review") in kinds
    assert ("person", "Carol Jones") in kinds
    assert ("person", "Dave Lee") in kinds
    assert ("location", "Room 4") in kinds


def test_ics_line_unfolding():
    # RFC 5545 folding: CRLF + one leading space is removed on unfold, so the
    # content space must sit before the fold point.
    ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:e2\nSUMMARY:Long title that \n "
           "continues here\nEND:VEVENT\nEND:VCALENDAR")
    events = _parse_ics(ics)
    assert events[0]["summary"] == "Long title that continues here"


# -- filesystem -----------------------------------------------------------

def test_fs_normalize_document_entity():
    obs = FSDocumentsConnector().normalize([
        {"path": "/docs/plan.md", "name": "plan.md", "size": 10,
         "mtime": 123.0, "snippet": "roadmap"}])
    assert obs[0].domain == "document"
    assert obs[0].entities[0].kind == "document"
    assert obs[0].entities[0].external_ids["path"] == "/docs/plan.md"


def test_fs_fetch_only_new_files(tmp_path):
    c = FSDocumentsConnector()
    import os
    monkey = {"COLONY_CONNECTOR_FS_PATH": str(tmp_path),
              "COLONY_CONNECTOR_FS_EXTENSIONS": "txt"}
    for k, v in monkey.items():
        os.environ[k] = v
    try:
        (tmp_path / "a.txt").write_text("hello")
        c._last_poll = 0.0
        assert len(c._fetch()) == 1
        # nothing new since a far-future last_poll
        c._last_poll = 9e12
        assert c._fetch() == []
    finally:
        for k in monkey:
            os.environ.pop(k, None)


# -- webhook pull ---------------------------------------------------------

def test_dig_dotted_path():
    data = {"a": {"b": [{"c": 42}]}}
    assert _dig(data, "a.b.0.c") == 42
    assert _dig(data, "a.x.y") is None


def test_webhook_normalize_maps_fields():
    obs = WebhookPullConnector().normalize(
        {"stats": {"mrr": 1200, "users": 55}, "id": "prod-x"},
        field_map={"mrr": "stats.mrr", "users": "stats.users"},
        entity_name="Widget", entity_kind="product", id_field="id")
    assert obs[0].domain == "metrics" and obs[0].external_id == "prod-x"
    assert obs[0].payload["metrics"] == {"mrr": 1200, "users": 55}
    assert obs[0].entities[0].kind == "product"


# -- base contract --------------------------------------------------------

def test_observation_store_row_shape():
    o = Observation(domain="email", external_id="x1", ts=100.0,
                    payload={"subject": "hi"},
                    entities=[EntityHint(kind="person", name="A")], text="t")
    row = o.to_store_row()
    assert row["entity_id"] == "x1"
    assert row["payload"]["_entities"][0]["name"] == "A"
    assert row["payload"]["_text"] == "t"


def test_due_and_mark_polled():
    c = FSDocumentsConnector()
    c._last_poll = 0.0
    assert c.due(now=c.poll_secs + 1) is True
    c.mark_polled(now=1000.0)
    assert c.due(now=1000.0 + c.poll_secs - 1) is False


# -- manager: mode gate + wiring -----------------------------------------

class _StubConnector(Connector):
    name = "stub"
    domain = "email"
    default_poll_secs = 0

    def __init__(self, observations):
        super().__init__()
        self._obs = observations

    @property
    def enabled(self):
        return True

    def poll(self):
        return self._obs


class _MockObsStore:
    def __init__(self):
        self.batches = []

    def record_batch(self, domain, rows, reported_by=None):
        self.batches.append((domain, rows, reported_by))
        return len(rows)


class _MockPopulator:
    def __init__(self):
        self.texts = []

    async def populate_from_text(self, text, source_id):
        self.texts.append((text, source_id))


class _FakeDirectives:
    def __init__(self, allowed):
        self._v = Verdict(allowed=allowed, reason="ok" if allowed else "blocked")

    def check(self, action):  # noqa: ARG002
        return self._v


def _obs():
    return [Observation(domain="email", external_id="e1", ts=1.0,
                        payload={"subject": "hi"},
                        entities=[EntityHint(kind="person", name="A")],
                        text="Email from A")]


@pytest.mark.asyncio
async def test_manager_off_is_noop(monkeypatch):
    monkeypatch.setenv("COLONY_CONNECTORS_MODE", "off")
    store, pop = _MockObsStore(), _MockPopulator()
    mgr = ConnectorManager(observation_store=store, populator=pop)
    mgr.register(_StubConnector(_obs()))
    report = await mgr.poll_due(now=1000.0)
    assert report["observations"] == 0 and store.batches == [] and pop.texts == []


@pytest.mark.asyncio
async def test_manager_shadow_logs_but_writes_nothing(monkeypatch):
    monkeypatch.setenv("COLONY_CONNECTORS_MODE", "shadow")
    store, pop = _MockObsStore(), _MockPopulator()
    mgr = ConnectorManager(observation_store=store, populator=pop)
    mgr.register(_StubConnector(_obs()))
    report = await mgr.poll_due(now=1000.0)
    assert report["observations"] == 1  # counted
    assert store.batches == [] and pop.texts == []  # but nothing written


@pytest.mark.asyncio
async def test_manager_live_records_and_populates(monkeypatch):
    monkeypatch.setenv("COLONY_CONNECTORS_MODE", "live")
    store, pop = _MockObsStore(), _MockPopulator()
    mgr = ConnectorManager(observation_store=store, populator=pop)
    mgr.register(_StubConnector(_obs()))
    report = await mgr.poll_due(now=1000.0)
    assert report["observations"] == 1 and report["populated"] == 1
    assert store.batches and store.batches[0][0] == "email"
    assert pop.texts and pop.texts[0][0] == "Email from A"


@pytest.mark.asyncio
async def test_manager_boundary_suppresses_ingest(monkeypatch):
    monkeypatch.setenv("COLONY_CONNECTORS_MODE", "live")
    store, pop = _MockObsStore(), _MockPopulator()
    mgr = ConnectorManager(observation_store=store, populator=pop,
                           directive_manager=_FakeDirectives(False))
    mgr.register(_StubConnector(_obs()))
    report = await mgr.poll_due(now=1000.0)
    assert report["skipped_boundary"] == 1 and report["observations"] == 0
    assert store.batches == [] and pop.texts == []


def test_register_default_connectors_only_enabled(monkeypatch):
    for k in ("IMAP", "CALENDAR", "FS", "WEBHOOK"):
        monkeypatch.delenv(f"COLONY_CONNECTOR_{k}_ENABLED", raising=False)
    monkeypatch.setenv("COLONY_CONNECTOR_FS_ENABLED", "true")
    mgr = ConnectorManager()
    n = mgr.register_default_connectors()
    assert n == 1
    assert mgr.status()["connectors"][0]["name"] == "fs"
