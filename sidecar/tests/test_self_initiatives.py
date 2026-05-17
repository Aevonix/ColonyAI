"""Tests for self-initiative generation and execution (v0.11.0)."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from colony_sidecar.intelligence.components.initiative_engine import (
    InitiativeEngine,
    InitiativeType,
    Initiative,
    InitiativeConfig,
)
from colony_sidecar.skills.base import ExecutionResult, InitiativeExecutionContext


class FakeGraphClient:
    """Minimal fake graph client for testing."""

    def __init__(self, records=None):
        self.database = "neo4j"
        self._records = records or []
        self.driver = MagicMock()
        self.driver.session = MagicMock()

    async def ensure_colony_self(self):
        pass


class FakeEventBus:
    pass


class FakeMindModel:
    pass


@pytest.fixture
def engine():
    return InitiativeEngine(
        graph_client=FakeGraphClient(),
        event_bus=FakeEventBus(),
        mind_model=FakeMindModel(),
        config=InitiativeConfig(),
    )


class TestSelfInitiativeGenerators:
    """Test self-initiative generation methods."""

    @pytest.mark.asyncio
    async def test_generate_subsystem_health_initiatives(self, engine):
        engine.add_context("subsystem_health", [
            {"entity_id": "embed_pipeline", "name": "Embed", "status": "degraded", "latency_ms": 1500},
        ])
        initiatives = await engine._generate_subsystem_health_initiatives()
        assert len(initiatives) == 1
        assert initiatives[0].type == InitiativeType.SUBSYSTEM_HEALTH
        assert initiatives[0].entity_id == "embed_pipeline"
        assert initiatives[0].priority > 0.5

    @pytest.mark.asyncio
    async def test_generate_data_quality_initiatives(self, engine):
        engine.add_context("data_quality_issues", [
            {"entity_id": "orphan_memories", "entity_type": "orphan_nodes", "count": 5, "description": "5 orphans"},
        ])
        initiatives = await engine._generate_data_quality_initiatives()
        assert len(initiatives) == 1
        assert initiatives[0].type == InitiativeType.DATA_QUALITY

    @pytest.mark.asyncio
    async def test_generate_operational_initiatives(self, engine):
        engine.add_context("operational_tasks", [
            {"entity_id": "database_backup", "entity_type": "backup", "description": "No backups", "age_days": 10},
        ])
        initiatives = await engine._generate_operational_initiatives()
        assert len(initiatives) == 1
        assert initiatives[0].type == InitiativeType.OPERATIONAL
        assert initiatives[0].priority > 0.5

    @pytest.mark.asyncio
    async def test_generate_capability_gap_empty(self, engine):
        initiatives = await engine._generate_capability_gap_initiatives()
        assert len(initiatives) == 0

    @pytest.mark.asyncio
    async def test_generate_knowledge_acquisition_empty(self, engine):
        initiatives = await engine._generate_knowledge_acquisition_initiatives()
        assert len(initiatives) == 0

    @pytest.mark.asyncio
    async def test_generate_behavioral_correction_empty(self, engine):
        initiatives = await engine._generate_behavioral_correction_initiatives()
        assert len(initiatives) == 0


class TestRelationshipGating:
    """Test that relationship initiatives are gated behind MANAGES edges."""

    @pytest.mark.asyncio
    async def test_relationship_without_manages_skipped(self):
        """Relationship initiatives should NOT generate for contacts without MANAGES edge."""
        graph = FakeGraphClient()
        # Mock the session to return no MANAGES edges
        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={"cnt": 0})
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph.driver.session = MagicMock(return_value=mock_cm)

        engine = InitiativeEngine(
            graph_client=graph,
            event_bus=FakeEventBus(),
            mind_model=FakeMindModel(),
        )
        engine.add_context("neglected_contacts", [
            {"entity_id": "person-1", "name": "Test Person", "days_since_contact": 10},
        ])
        initiatives = await engine._generate_relationship_suggestions()
        assert len(initiatives) == 0

    @pytest.mark.asyncio
    async def test_relationship_with_manages_generated(self):
        """Relationship initiatives SHOULD generate for contacts with MANAGES edge."""
        graph = FakeGraphClient()
        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={"cnt": 1})
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph.driver.session = MagicMock(return_value=mock_cm)

        engine = InitiativeEngine(
            graph_client=graph,
            event_bus=FakeEventBus(),
            mind_model=FakeMindModel(),
        )
        engine.add_context("neglected_contacts", [
            {"entity_id": "person-1", "name": "Test Person", "days_since_contact": 10},
        ])
        initiatives = await engine._generate_relationship_suggestions()
        assert len(initiatives) == 1
        assert initiatives[0].type == InitiativeType.RELATIONSHIP


class TestInitiativeTypeEnum:
    """Test that new initiative types are registered."""

    def test_self_initiative_types_exist(self):
        assert InitiativeType.SUBSYSTEM_HEALTH == "subsystem_health"
        assert InitiativeType.DATA_QUALITY == "data_quality"
        assert InitiativeType.OPERATIONAL == "operational"
        assert InitiativeType.CAPABILITY_GAP == "capability_gap"
        assert InitiativeType.KNOWLEDGE_ACQUISITION == "knowledge_acquisition"
        assert InitiativeType.BEHAVIORAL_CORRECTION == "behavioral_correction"
