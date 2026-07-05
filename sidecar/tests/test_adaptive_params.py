"""AdaptiveParamStore + the closed meta-learning loop.

The historical bug this guards: StrategyAdjuster persisted a
similarity_threshold that no consumer ever read back (the consolidator
hardcoded 0.92), so the flagship "self-improvement" adjustment had zero
behavioral effect. These tests pin the read-back path end to end.
"""

import pytest

from colony_sidecar.self_model.journal import ActionJournal
from colony_sidecar.self_model.params import (
    PARAM_CONSOLIDATION_THRESHOLD,
    PARAM_RECALL_MIN_RELEVANCE,
    AdaptiveParamStore,
    register_core_params,
)


@pytest.fixture()
def store() -> AdaptiveParamStore:
    s = AdaptiveParamStore()
    register_core_params(s)
    return s


class TestStore:
    def test_registered_default(self, store):
        assert store.get(PARAM_CONSOLIDATION_THRESHOLD) == pytest.approx(0.92)
        assert store.get(PARAM_RECALL_MIN_RELEVANCE) == pytest.approx(0.0)

    def test_set_and_get(self, store):
        applied = store.set(PARAM_RECALL_MIN_RELEVANCE, 0.35, reason="test")
        assert applied == pytest.approx(0.35)
        assert store.get(PARAM_RECALL_MIN_RELEVANCE) == pytest.approx(0.35)

    def test_set_clamps_to_bounds(self, store):
        # A meta-learning adjustment can never mass-merge memories: the
        # consolidation floor is 0.85 no matter what the writer requests.
        applied = store.set(PARAM_CONSOLIDATION_THRESHOLD, 0.5, reason="bad")
        assert applied == pytest.approx(0.85)
        # And retrieval can never be starved: min_relevance caps at 0.5.
        applied = store.set(PARAM_RECALL_MIN_RELEVANCE, 0.9, reason="bad")
        assert applied == pytest.approx(0.5)

    def test_unregistered_param_refused(self, store):
        assert store.set("made.up.knob", 1.0) is None
        assert store.get("made.up.knob", default=7.0) == pytest.approx(7.0)

    def test_reset_restores_default(self, store):
        store.set(PARAM_RECALL_MIN_RELEVANCE, 0.4)
        store.reset(PARAM_RECALL_MIN_RELEVANCE)
        assert store.get(PARAM_RECALL_MIN_RELEVANCE) == pytest.approx(0.0)

    def test_reregistration_keeps_value_but_reclamps(self, store):
        store.set(PARAM_RECALL_MIN_RELEVANCE, 0.4)
        store.register(PARAM_RECALL_MIN_RELEVANCE, default=0.0, lo=0.0,
                       hi=0.2, description="narrowed")
        assert store.get(PARAM_RECALL_MIN_RELEVANCE) == pytest.approx(0.2)

    def test_set_is_journaled(self):
        journal = ActionJournal()
        s = AdaptiveParamStore(journal=journal)
        register_core_params(s)
        s.set(PARAM_RECALL_MIN_RELEVANCE, 0.3, reason="semantic_mismatch gap")
        entries = journal.recent(limit=5)
        assert any(e["domain"] == "meta_learning" and
                   PARAM_RECALL_MIN_RELEVANCE in (e.get("description") or "")
                   for e in entries)

    def test_snapshot_reports_effective(self, store):
        store.set(PARAM_RECALL_MIN_RELEVANCE, 0.25)
        snap = {p["name"]: p for p in store.snapshot()}
        assert snap[PARAM_RECALL_MIN_RELEVANCE]["effective"] == pytest.approx(0.25)
        assert snap[PARAM_CONSOLIDATION_THRESHOLD]["effective"] == pytest.approx(0.92)


class TestConsolidatorReadsBack:
    async def test_consolidator_resolves_threshold_per_run(self, store):
        from colony_sidecar.intelligence.graph.consolidator import (
            MemoryConsolidator,
        )

        class _Graph:
            async def execute(self, query, **params):
                return []

        c = MemoryConsolidator(_Graph(), params=store)
        assert c.similarity_threshold == pytest.approx(0.92)
        store.set(PARAM_CONSOLIDATION_THRESHOLD, 0.96, reason="test")
        await c.run()
        # The adjustment took effect without reconstructing the consolidator.
        assert c.similarity_threshold == pytest.approx(0.96)

    async def test_consolidator_without_params_keeps_default(self):
        from colony_sidecar.intelligence.graph.consolidator import (
            MemoryConsolidator,
        )

        class _Graph:
            async def execute(self, query, **params):
                return []

        c = MemoryConsolidator(_Graph())
        await c.run()
        assert c.similarity_threshold == pytest.approx(0.92)


class TestStrategyAdjusterWritesReadableKnobs:
    def _adjuster(self, store):
        from colony_sidecar.intelligence.cognition.strategy_adjuster import (
            StrategyAdjuster,
        )
        return StrategyAdjuster(graph=object(), params=store)

    async def test_adjust_similarity_threshold_sets_recall_floor(self, store):
        adj = self._adjuster(store)
        result = await adj._adjust_threshold(threshold=0.35)
        assert result["success"] is True
        assert result["param"] == PARAM_RECALL_MIN_RELEVANCE
        assert store.get(PARAM_RECALL_MIN_RELEVANCE) == pytest.approx(0.35)

    async def test_adjust_consolidation_threshold(self, store):
        adj = self._adjuster(store)
        result = await adj._adjust_consolidation_threshold(threshold=0.95)
        assert result["success"] is True
        assert store.get(PARAM_CONSOLIDATION_THRESHOLD) == pytest.approx(0.95)

    async def test_out_of_bounds_request_is_clamped_not_applied_raw(self, store):
        adj = self._adjuster(store)
        result = await adj._adjust_consolidation_threshold(threshold=0.5)
        assert result["success"] is True
        assert result["applied"] == pytest.approx(0.85)

    async def test_without_param_store_adjustment_fails_loudly(self):
        from colony_sidecar.intelligence.cognition.strategy_adjuster import (
            StrategyAdjuster,
        )
        adj = StrategyAdjuster(graph=object())
        result = await adj._adjust_threshold(threshold=0.3)
        assert result["success"] is False
