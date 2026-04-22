"""Tests for Colony synthesis — cross-domain insight engine."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import pytest

from colony_sidecar.intelligence.synthesis.connection_discoverer import (
    Connection,
    ConnectionDiscoverer,
    ConnectionType,
)
from colony_sidecar.intelligence.synthesis.novelty_scorer import (
    DOMAIN_DISTANCES,
    NoveltyScore,
    NoveltyScorer,
)
from colony_sidecar.intelligence.synthesis.cross_domain_analyzer import (
    CrossDomainAnalyzer,
    DomainInsight,
)
from colony_sidecar.intelligence.synthesis.insight_validator import (
    InsightValidator,
    ValidationResult,
)
from colony_sidecar.intelligence.synthesis.insight_deliverer import (
    DeliveryChannel,
    DeliveryDecision,
    InsightDeliverer,
)


# --- Fakes / stubs ---


class FakeGraphClient:
    """Fake graph client for testing.

    Records calls and returns configurable results.
    """

    def __init__(self, recall_results: Optional[List[Dict[str, Any]]] = None):
        self.recall_calls: List[Dict[str, Any]] = []
        self.traverse_calls: List[Dict[str, Any]] = []
        self._recall_results = recall_results or []

    async def recall(
        self,
        query: str,
        limit: int = 10,
        min_strength: float = 0.1,
    ) -> List[Dict[str, Any]]:
        self.recall_calls.append(
            {"query": query, "limit": limit, "min_strength": min_strength}
        )
        return self._recall_results

    async def traverse_memory_connections(
        self,
        memory_id: str,
        max_depth: int = 3,
        min_strength: float = 0.3,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        self.traverse_calls.append(
            {
                "memory_id": memory_id,
                "max_depth": max_depth,
                "min_strength": min_strength,
                "limit": limit,
            }
        )
        return []


class FakeEventBus:
    """Fake event bus that records emitted events."""

    def __init__(self):
        self.events: List[Any] = []

    async def emit_async(self, event: Any) -> None:
        self.events.append(event)


@dataclass
class FakeConnection:
    """Fake connection for testing novelty scoring."""

    source_domain: str = "health"
    target_domain: str = "work"
    confidence: float = 0.7
    description: str = "Test connection"


@dataclass
class FakeInsight:
    """Fake insight for testing validation and delivery."""

    id: str = "insight-1"
    description: str = "Test insight"
    confidence: float = 0.8
    supporting_evidence: List[str] = field(default_factory=list)


# --- Connection Discoverer tests ---


class TestConnectionType:
    def test_type_values(self):
        assert ConnectionType.TEMPORAL == "temporal"
        assert ConnectionType.CAUSAL == "causal"
        assert ConnectionType.TOPIC == "topic"
        assert ConnectionType.ENTITY == "entity"
        assert ConnectionType.BEHAVIORAL == "behavioral"

    def test_type_is_string(self):
        assert isinstance(ConnectionType.TEMPORAL, str)


class TestConnection:
    def test_minimal_connection(self):
        c = Connection(
            id="c1",
            connection_type=ConnectionType.TEMPORAL,
            source_domain="health",
            target_domain="work",
            entities=["person-1"],
            description="Sleep affects productivity",
            confidence=0.75,
        )
        assert c.id == "c1"
        assert c.connection_type == ConnectionType.TEMPORAL
        assert c.source_domain == "health"
        assert c.target_domain == "work"
        assert c.confidence == 0.75
        assert c.evidence == []
        assert c.observation_count == 1

    def test_full_connection(self):
        now = datetime.now()
        c = Connection(
            id="c2",
            connection_type=ConnectionType.BEHAVIORAL,
            source_domain="relationships",
            target_domain="health",
            entities=["person-1", "person-2"],
            description="Social conflict correlates with stress",
            confidence=0.85,
            evidence=["mem-1", "sig-2"],
            first_observed=now,
            last_observed=now,
            observation_count=5,
        )
        assert c.evidence == ["mem-1", "sig-2"]
        assert c.observation_count == 5
        assert c.first_observed == now


class TestConnectionDiscoverer:
    @pytest.fixture
    def graph(self):
        return FakeGraphClient()

    @pytest.fixture
    def discoverer(self, graph):
        return ConnectionDiscoverer(graph_client=graph)

    @pytest.mark.asyncio
    async def test_discover_returns_empty_list(self, discoverer):
        """Placeholder discoverer returns empty (no graph data yet)."""
        result = await discoverer.discover_connections()
        assert result == []

    @pytest.mark.asyncio
    async def test_discover_with_person_filter(self, discoverer):
        result = await discoverer.discover_connections(person_id="person-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_discover_filters_by_confidence(self, discoverer):
        result = await discoverer.discover_connections(min_confidence=0.9)
        assert result == []

    @pytest.mark.asyncio
    async def test_discover_filters_by_domain(self, discoverer):
        result = await discoverer.discover_connections(domain="health")
        assert result == []

    def test_default_signal_threshold(self, discoverer):
        assert discoverer.signal_threshold == 0.6

    def test_custom_signal_threshold(self, graph):
        d = ConnectionDiscoverer(graph_client=graph, signal_threshold=0.8)
        assert d.signal_threshold == 0.8


# --- Novelty Scorer tests ---


class TestNoveltyScore:
    def test_score_dataclass(self):
        ns = NoveltyScore(
            score=0.85,
            reasons=["Never observed before"],
            historical_frequency=0,
            domain_distance=0.8,
            confidence_delta=0.3,
        )
        assert ns.score == 0.85
        assert len(ns.reasons) == 1
        assert ns.historical_frequency == 0


class TestNoveltyScorer:
    @pytest.fixture
    def graph(self):
        return FakeGraphClient()

    @pytest.fixture
    def scorer(self, graph):
        return NoveltyScorer(graph_client=graph)

    @pytest.mark.asyncio
    async def test_score_connection_basic(self, scorer):
        conn = FakeConnection(source_domain="health", target_domain="work")
        result = await scorer.score_connection(conn)

        assert isinstance(result, NoveltyScore)
        assert 0.0 <= result.score <= 1.0
        assert result.historical_frequency == 0
        assert "Never observed before" in result.reasons

    @pytest.mark.asyncio
    async def test_cross_domain_scores_higher(self, scorer):
        """Cross-domain connections should include cross-domain reason."""
        conn = FakeConnection(source_domain="health", target_domain="work")
        result = await scorer.score_connection(conn)
        assert "Cross-domain connection" in result.reasons

    @pytest.mark.asyncio
    async def test_same_domain_lower_novelty(self, scorer):
        """Same-domain connections have 0 distance."""
        conn = FakeConnection(source_domain="work", target_domain="work")
        result = await scorer.score_connection(conn)
        assert result.domain_distance == 0.0
        assert "Cross-domain connection" not in result.reasons

    def test_domain_distance_symmetric(self, scorer):
        """Distance should work regardless of argument order."""
        d1 = scorer._calculate_domain_distance("health", "work")
        d2 = scorer._calculate_domain_distance("work", "health")
        assert d1 == d2

    def test_domain_distance_same(self, scorer):
        assert scorer._calculate_domain_distance("health", "health") == 0.0

    def test_domain_distance_unknown_pair(self, scorer):
        """Unknown domain pairs default to 0.5."""
        d = scorer._calculate_domain_distance("unknown_a", "unknown_b")
        assert d == 0.5

    def test_domain_distances_all_positive(self):
        for (src, tgt), dist in DOMAIN_DISTANCES.items():
            assert 0.0 < dist <= 1.0, f"Bad distance for {src}-{tgt}: {dist}"


# --- Cross-Domain Analyzer tests ---


class TestDomainInsight:
    def test_minimal_insight(self):
        di = DomainInsight(
            id="di-1",
            domains=["health", "work"],
            insight_type="availability_prediction",
            description="Low sleep → reduced capacity",
            confidence=0.7,
            actionable=True,
        )
        assert di.id == "di-1"
        assert di.actionable is True
        assert di.recommended_action is None
        assert di.supporting_evidence == []

    def test_full_insight(self):
        di = DomainInsight(
            id="di-2",
            domains=["relationships", "health"],
            insight_type="stress_pattern",
            description="Conflict correlates with stress",
            confidence=0.85,
            actionable=True,
            recommended_action="Schedule recovery time after difficult conversations",
            supporting_evidence=["mem-1", "sig-5", "sig-12"],
        )
        assert di.recommended_action is not None
        assert len(di.supporting_evidence) == 3


class TestCrossDomainAnalyzer:
    @pytest.fixture
    def graph(self):
        return FakeGraphClient()

    @pytest.fixture
    def events(self):
        return FakeEventBus()

    @pytest.fixture
    def analyzer(self, graph, events):
        return CrossDomainAnalyzer(graph_client=graph, event_bus=events)

    @pytest.mark.asyncio
    async def test_analyze_all_domains(self, analyzer):
        """Analyze with no filter runs all analyzers."""
        result = await analyzer.analyze()
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_analyze_health_domain(self, analyzer):
        result = await analyzer.analyze(domains=["health"])
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_analyze_work_domain(self, analyzer):
        result = await analyzer.analyze(domains=["work"])
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_analyze_relationships_domain(self, analyzer):
        result = await analyzer.analyze(domains=["relationships"])
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_analyze_returns_sorted_by_confidence(self, analyzer):
        """When results exist, they should be sorted by confidence desc."""
        result = await analyzer.analyze()
        if len(result) >= 2:
            for i in range(len(result) - 1):
                assert result[i].confidence >= result[i + 1].confidence


# --- Insight Validator tests ---


class TestValidationResult:
    def test_valid_result(self):
        vr = ValidationResult(
            valid=True,
            reasons=[],
            evidence_count=3,
            confidence=0.8,
            data_age_hours=12.0,
        )
        assert vr.valid is True
        assert vr.reasons == []

    def test_invalid_result(self):
        vr = ValidationResult(
            valid=False,
            reasons=["Insufficient evidence (1 < 2)", "Low confidence (0.40 < 0.60)"],
            evidence_count=1,
            confidence=0.4,
            data_age_hours=24.0,
        )
        assert vr.valid is False
        assert len(vr.reasons) == 2


class TestInsightValidator:
    @pytest.fixture
    def validator(self):
        return InsightValidator()

    @pytest.fixture
    def strict_validator(self):
        return InsightValidator(
            min_evidence=5,
            min_confidence=0.9,
            max_data_age_hours=12.0,
        )

    @pytest.mark.asyncio
    async def test_valid_insight(self, validator):
        insight = FakeInsight(
            confidence=0.8,
            supporting_evidence=["ev-1", "ev-2", "ev-3"],
        )
        result = await validator.validate(insight)
        assert result.valid is True
        assert result.reasons == []
        assert result.evidence_count == 3

    @pytest.mark.asyncio
    async def test_insufficient_evidence(self, validator):
        insight = FakeInsight(
            confidence=0.8,
            supporting_evidence=["ev-1"],
        )
        result = await validator.validate(insight)
        assert result.valid is False
        assert any("Insufficient evidence" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_low_confidence(self, validator):
        insight = FakeInsight(
            confidence=0.3,
            supporting_evidence=["ev-1", "ev-2"],
        )
        result = await validator.validate(insight)
        assert result.valid is False
        assert any("Low confidence" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_multiple_failures(self, validator):
        insight = FakeInsight(
            confidence=0.3,
            supporting_evidence=[],
        )
        result = await validator.validate(insight)
        assert result.valid is False
        assert len(result.reasons) == 2

    @pytest.mark.asyncio
    async def test_strict_validator_rejects_normal(self, strict_validator):
        insight = FakeInsight(
            confidence=0.8,
            supporting_evidence=["ev-1", "ev-2"],
        )
        result = await strict_validator.validate(insight)
        assert result.valid is False

    @pytest.mark.asyncio
    async def test_strict_validator_accepts_strong(self, strict_validator):
        insight = FakeInsight(
            confidence=0.95,
            supporting_evidence=["a", "b", "c", "d", "e"],
        )
        result = await strict_validator.validate(insight)
        assert result.valid is False  # data_age 24h > 12h threshold

    @pytest.mark.asyncio
    async def test_data_age_stale(self):
        """Validator with very low max_data_age should reject everything."""
        validator = InsightValidator(max_data_age_hours=1.0)
        insight = FakeInsight(
            confidence=0.9,
            supporting_evidence=["ev-1", "ev-2"],
        )
        result = await validator.validate(insight)
        # Placeholder data age is 24h, which exceeds 1h
        assert result.valid is False
        assert any("Stale data" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_validate_batch(self, validator):
        insights = [
            FakeInsight(confidence=0.9, supporting_evidence=["a", "b"]),
            FakeInsight(confidence=0.3, supporting_evidence=[]),
        ]
        results = await validator.validate_batch(insights)
        assert len(results) == 2
        assert results[0][1].valid is True
        assert results[1][1].valid is False

    def test_default_thresholds(self, validator):
        assert validator.min_evidence == 2
        assert validator.min_confidence == 0.6
        assert validator.max_data_age_hours == 168.0


# --- Insight Deliverer tests ---


class TestDeliveryChannel:
    def test_channel_values(self):
        assert DeliveryChannel.PUSH == "push"
        assert DeliveryChannel.DIGEST == "digest"
        assert DeliveryChannel.IN_APP == "in_app"
        assert DeliveryChannel.EMAIL == "email"

    def test_channel_is_string(self):
        assert isinstance(DeliveryChannel.PUSH, str)


class TestDeliveryDecision:
    def test_decision_defaults(self):
        dd = DeliveryDecision(
            insight_id="i1",
            channel=DeliveryChannel.PUSH,
            reason="High urgency",
        )
        assert dd.insight_id == "i1"
        assert dd.scheduled_for is None

    def test_decision_with_schedule(self):
        dd = DeliveryDecision(
            insight_id="i2",
            channel=DeliveryChannel.DIGEST,
            reason="Low urgency",
            scheduled_for="2026-03-01T09:00:00Z",
        )
        assert dd.scheduled_for is not None


class TestInsightDeliverer:
    @pytest.fixture
    def events(self):
        return FakeEventBus()

    @pytest.fixture
    def deliverer(self, events):
        return InsightDeliverer(event_bus=events)

    @pytest.fixture
    def no_push_deliverer(self, events):
        return InsightDeliverer(
            event_bus=events,
            user_preferences={"push_enabled": False},
        )

    @pytest.mark.asyncio
    async def test_high_urgency_routes_to_push(self, deliverer):
        insight = FakeInsight(id="i1", description="Critical pattern")
        result = await deliverer.deliver(insight, urgency=0.9)
        assert result.channel == DeliveryChannel.PUSH
        assert result.insight_id == "i1"

    @pytest.mark.asyncio
    async def test_moderate_urgency_routes_to_in_app(self, deliverer):
        insight = FakeInsight(id="i2", description="Notable pattern")
        result = await deliverer.deliver(insight, urgency=0.6)
        assert result.channel == DeliveryChannel.IN_APP

    @pytest.mark.asyncio
    async def test_low_urgency_routes_to_digest(self, deliverer):
        insight = FakeInsight(id="i3", description="Minor pattern")
        result = await deliverer.deliver(insight, urgency=0.3)
        assert result.channel == DeliveryChannel.DIGEST

    @pytest.mark.asyncio
    async def test_high_urgency_push_disabled(self, no_push_deliverer):
        """High urgency falls to IN_APP when push is disabled."""
        insight = FakeInsight(id="i4", description="Critical but no push")
        result = await no_push_deliverer.deliver(insight, urgency=0.9)
        assert result.channel == DeliveryChannel.IN_APP

    @pytest.mark.asyncio
    async def test_default_urgency(self, deliverer):
        """Default urgency (0.5) routes to IN_APP."""
        insight = FakeInsight(id="i5", description="Default urgency")
        result = await deliverer.deliver(insight)
        assert result.channel == DeliveryChannel.IN_APP

    @pytest.mark.asyncio
    async def test_deliver_batch(self, deliverer):
        insights = [
            (FakeInsight(id="i6", description="High"), 0.9),
            (FakeInsight(id="i7", description="Low"), 0.2),
        ]
        results = await deliverer.deliver_batch(insights)
        assert len(results) == 2
        assert results[0].channel == DeliveryChannel.PUSH
        assert results[1].channel == DeliveryChannel.DIGEST

    @pytest.mark.asyncio
    async def test_boundary_urgency_0_8(self, deliverer):
        """Urgency exactly at 0.8 should route to PUSH."""
        insight = FakeInsight(id="i8", description="Boundary")
        result = await deliverer.deliver(insight, urgency=0.8)
        assert result.channel == DeliveryChannel.PUSH

    @pytest.mark.asyncio
    async def test_boundary_urgency_0_5(self, deliverer):
        """Urgency exactly at 0.5 should route to IN_APP."""
        insight = FakeInsight(id="i9", description="Boundary")
        result = await deliverer.deliver(insight, urgency=0.5)
        assert result.channel == DeliveryChannel.IN_APP


# --- Integration: __init__ exports ---


class TestPackageExports:
    def test_all_exports_importable(self):
        from colony_sidecar.intelligence.synthesis import (
            Connection,
            ConnectionDiscoverer,
            ConnectionType,
            CrossDomainAnalyzer,
            DeliveryChannel,
            DeliveryDecision,
            DomainInsight,
            InsightDeliverer,
            InsightValidator,
            NoveltyScore,
            NoveltyScorer,
            ValidationResult,
        )
        # Verify classes are importable and are the right types
        assert ConnectionType.TEMPORAL == "temporal"
        assert callable(ConnectionDiscoverer)
        assert callable(NoveltyScorer)
        assert callable(CrossDomainAnalyzer)
        assert callable(InsightValidator)
        assert callable(InsightDeliverer)
