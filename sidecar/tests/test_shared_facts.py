"""Tests for SharedFactsStore."""

import os
import tempfile

import pytest

from colony_sidecar.tom.facts import SharedFactsStore


@pytest.fixture
def store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    s = SharedFactsStore(path)
    yield s
    s.close()
    os.unlink(path)


class TestSharedFactsCreate:
    def test_create_basic(self, store):
        result = store.create_fact(
            contact_id="owner",
            fact="Colony v0.3.0 shipped today",
            source="told_to_contact",
        )
        assert result["contact_id"] == "owner"
        assert result["fact"] == "Colony v0.3.0 shipped today"
        assert result["source"] == "told_to_contact"
        assert result["confidence"] == 0.8
        assert result["id"]
        assert result["created_at"]

    def test_create_with_confidence(self, store):
        result = store.create_fact(
            contact_id="owner",
            fact="Something inferred",
            source="inferred",
            confidence=0.5,
        )
        assert result["confidence"] == 0.5

    def test_confidence_clamped_high(self, store):
        result = store.create_fact(
            contact_id="owner", fact="x", source="explicit", confidence=2.0,
        )
        assert result["confidence"] == 1.0

    def test_confidence_clamped_low(self, store):
        result = store.create_fact(
            contact_id="owner", fact="x", source="explicit", confidence=-1.0,
        )
        assert result["confidence"] == 0.0

    def test_create_with_metadata(self, store):
        result = store.create_fact(
            contact_id="owner",
            fact="API key configured",
            source="shared_context",
            metadata={"platform": "moonshot"},
        )
        assert result["metadata"] == {"platform": "moonshot"}

    def test_create_with_expiry(self, store):
        result = store.create_fact(
            contact_id="owner",
            fact="Temporary access",
            source="told_to_contact",
            expires_at="2026-12-31T23:59:59+00:00",
        )
        assert result["expires_at"] == "2026-12-31T23:59:59+00:00"


class TestSharedFactsGet:
    def test_get_existing(self, store):
        created = store.create_fact(contact_id="owner", fact="test", source="explicit")
        result = store.get_fact(created["id"])
        assert result is not None
        assert result["id"] == created["id"]

    def test_get_nonexistent(self, store):
        assert store.get_fact("nope") is None

    def test_metadata_deserialized(self, store):
        created = store.create_fact(
            contact_id="owner", fact="test", source="explicit",
            metadata={"key": "value"},
        )
        result = store.get_fact(created["id"])
        assert result["metadata"] == {"key": "value"}


class TestSharedFactsList:
    def test_list_all(self, store):
        store.create_fact(contact_id="owner", fact="f1", source="explicit")
        store.create_fact(contact_id="alice", fact="f2", source="inferred")
        result = store.list_facts()
        assert result["total"] == 2
        assert len(result["facts"]) == 2

    def test_list_by_contact(self, store):
        store.create_fact(contact_id="owner", fact="f1", source="explicit")
        store.create_fact(contact_id="alice", fact="f2", source="inferred")
        result = store.list_facts(contact_id="owner")
        assert result["total"] == 1
        assert result["facts"][0]["contact_id"] == "owner"

    def test_list_by_source(self, store):
        store.create_fact(contact_id="owner", fact="f1", source="told_by_contact")
        store.create_fact(contact_id="owner", fact="f2", source="inferred")
        result = store.list_facts(source="inferred")
        assert result["total"] == 1
        assert result["facts"][0]["source"] == "inferred"

    def test_list_min_confidence(self, store):
        store.create_fact(contact_id="owner", fact="f1", source="explicit", confidence=0.9)
        store.create_fact(contact_id="owner", fact="f2", source="explicit", confidence=0.3)
        result = store.list_facts(min_confidence=0.5)
        assert result["total"] == 1
        assert result["facts"][0]["confidence"] == 0.9

    def test_list_pagination(self, store):
        for i in range(5):
            store.create_fact(contact_id="owner", fact=f"fact {i}", source="explicit")
        result = store.list_facts(limit=2, offset=0)
        assert len(result["facts"]) == 2
        assert result["total"] == 5

    def test_expired_facts_excluded(self, store):
        store.create_fact(
            contact_id="owner", fact="expired", source="explicit",
            expires_at="2020-01-01T00:00:00+00:00",
        )
        store.create_fact(contact_id="owner", fact="valid", source="explicit")
        result = store.list_facts(contact_id="owner")
        assert result["total"] == 1
        assert result["facts"][0]["fact"] == "valid"


