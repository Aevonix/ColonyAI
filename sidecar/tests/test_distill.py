"""Distiller thresholds + record_turn distill format (U13).

Locks: env unset -> distiller thresholds are the historical 3/0.5/7 and
record_turn stores the VERBATIM summary (COLONY_DISTILL_TURNS stays a
shadow flag, flip is owner-gated). When distill is on, the stored content
joins lines with '; ' (never an em dash — the text is prompt-injected) and
preserves the user speaker label.
"""

from __future__ import annotations

import pytest

from colony_sidecar.intelligence.graph import client as client_mod
from colony_sidecar.intelligence.graph.distiller import MemoryDistiller


# --- MemoryDistiller threshold knobs -------------------------------------------

class _FakeGraph:
    def __init__(self):
        self.executed = []

    async def execute(self, query, **params):
        self.executed.append((query, params))
        return []


class TestDistillerKnobs:
    def test_defaults_unchanged_without_env(self, monkeypatch):
        for var in ("COLONY_DISTILL_MIN_RECALLS", "COLONY_DISTILL_MIN_STRENGTH",
                    "COLONY_DISTILL_MIN_AGE_DAYS"):
            monkeypatch.delenv(var, raising=False)
        d = MemoryDistiller(_FakeGraph())
        assert d._min_recalls == 3
        assert d._min_strength == 0.5
        assert d._min_age_days == 7

    def test_env_knobs_apply(self, monkeypatch):
        monkeypatch.setenv("COLONY_DISTILL_MIN_RECALLS", "2")
        monkeypatch.setenv("COLONY_DISTILL_MIN_STRENGTH", "0.3")
        monkeypatch.setenv("COLONY_DISTILL_MIN_AGE_DAYS", "1")
        d = MemoryDistiller(_FakeGraph())
        assert d._min_recalls == 2
        assert d._min_strength == 0.3
        assert d._min_age_days == 1

    def test_explicit_args_beat_env(self, monkeypatch):
        monkeypatch.setenv("COLONY_DISTILL_MIN_RECALLS", "2")
        monkeypatch.setenv("COLONY_DISTILL_MIN_STRENGTH", "0.3")
        monkeypatch.setenv("COLONY_DISTILL_MIN_AGE_DAYS", "1")
        d = MemoryDistiller(_FakeGraph(), min_recalls=5, min_strength=0.9,
                            min_age_days=14)
        assert d._min_recalls == 5
        assert d._min_strength == 0.9
        assert d._min_age_days == 14

    def test_invalid_env_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("COLONY_DISTILL_MIN_RECALLS", "banana")
        monkeypatch.setenv("COLONY_DISTILL_MIN_STRENGTH", "")
        d = MemoryDistiller(_FakeGraph())
        assert d._min_recalls == 3
        assert d._min_strength == 0.5

    @pytest.mark.asyncio
    async def test_thresholds_reach_candidate_query(self, monkeypatch):
        monkeypatch.setenv("COLONY_DISTILL_MIN_RECALLS", "4")
        monkeypatch.setenv("COLONY_DISTILL_MIN_STRENGTH", "0.6")
        monkeypatch.setenv("COLONY_DISTILL_MIN_AGE_DAYS", "10")
        graph = _FakeGraph()
        d = MemoryDistiller(graph)
        await d._fetch_candidates()
        _, params = graph.executed[0]
        assert params["min_recalls"] == 4
        assert params["min_strength"] == 0.6
        assert params["min_age_days"] == 10


# --- record_turn distill format --------------------------------------------------

class _RecordTurnFixture:
    def __init__(self):
        self.stored = []
        g = client_mod.ColonyGraph.__new__(client_mod.ColonyGraph)

        async def _store_memory(**kwargs):
            self.stored.append(kwargs)
            return "mem-1"

        g.store_memory = _store_memory
        self.graph = g


_SUMMARY = ("User: my favorite color is blue\n"
            "Agent: noted, I will remember that\n"
            "standalone line")


@pytest.mark.asyncio
async def test_default_stores_verbatim_summary(monkeypatch):
    """Regression lock: COLONY_DISTILL_TURNS unset -> content == summary."""
    monkeypatch.delenv("COLONY_DISTILL_TURNS", raising=False)
    fx = _RecordTurnFixture()
    mid = await fx.graph.record_turn(
        session_id="s1", contact_id="c1", topics=[], entities=["color"],
        tools_used=[], summary=_SUMMARY)
    assert mid == "mem-1"
    assert fx.stored[0]["content"] == _SUMMARY


@pytest.mark.asyncio
async def test_distill_on_joins_with_semicolon_and_keeps_user_speaker(monkeypatch):
    monkeypatch.setenv("COLONY_DISTILL_TURNS", "1")
    fx = _RecordTurnFixture()
    await fx.graph.record_turn(
        session_id="s1", contact_id="c1", topics=[], entities=["color"],
        tools_used=[], summary=_SUMMARY)
    content = fx.stored[0]["content"]
    assert content == ("User: my favorite color is blue; "
                       "noted, I will remember that; standalone line")
    # prompt-injected text: never an em dash, in any join
    assert "—" not in content


@pytest.mark.asyncio
async def test_distill_on_empty_lines_dropped(monkeypatch):
    monkeypatch.setenv("COLONY_DISTILL_TURNS", "1")
    fx = _RecordTurnFixture()
    await fx.graph.record_turn(
        session_id="s1", contact_id="c1", topics=[], entities=[],
        tools_used=[], summary="User: hello\n\nAgent:\nAgent: done")
    assert fx.stored[0]["content"] == "User: hello; done"
