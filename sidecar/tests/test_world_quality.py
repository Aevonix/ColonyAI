"""World-model precision: noise gates + dedup-critical name matching.

Regressions found in live review after the populate-live flip: hyphenated
names never matched in FTS (every mention minted a duplicate entity), URLs
became products, and title-cased operational phrases became persons.
"""

from __future__ import annotations

import pytest

from colony_sidecar.world_model.entities import ProductEntity
from colony_sidecar.world_model.populator import _is_low_quality
from colony_sidecar.world_model.sqlite.backend import SQLiteBackend, _fts_escape


# ---------------------------------------------------------------------------
# Noise gates
# ---------------------------------------------------------------------------

def test_urls_and_paths_rejected_as_names():
    for bad in ("http://127.0.0.1:1234/send", "https://...",
                "www.example.com/page", "some/path/file"):
        assert _is_low_quality(bad, "product"), bad


def test_operational_phrases_rejected_as_persons():
    for bad in ("Root Cause", "Orphan Messages",
                "Initiatives Spawn Fresh Sessions", "Colony Operations"):
        assert _is_low_quality(bad, "person"), bad


def test_real_names_still_pass():
    assert not _is_low_quality("Jordan Reyes", "person")
    assert not _is_low_quality("Hugging Face", "company")
    assert not _is_low_quality("huggingface-hub", "product")


# ---------------------------------------------------------------------------
# FTS escaping + exact-name fallback (dedup-critical)
# ---------------------------------------------------------------------------

def test_fts_escape_preserves_token_boundaries():
    # deleting the hyphen used to fuse tokens into an unmatchable term
    assert _fts_escape("huggingface-hub") == "huggingface hub"
    assert _fts_escape("colony-operations") == "colony operations"
    assert _fts_escape('robert"; DROP TABLE') == "robert DROP TABLE"


@pytest.mark.asyncio
async def test_hyphenated_names_resolve_to_existing_entity(tmp_path):
    backend = SQLiteBackend(str(tmp_path / "wm.db"))
    await backend.connect()
    ent = ProductEntity(id="we-test-1", name="huggingface-hub",
                        entity_type="product", confidence=0.9)
    await backend.upsert_entity(ent)
    found = await backend.find_entities("huggingface-hub", min_confidence=0.0)
    assert any(e.id == "we-test-1" for e in found)
    # exact-name fallback catches it even with a type filter
    found2 = await backend.find_entities("huggingface-hub",
                                         entity_type="product",
                                         min_confidence=0.0)
    assert any(e.id == "we-test-1" for e in found2)
    await backend.close()
