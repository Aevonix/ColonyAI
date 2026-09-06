"""Canonical ingress avoids duplicate facts; older linked facts still erase.

The API and SQLite stores are real; extraction and graph I/O are controlled.
The graph read fence is the production implementation, not a fixture oracle.
"""
import asyncio
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.routers import host
from colony_sidecar.intelligence.graph.client import ColonyGraph
from colony_sidecar.tom.facts import SharedFactsStore
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.idempotency import SourceErased
from test_turn_source_evidence import source_app, envelope, recalled


FACT = "The test hydrofoil departs Friday at nine."


class Graph:
    _source_projection_erased = staticmethod(ColonyGraph._source_projection_erased)
    _filter_erased_source_memories = ColonyGraph._filter_erased_source_memories

    def __init__(self):
        self.rows = {}
        self.cleanup_fails = False

    async def record_turn(self, *args, **kwargs):
        pass  # Isolate the additional ToM mirror from the separate turn summary.

    async def store_memory(self, **kwargs):
        if self._source_projection_erased(kwargs.get("source_uri")):
            return ""
        key = kwargs["content_hash"]
        self.rows[key] = dict(kwargs, id=key, type="fact", strength=0.9, relevance=0.9)
        return key

    async def recall(self, query, *, person_id=None, **kwargs):
        return [r for r in self.rows.values() if r["person_id"] == person_id and "hydrofoil" in query.lower()]

    async def delete_source_memories(self, turn_ids):
        if self.cleanup_fails:
            raise OSError("graph unavailable")
        selected = {"turn:" + value for value in turn_ids}
        self.rows = {k: r for k, r in self.rows.items() if r["source_uri"] not in selected}


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
    graph, extractor, tasks = Graph(), Extractor(), []
    monkeypatch.setattr(host, "_facts_store", facts)
    monkeypatch.setattr(host, "_affect_store", SimpleNamespace())
    monkeypatch.setattr(host, "_engagement_store", None)
    monkeypatch.setattr(host, "_tom_extractor", extractor)
    monkeypatch.setattr(host, "_graph", graph)

    def spawn(coro):
        if coro.cr_code.co_name == "_run_tom_extraction":
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task
        coro.close()  # No unrelated cognition/network background jobs in this fixture.

    monkeypatch.setattr(host, "_spawn_task", spawn)
    yield SimpleNamespace(app=source_app, ledger=ledger, facts=facts, graph=graph, extractor=extractor, tasks=tasks)
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


async def retained_linked_fact(runtime, turn="turn-a"):
    """A pre-cutover projection, not a new automatic knowledge path."""
    lineage, _ = runtime.facts.source_input(turn, "contact-a")
    record = runtime.facts.create_fact(contact_id="contact-a", fact=FACT, source="told_by_contact",
        source_lineage=lineage, metadata={"model_provenance": {"model_id": "old-neutral-model"}})
    await host._mirror_fact_to_graph(FACT, "contact-a", "told_by_contact", .8, record=record)
    return record


@pytest.mark.asyncio
async def test_ordinary_contact_knowledge_has_lineage_without_becoming_world_fact(runtime):
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
        body = await ingest(client, runtime)
        assert runtime.facts.list_facts()["total"] == 0 and runtime.graph.rows == {}
        record = await retained_linked_fact(runtime)
        assert record["source_lineage"]["turn_id"] == "turn-a"
        assert len(record["source_lineage"]["message_hashes"]) == 2
        assert record["metadata"]["model_provenance"]["model_id"] == "old-neutral-model"
        assert FACT in runtime.extractor.texts[0] and "Tuesday" not in runtime.extractor.texts[0]
        # A later backfill must not reclassify this estimate as a world fact.
        estimate = dict(record, metadata={"automatic_projection": True})
        assert not await host._mirror_fact_to_graph(FACT, "contact-a", "told_by_contact", 0.8, record=estimate)
        assert FACT in await recalled(client, session="voice-session")
        assert await recalled(client, contact="contact-b") == ""
        result = await forget(client)
        assert result["shared_facts_cleanup"] == result["graph_cleanup"] == "complete"
        assert runtime.facts._conn.execute("SELECT count(*) FROM shared_facts").fetchone()[0] == 0
        assert runtime.graph.rows == {}
        assert await recalled(client, session="voice-session") == ""
        replay = await client.put("/v2/host/turns/turn-a", json=body)
        assert replay.json()["skipped_reason"] == "source_erased"
        await host._run_tom_extraction(FACT, "contact-a", source_id="turn-a")
        assert runtime.facts.list_facts()["total"] == 0 and runtime.graph.rows == {}