class TestSharedFactsUpdate:
    def test_update_confidence(self, store):
        created = store.create_fact(contact_id="owner", fact="test", source="explicit")
        result = store.update_fact(created["id"], confidence=0.5)
        assert result["confidence"] == 0.5

    def test_update_fact_text(self, store):
        created = store.create_fact(contact_id="owner", fact="old", source="explicit")
        result = store.update_fact(created["id"], fact="new text")
        assert result["fact"] == "new text"

    def test_update_nonexistent(self, store):
        assert store.update_fact("nope", confidence=0.5) is None

    def test_update_nothing(self, store):
        created = store.create_fact(contact_id="owner", fact="test", source="explicit")
        result = store.update_fact(created["id"])
        assert result["fact"] == "test"


class TestSharedFactsDelete:
    def test_delete_existing(self, store):
        created = store.create_fact(contact_id="owner", fact="test", source="explicit")
        assert store.delete_fact(created["id"]) is True
        assert store.get_fact(created["id"]) is None

    def test_delete_nonexistent(self, store):
        assert store.delete_fact("nope") is False


class TestSharedFactsPurge:
    def test_purge_expired(self, store):
        store.create_fact(
            contact_id="owner", fact="expired", source="explicit",
            expires_at="2020-01-01T00:00:00+00:00",
        )
        store.create_fact(contact_id="owner", fact="valid", source="explicit")
        count = store.purge_expired()
        assert count == 1
        result = store.list_facts(contact_id="owner")
        assert result["total"] == 1

    def test_purge_no_expired(self, store):
        store.create_fact(contact_id="owner", fact="valid", source="explicit")
        assert store.purge_expired() == 0


class TestFactGraphMirror:
    """A shared fact must be mirrored into the memory graph as a `fact`
    memory, or semantic recall can never find it (recall.fact_coverage)."""

    @pytest.mark.asyncio
    async def test_mirror_stores_fact_memory(self, monkeypatch):
        import colony_sidecar.api.routers.host as host_mod

        calls = []

        class FakeGraph:
            async def store_memory(self, **kw):
                calls.append(kw)
                return "mem-1"

        monkeypatch.setattr(host_mod, "_graph", FakeGraph())
        await host_mod._mirror_fact_to_graph(
            "Marc prefers plain prose", "cid-1", "inferred", 0.9)
        assert len(calls) == 1
        kw = calls[0]
        assert kw["memory_type"] == "fact"
        assert kw["content"] == "Marc prefers plain prose"
        assert kw["person_id"] == "cid-1"
        assert kw["importance"] == 0.9
        assert kw["content_hash"]          # dedup key present -> idempotent

    @pytest.mark.asyncio
    async def test_mirror_noops_without_graph_or_text(self, monkeypatch):
        import colony_sidecar.api.routers.host as host_mod
        monkeypatch.setattr(host_mod, "_graph", None)
        await host_mod._mirror_fact_to_graph("x", "c", "s", 0.5)   # no crash
        calls = []

        class FakeGraph:
            async def store_memory(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(host_mod, "_graph", FakeGraph())
        await host_mod._mirror_fact_to_graph("   ", "c", "s", 0.5)
        assert calls == []

    @pytest.mark.asyncio
    async def test_mirror_swallows_graph_errors(self, monkeypatch):
        import colony_sidecar.api.routers.host as host_mod

        class BrokenGraph:
            async def store_memory(self, **kw):
                raise RuntimeError("neo4j down")

        monkeypatch.setattr(host_mod, "_graph", BrokenGraph())
        await host_mod._mirror_fact_to_graph("f", "c", "s", 0.5)   # must not raise
