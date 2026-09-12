"""Canonical ingress, contact-fact lineage and erasure work without a graph.

The API, SQLite stores and source visibility checks are real. No model or
external database is required by this fixture.
"""
import asyncio
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from apsimo.api.routers import host
from apsimo.tom.facts import SharedFactsStore
from apsimo.turns import TurnIdempotencyLedger
from apsimo.turns.idempotency import SourceErased
from test_turn_source_evidence import source_app, envelope, recalled


FACT = "The test hydrofoil departs Friday at nine."


class Extractor:
    def __init__(self):
        self.texts = []
        self.started = asyncio.Event()
        self.release = None

    async def extract_affect(self, text, contact_id, **kwargs):
        self.texts.append(text)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        return None

    async def extract_facts(self, *args, **kwargs):
        raise AssertionError("Canonical ingress must not invoke a second fact extractor")

    async def extract_engagement(self, *args, **kwargs):
        return None


@pytest.fixture
def runtime(source_app, monkeypatch, tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    facts = SharedFactsStore(str(tmp_path / "facts.db"), source_ledger=ledger)
    extractor, tasks = Extractor(), []
    monkeypatch.setattr(host, "_facts_store", facts)
    monkeypatch.setattr(host, "_affect_store", SimpleNamespace())
    monkeypatch.setattr(host, "_engagement_store", None)
    monkeypatch.setattr(host, "_tom_extractor", extractor)
    monkeypatch.setattr(host, "_graph", None)

    def spawn(coro):
        if coro.cr_code.co_name == "_run_tom_extraction":
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task
        coro.close()  # No unrelated cognition/network background jobs in this fixture.

    monkeypatch.setattr(host, "_spawn_task", spawn)
    yield SimpleNamespace(app=source_app, ledger=ledger, facts=facts, extractor=extractor, tasks=tasks)
    for task in tasks:
        if not task.done():
            task.cancel()
    facts.close()


async def ingest(client, runtime, turn_id="turn-a", session="session-a", *, wait=True):
    body = envelope(turn_id)
    body["context"]["session_id"] = session
    body["user_message"]["content"] = FACT
    body["summary"] = "Untrusted generated summary claims departure is Tuesday."
    response = await client.put("/v2/host/turns/" + turn_id, json=body)
    assert response.status_code == 201, response.text
    assert response.json()["source_recorded"]
    if wait:
        await asyncio.wait_for(asyncio.gather(*runtime.tasks), 3)
    return body


async def forget(client, turn_id="turn-a"):
    response = await client.post("/v1/host/memory/sources/forget", json={"contact_id": "contact-a", "source_ids": [turn_id]})
    assert response.status_code == 200, response.text
    return response.json()


def linked_fact(runtime, turn="turn-a"):
    """A contact estimate retains exact support independently of its wording."""
    lineage, _ = runtime.facts.source_input(turn, "contact-a")
    record = runtime.facts.create_fact(contact_id="contact-a", fact=FACT, source="told_by_contact",
        source_lineage=lineage, metadata={"model_provenance": {"model_id": "old-neutral-model"}})
    return record


@pytest.mark.asyncio
async def test_ordinary_contact_knowledge_has_lineage_without_becoming_world_fact(runtime):
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
        body = await ingest(client, runtime)
        assert runtime.facts.list_facts()["total"] == 0
        record = linked_fact(runtime)
        assert record["source_lineage"]["turn_id"] == "turn-a"
        assert len(record["source_lineage"]["message_hashes"]) == 2
        assert record["metadata"]["model_provenance"]["model_id"] == "old-neutral-model"
        assert runtime.extractor.texts == []
        assert FACT in await recalled(client, session="voice-session")
        assert await recalled(client, contact="contact-b") == ""
        result = await forget(client)
        assert result["shared_facts_cleanup"] == "complete"
        assert runtime.facts._conn.execute("SELECT count(*) FROM shared_facts").fetchone()[0] == 0
        assert await recalled(client, session="voice-session") == ""
        replay = await client.put("/v2/host/turns/turn-a", json=body)
        assert replay.json()["skipped_reason"] == "source_erased"
        with pytest.raises(SourceErased):
            runtime.facts.source_input("turn-a", "contact-a")
        assert runtime.facts.list_facts()["total"] == 0


@pytest.mark.asyncio
async def test_ordinary_ingress_does_not_start_retired_affect_extraction(runtime):
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
        await ingest(client, runtime, wait=False)
        assert runtime.tasks == [] and runtime.extractor.texts == []
        await forget(client)
        assert runtime.facts.list_facts()["total"] == 0
        assert await recalled(client, session="voice-session") == ""


@pytest.mark.asyncio
async def test_independent_same_wording_and_unlinked_fact_survive(runtime):
    unlinked = runtime.facts.create_fact(contact_id="contact-a", fact=FACT)
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
        await ingest(client, runtime, "turn-a", "session-a")
        await ingest(client, runtime, "turn-b", "session-b")
        linked_fact(runtime, "turn-a")
        linked_fact(runtime, "turn-b")
        assert runtime.facts.list_facts()["total"] == 3
        await forget(client)
        survivors = runtime.facts.list_facts()["facts"]
        assert len(survivors) == 2 and runtime.facts.get_fact(unlinked["id"]) == unlinked
        assert {r["source_lineage"]["turn_id"] for r in survivors if r.get("source_lineage")} == {"turn-b"}
        assert FACT in await recalled(client, session="voice-session")


@pytest.mark.asyncio
async def test_failed_cleanup_hides_rows_and_blocks_late_writes(runtime, monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
        await ingest(client, runtime)
        record = linked_fact(runtime)
        purge = runtime.facts.purge_erased_sources
        def unavailable(*args, **kwargs):
            raise OSError("facts unavailable")
        monkeypatch.setattr(runtime.facts, "purge_erased_sources", unavailable)
        result = await forget(client)
        assert result["shared_facts_cleanup"] == "pending"
        assert runtime.facts._conn.execute("SELECT count(*) FROM shared_facts").fetchone()[0] == 1
        assert runtime.facts.get_fact(record["id"]) is None
        assert runtime.facts.list_facts()["total"] == 0
        assert await recalled(client, session="voice-session") == ""
        # Late extraction cannot recreate erased support.
        with pytest.raises(SourceErased):
            runtime.facts.create_fact(contact_id="contact-a", fact=FACT, source_lineage=record["source_lineage"])
        # An out-of-band write remains hidden on reopen.
        runtime.facts._conn.execute("UPDATE shared_facts SET id='late-old-writer'")
        runtime.facts._conn.commit()
        reopened = SharedFactsStore(runtime.facts._db_path, source_ledger=runtime.ledger)
        assert reopened.list_facts()["total"] == 0
        reopened.close()
        monkeypatch.setattr(runtime.facts, "purge_erased_sources", purge)
        result = await forget(client)
        assert result["shared_facts_cleanup"] == "complete"
        assert runtime.facts._conn.execute("SELECT count(*) FROM shared_facts").fetchone()[0] == 0


def test_partial_checkpoint_and_missing_origin_never_gain_person_fact(runtime):
    runtime.ledger.record_source("checkpoint", contact_id="contact-a", session_id="s", messages=[{"role": "user", "content": FACT}], scope="session")
    with pytest.raises(SourceErased):
        runtime.facts.source_input("checkpoint", "contact-a")
    with pytest.raises(SourceErased):
        runtime.facts.source_input("absent", "contact-a")
