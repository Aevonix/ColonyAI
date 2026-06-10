"""Tests for the gap-surfacing initiative generators (v0.17.0).

Covers the loader + generator pairs for capability gaps, knowledge
acquisition, and behavioral correction:

- seeded mock graph data produces initiatives
- graph=None degrades to []
- query exceptions degrade to []
- dedup keys are stable across runs
"""

import pytest
from unittest.mock import MagicMock

from colony_sidecar.intelligence.components.initiative_engine import (
    InitiativeConfig,
    InitiativeEngine,
    InitiativeType,
)


# ----------------------------------------------------------------------
# Graph mocks (same shape as tests/test_initiative_pipeline_v016.py, plus
# async-iteration support since the gap loaders use ``async for record``)
# ----------------------------------------------------------------------


class FakeResult:
    """Async-iterable query result mimicking neo4j's AsyncResult."""

    def __init__(self, records=None, single=None):
        self._records = list(records or [])
        self._single = single

    def __aiter__(self):
        self._iter = iter(self._records)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def single(self):
        return self._single


class FakeSession:
    """Async-context-manager session replaying canned results per run()."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []  # (query, params) tuples

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def run(self, query, **params):
        self.calls.append((query, params))
        if self._results:
            return self._results.pop(0)
        return FakeResult()


class FakeGraphClient:
    def __init__(self, results=None):
        self.database = "neo4j"
        self.session = FakeSession(results or [])
        self.driver = MagicMock()
        self.driver.session = MagicMock(return_value=self.session)


class FailingGraphClient:
    """Graph client whose session() raises — simulates a dead driver."""

    def __init__(self):
        self.database = "neo4j"
        self.driver = MagicMock()
        self.driver.session = MagicMock(
            side_effect=RuntimeError("connection refused")
        )


def _engine(graph, config=None):
    return InitiativeEngine(
        graph_client=graph,
        event_bus=MagicMock(),
        mind_model=MagicMock(),
        config=config or InitiativeConfig(),
    )


CAPABILITY_RECORD = {
    "id": "cap-web-search",
    "name": "web_search",
    "failure_count": 5,
    "last_failure": None,
    "failure_mode": "missing",
}

CONCEPT_RECORD = {
    "id": "concept-1",
    "name": "vector databases",
    "confidence_score": 0.2,
    "encounter_count": 4,
    "domain": "technology",
    "source": "web_search",
}

PATTERN_RECORD = {
    "id": "pat-1",
    "trigger": "responds with 12-hour times",
    "action": "use 24-hour times",
    "recurrence_count": 4,
    "confidence": 0.9,
    "pattern_type": "behavioral",
}


# ----------------------------------------------------------------------
# Capability gap
# ----------------------------------------------------------------------


class TestCapabilityGapGenerator:
    @pytest.mark.asyncio
    async def test_creates_initiatives_from_seeded_graph(self):
        graph = FakeGraphClient(results=[FakeResult(records=[CAPABILITY_RECORD])])
        engine = _engine(graph)

        await engine._load_capability_gaps()
        initiatives = await engine._generate_capability_gap_initiatives()

        assert len(initiatives) == 1
        init = initiatives[0]
        assert init.type == InitiativeType.CAPABILITY_GAP
        assert init.entity_id == "cap-web-search"
        assert init.dedup_key == "capability_gap:cap-web-search"
        assert 0.5 <= init.priority <= 0.75
        assert "5" in init.rationale  # honest: cites the failure count
        assert init.trigger_data["failure_count"] == 5
        assert init.trigger_data["entity_type"] == "capability_gap"

    @pytest.mark.asyncio
    async def test_threshold_default_and_env_override(self, monkeypatch):
        # Default threshold is 3
        graph = FakeGraphClient(results=[FakeResult(records=[CAPABILITY_RECORD])])
        engine = _engine(graph)
        await engine._load_capability_gaps()
        assert graph.session.calls[0][1]["threshold"] == 3

        # COLONY_CAPABILITY_GAP_FAILURES overrides via from_env
        monkeypatch.setenv("COLONY_CAPABILITY_GAP_FAILURES", "7")
        graph2 = FakeGraphClient(results=[FakeResult(records=[CAPABILITY_RECORD])])
        engine2 = _engine(graph2, config=InitiativeConfig.from_env())
        await engine2._load_capability_gaps()
        assert graph2.session.calls[0][1]["threshold"] == 7

    @pytest.mark.asyncio
    async def test_relationship_fallback_variant(self):
        # First (node-based) variant finds nothing; the NEEDS_CAPABILITY
        # relationship variant still surfaces the gap.
        graph = FakeGraphClient(
            results=[FakeResult(records=[]), FakeResult(records=[CAPABILITY_RECORD])]
        )
        engine = _engine(graph)
        await engine._load_capability_gaps()
        initiatives = await engine._generate_capability_gap_initiatives()
        assert len(initiatives) == 1
        assert len(graph.session.calls) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_when_graph_none(self):
        engine = _engine(None)
        await engine._load_capability_gaps()
        initiatives = await engine._generate_capability_gap_initiatives()
        assert initiatives == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_query_exception(self):
        engine = _engine(FailingGraphClient())
        await engine._load_capability_gaps()
        initiatives = await engine._generate_capability_gap_initiatives()
        assert initiatives == []

    @pytest.mark.asyncio
    async def test_dedup_key_stable_across_runs(self):
        graph = FakeGraphClient(results=[FakeResult(records=[CAPABILITY_RECORD])])
        engine = _engine(graph)
        await engine._load_capability_gaps()
        first = await engine._generate_capability_gap_initiatives()
        second = await engine._generate_capability_gap_initiatives()
        assert first[0].dedup_key == second[0].dedup_key == "capability_gap:cap-web-search"

    @pytest.mark.asyncio
    async def test_defensive_on_malformed_context(self):
        engine = _engine(None)
        engine._context["capability_gaps"] = [None]  # not a dict
        initiatives = await engine._generate_capability_gap_initiatives()
        assert initiatives == []


# ----------------------------------------------------------------------
# Knowledge acquisition
# ----------------------------------------------------------------------


class TestKnowledgeAcquisitionGenerator:
    @pytest.mark.asyncio
    async def test_creates_initiatives_from_seeded_graph(self):
        graph = FakeGraphClient(results=[FakeResult(records=[CONCEPT_RECORD])])
        engine = _engine(graph)

        await engine._load_knowledge_gaps()
        initiatives = await engine._generate_knowledge_acquisition_initiatives()

        assert len(initiatives) == 1
        init = initiatives[0]
        assert init.type == InitiativeType.KNOWLEDGE_ACQUISITION
        assert init.entity_id == "concept-1"
        assert init.dedup_key == "knowledge_gap:concept-1"
        assert 0.5 <= init.priority <= 0.75
        # confidence 0.2 -> 0.5 + 0.8 * 0.25 = 0.7
        assert init.priority == pytest.approx(0.7)
        assert "0.20" in init.rationale  # honest: cites confidence
        assert "vector databases" in init.description

    @pytest.mark.asyncio
    async def test_priority_clamped_at_bounds(self):
        engine = _engine(None)
        engine._context["knowledge_gaps"] = [
            {"id": "c-low", "name": "low", "confidence_score": 0.0, "encounter_count": 1},
            {"id": "c-high", "name": "high", "confidence_score": 1.0, "encounter_count": 1},
        ]
        initiatives = await engine._generate_knowledge_acquisition_initiatives()
        priorities = {i.entity_id: i.priority for i in initiatives}
        assert priorities["c-low"] == pytest.approx(0.75)
        assert priorities["c-high"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_returns_empty_when_graph_none(self):
        engine = _engine(None)
        await engine._load_knowledge_gaps()
        initiatives = await engine._generate_knowledge_acquisition_initiatives()
        assert initiatives == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_query_exception(self):
        engine = _engine(FailingGraphClient())
        await engine._load_knowledge_gaps()
        initiatives = await engine._generate_knowledge_acquisition_initiatives()
        assert initiatives == []

    @pytest.mark.asyncio
    async def test_dedup_key_stable_across_runs(self):
        graph = FakeGraphClient(results=[FakeResult(records=[CONCEPT_RECORD])])
        engine = _engine(graph)
        await engine._load_knowledge_gaps()
        first = await engine._generate_knowledge_acquisition_initiatives()
        second = await engine._generate_knowledge_acquisition_initiatives()
        assert first[0].dedup_key == second[0].dedup_key == "knowledge_gap:concept-1"

    @pytest.mark.asyncio
    async def test_defensive_on_malformed_context(self):
        engine = _engine(None)
        engine._context["knowledge_gaps"] = ["not-a-dict"]
        initiatives = await engine._generate_knowledge_acquisition_initiatives()
        assert initiatives == []


# ----------------------------------------------------------------------
# Behavioral correction
# ----------------------------------------------------------------------


class TestBehavioralCorrectionGenerator:
    @pytest.mark.asyncio
    async def test_creates_initiatives_from_seeded_graph(self):
        graph = FakeGraphClient(results=[FakeResult(records=[PATTERN_RECORD])])
        engine = _engine(graph)

        await engine._load_behavioral_patterns()
        initiatives = await engine._generate_behavioral_correction_initiatives()

        assert len(initiatives) == 1
        init = initiatives[0]
        assert init.type == InitiativeType.BEHAVIORAL_CORRECTION
        assert init.entity_id == "pat-1"
        assert init.dedup_key == "behavioral_correction:pat-1"
        assert 0.5 <= init.priority <= 0.75
        # recurrence 4 -> 0.5 + 4 * 0.05 = 0.7
        assert init.priority == pytest.approx(0.7)
        assert "4" in init.rationale  # honest: cites recurrence count
        assert "24-hour times" in init.rationale  # cites the expected action
        assert "12-hour times" in init.description

    @pytest.mark.asyncio
    async def test_returns_empty_when_graph_none(self):
        engine = _engine(None)
        await engine._load_behavioral_patterns()
        initiatives = await engine._generate_behavioral_correction_initiatives()
        assert initiatives == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_query_exception(self):
        engine = _engine(FailingGraphClient())
        await engine._load_behavioral_patterns()
        initiatives = await engine._generate_behavioral_correction_initiatives()
        assert initiatives == []

    @pytest.mark.asyncio
    async def test_dedup_key_stable_across_runs(self):
        graph = FakeGraphClient(results=[FakeResult(records=[PATTERN_RECORD])])
        engine = _engine(graph)
        await engine._load_behavioral_patterns()
        first = await engine._generate_behavioral_correction_initiatives()
        second = await engine._generate_behavioral_correction_initiatives()
        assert (
            first[0].dedup_key
            == second[0].dedup_key
            == "behavioral_correction:pat-1"
        )

    @pytest.mark.asyncio
    async def test_defensive_on_malformed_context(self):
        engine = _engine(None)
        engine._context["behavioral_patterns"] = [42]
        initiatives = await engine._generate_behavioral_correction_initiatives()
        assert initiatives == []


# ----------------------------------------------------------------------
# Cross-cutting: executors must not call the nonexistent bus .publish()
# ----------------------------------------------------------------------


class TestEventBusApiUsage:
    def test_no_publish_calls_in_executors(self):
        """EventBus exposes emit()/emit_async(); .publish() does not exist.

        Guards against the v0.11.0 regression where DataQualitySkill and
        OperationalHygieneSkill called self.events.publish().
        """
        import inspect

        from colony_sidecar.skills.executors import data_quality, operational_hygiene

        for module in (data_quality, operational_hygiene):
            source = inspect.getsource(module)
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                assert ".publish(" not in stripped, (
                    f"{module.__name__} calls .publish() — EventBus has emit()/emit_async()"
                )

    def test_event_bus_has_emit_not_publish(self):
        from colony_sidecar.events.bus import EventBus

        bus = EventBus()
        assert hasattr(bus, "emit")
        assert hasattr(bus, "emit_async")
        assert not hasattr(bus, "publish")