@pytest.mark.asyncio
async def test_forget_during_actual_ingress_background_extraction(runtime):
    runtime.extractor.release = asyncio.Event()
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
        await ingest(client, runtime, wait=False)
        await asyncio.wait_for(runtime.extractor.started.wait(), 3)
        await forget(client)
        runtime.extractor.release.set()
        await asyncio.wait_for(asyncio.gather(*runtime.tasks), 3)
        assert runtime.facts.list_facts()["total"] == 0
        assert runtime.graph.rows == {}
        assert await recalled(client, session="voice-session") == ""


@pytest.mark.asyncio
async def test_independent_same_wording_and_legacy_support_survive(runtime):
    legacy = runtime.facts.create_fact(contact_id="contact-a", fact=FACT)
    await host._mirror_fact_to_graph(FACT, "contact-a", "shared_context", 0.8, record=legacy)
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
        await ingest(client, runtime, "turn-a", "session-a")
        await ingest(client, runtime, "turn-b", "session-b")
        await retained_linked_fact(runtime, "turn-a")
        await retained_linked_fact(runtime, "turn-b")
        assert len(runtime.graph.rows) == 3
        await forget(client)
        survivors = runtime.facts.list_facts()["facts"]
        assert len(survivors) == 2 and runtime.facts.get_fact(legacy["id"]) == legacy
        assert {r["source_uri"] for r in runtime.graph.rows.values()} == {"tom:shared_fact", "turn:turn-b"}
        assert FACT in await recalled(client, session="voice-session")


@pytest.mark.asyncio
async def test_failed_cleanup_hides_rows_and_blocks_late_backfill(runtime, monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
        await ingest(client, runtime)
        record = await retained_linked_fact(runtime)
        purge = runtime.facts.purge_erased_sources
        def unavailable(*args, **kwargs):
            raise OSError("facts unavailable")
        monkeypatch.setattr(runtime.facts, "purge_erased_sources", unavailable)
        runtime.graph.cleanup_fails = True
        result = await forget(client)
        assert result["shared_facts_cleanup"] == result["graph_cleanup"] == "pending"
        assert runtime.facts._conn.execute("SELECT count(*) FROM shared_facts").fetchone()[0] == 1
        assert len(runtime.graph.rows) == 1
        assert runtime.facts.get_fact(record["id"]) is None
        assert runtime.facts.list_facts()["total"] == 0
        assert await recalled(client, session="voice-session") == ""
        # Captured backfill jobs and late extraction cannot recreate the erased support.
        assert not await host._mirror_fact_to_graph(FACT, "contact-a", "told_by_contact", 0.8, record=record)
        with pytest.raises(SourceErased):
            runtime.facts.create_fact(contact_id="contact-a", fact=FACT, source_lineage=record["source_lineage"])
        # Even an old writer bypassing the write fence remains hidden on reopen.
        runtime.facts._conn.execute("UPDATE shared_facts SET id='late-old-writer'")
        runtime.facts._conn.commit()
        reopened = SharedFactsStore(runtime.facts._db_path, source_ledger=runtime.ledger)
        assert reopened.list_facts()["total"] == 0
        reopened.close()
        monkeypatch.setattr(runtime.facts, "purge_erased_sources", purge)
        runtime.graph.cleanup_fails = False
        result = await forget(client)
        assert result["shared_facts_cleanup"] == result["graph_cleanup"] == "complete"
        assert runtime.facts._conn.execute("SELECT count(*) FROM shared_facts").fetchone()[0] == 0
        assert runtime.graph.rows == {}


def test_partial_checkpoint_and_missing_origin_never_gain_person_fact(runtime):
    runtime.ledger.record_source("checkpoint", contact_id="contact-a", session_id="s", messages=[{"role": "user", "content": FACT}], scope="session")
    with pytest.raises(SourceErased):
        runtime.facts.source_input("checkpoint", "contact-a")
    with pytest.raises(SourceErased):
        runtime.facts.source_input("absent", "contact-a")
