"""Tests for InitiativeEngine graph context loading.

These tests verify that the engine properly queries the graph and mind model
to populate context before generating initiatives.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from colony_sidecar.intelligence.components.initiative_engine import (
    InitiativeEngine,
    InitiativeConfig,
    InitiativeType,
    Initiative,
)


# Helper to create a mock graph with async driver session
class MockGraph:
    """Mock graph that simulates the ColonyGraph driver interface."""
    
    def __init__(self, records=None):
        self.database = "colony"
        self._records = records or []
        
        # Create mock session
        self.session = MagicMock()
        mock_session = AsyncMock()
        
        # Mock the session.run() to return async iterable
        mock_result = AsyncMock()
        
        async def async_iter():
            for r in self._records:
                yield r
        
        mock_result.__aiter__ = lambda self: async_iter()
        mock_session.run = AsyncMock(return_value=mock_result)
        
        # Mock session as async context manager
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        
        self.session.return_value = mock_session
        
        # Create mock driver
        self.driver = MagicMock()
        self.driver.session = self.session


class TestInitiativeConfig:
    """Test InitiativeConfig dataclass."""

    def test_default_values(self):
        config = InitiativeConfig()
        assert config.contact_neglect_days == 7
        assert config.goal_block_threshold_days == 1
        assert config.health_score_threshold == 70.0
        assert config.calendar_gap_threshold_hours == 2.0
        assert config.research_task_age_days == 1
        assert config.signal_accumulation_threshold == 10

    def test_custom_values(self):
        config = InitiativeConfig(
            contact_neglect_days=14,
            health_score_threshold=80.0,
        )
        assert config.contact_neglect_days == 14
        assert config.health_score_threshold == 80.0
        # Other values should be defaults
        assert config.goal_block_threshold_days == 1

    @patch.dict("os.environ", {
        "COLONY_INITIATIVE_CONTACT_NEGLECT_DAYS": "14",
        "COLONY_INITIATIVE_HEALTH_THRESHOLD": "80.0",
        "COLONY_INITIATIVE_SIGNAL_THRESHOLD": "20",
    })
    def test_from_env(self):
        config = InitiativeConfig.from_env()
        assert config.contact_neglect_days == 14
        assert config.health_score_threshold == 80.0
        assert config.signal_accumulation_threshold == 20


class TestGraphContextLoading:
    """Test that graph queries populate context correctly."""

    @pytest.fixture
    def engine(self):
        mock_graph = MockGraph()
        mock_mind = AsyncMock()
        mock_store = MagicMock()
        mock_goal_store = MagicMock()
        return InitiativeEngine(
            graph_client=mock_graph,
            event_bus=AsyncMock(),
            mind_model=mock_mind,
            store=mock_store,
            goal_store=mock_goal_store,
            config=InitiativeConfig(),
        )

    @pytest.mark.asyncio
    async def test_load_blocked_goals(self, engine):
        """Test loading blocked goals from graph."""
        # Set up mock records
        engine.graph._records = [
            {
                "id": "goal-1",
                "title": "Test Goal",
                "description": "A test goal",
                "blocked_at": datetime.now(timezone.utc).isoformat(),
                "priority": 0.8,
            }
        ]

        await engine._load_blocked_goals()

        assert len(engine._context["pending_tasks"]) == 1
        assert engine._context["pending_tasks"][0]["entity_id"] == "goal-1"
        assert engine._context["pending_tasks"][0]["description"] == "Test Goal"
        # Verify driver session was used
        engine.graph.driver.session.assert_called_once()

    @pytest.mark.asyncio
    async def test_load_blocked_goals_no_graph(self):
        """Test that loading works when graph is None."""
        engine = InitiativeEngine(
            graph_client=None,
            event_bus=AsyncMock(),
            mind_model=AsyncMock(),
        )
        
        await engine._load_blocked_goals()
        
        # Should not raise, context should be empty or not set
        assert "pending_tasks" not in engine._context or engine._context["pending_tasks"] == []

    @pytest.mark.asyncio
    async def test_load_blocked_goals_no_driver(self):
        """Test that loading works when graph has no driver."""
        mock_graph = MagicMock()
        del mock_graph.driver  # Remove driver attribute
        
        engine = InitiativeEngine(
            graph_client=mock_graph,
            event_bus=AsyncMock(),
            mind_model=AsyncMock(),
        )
        
        await engine._load_blocked_goals()
        
        assert "pending_tasks" not in engine._context or engine._context["pending_tasks"] == []

    @pytest.mark.asyncio
    async def test_load_blocked_goals_query_failure(self, engine):
        """Test graceful handling of graph query failure."""
        # Make session raise exception
        engine.graph.driver.session.side_effect = Exception("Neo4j connection failed")

        await engine._load_blocked_goals()

        # Should not raise, context should be empty
        assert engine._context.get("pending_tasks") == []

    @pytest.mark.asyncio
    async def test_load_neglected_contacts(self, engine):
        """Test loading neglected contacts from graph."""
        engine.graph._records = [
            {
                "id": "person-1",
                "name": "Alice",
                "last_interaction": None,
            },
            {
                "id": "person-2",
                "name": "Bob",
                "last_interaction": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
            },
        ]

        await engine._load_neglected_contacts()

        assert len(engine._context["neglected_contacts"]) == 2
        assert engine._context["neglected_contacts"][0]["name"] == "Alice"
        assert engine._context["neglected_contacts"][1]["name"] == "Bob"

    @pytest.mark.asyncio
    async def test_load_health_trends(self, engine):
        """Test loading health trends from mind model."""
        engine.mind_model.get_health_state.return_value = {
            "sleep_score": 65,
            "recovery_score": 80,
            "hrv_trend": -15,
        }

        await engine._load_health_trends()

        # Should generate alert for sleep_score (65 < 70)
        # Should NOT generate alert for recovery_score (80 >= 70)
        # Should generate alert for hrv_trend (-15 < -10)
        alerts = engine._context["health_alerts"]
        assert len(alerts) == 2
        assert alerts[0]["metric"] == "sleep_score"
        assert alerts[1]["metric"] == "hrv_trend"

    @pytest.mark.asyncio
    async def test_load_health_trends_no_mind_model(self):
        """Test that loading works when mind_model is None."""
        engine = InitiativeEngine(
            graph_client=MockGraph(),
            event_bus=AsyncMock(),
            mind_model=None,
        )
        
        await engine._load_health_trends()
        
        assert "health_alerts" not in engine._context or engine._context["health_alerts"] == []

    @pytest.mark.asyncio
    async def test_load_scheduling_opportunities(self, engine):
        """Test loading scheduling opportunities from mind model."""
        engine.mind_model.get_schedule_state.return_value = {
            "gaps": [
                {"start": "09:00", "end": "12:00", "duration_hours": 3.0},
                {"start": "14:00", "end": "15:00", "duration_hours": 1.0},  # Below threshold
            ],
            "overdue_commitments": [
                {"title": "Review PR", "days_overdue": 2},
            ],
        }

        await engine._load_scheduling_opportunities()

        opportunities = engine._context["scheduling_opportunities"]
        # Should have 1 gap (3h > 2h threshold) + 1 overdue = 2 total
        assert len(opportunities) == 2
        assert "Free block: 3.0 hours" in opportunities[0]["description"]
        assert "Overdue: Review PR" in opportunities[1]["description"]

    @pytest.mark.asyncio
    async def test_load_pending_signals(self, engine):
        """Test loading pending signals count."""
        engine.mind_model.get_pending_signal_count.return_value = 15

        await engine._load_pending_signals()

        # 15 > threshold of 10, should add scheduling opportunity
        opportunities = engine._context.get("scheduling_opportunities", [])
        signal_opps = [o for o in opportunities if "signals" in o["description"]]
        assert len(signal_opps) == 1
        assert "15 unprocessed signals" in signal_opps[0]["description"]

    @pytest.mark.asyncio
    async def test_load_pending_signals_below_threshold(self, engine):
        """Test that signals below threshold don't generate initiative."""
        engine.mind_model.get_pending_signal_count.return_value = 5

        await engine._load_pending_signals()

        # 5 < threshold of 10, should not add anything
        opportunities = engine._context.get("scheduling_opportunities", [])
        signal_opps = [o for o in opportunities if "signals" in o["description"]]
        assert len(signal_opps) == 0

    @pytest.mark.asyncio
    async def test_load_pending_research_tasks(self, engine):
        """Test loading pending research tasks from graph."""
        engine.graph._records = [
            {
                "id": "task-1",
                "title": "Investigate Neo4j performance",
                "description": "Research query optimization",
                "priority": 0.7,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ]

        await engine._load_pending_research_tasks()

        tasks = engine._context["pending_tasks"]
        assert len(tasks) == 1
        assert tasks[0]["entity_id"] == "task-1"
        assert "Research: Investigate Neo4j performance" in tasks[0]["description"]

    @pytest.mark.asyncio
    async def test_load_graph_context_parallel(self, engine):
        """Test that all loaders are called."""
        # Set up mocks to return empty results
        engine.graph._records = []
        engine.mind_model.get_health_state.return_value = {}
        engine.mind_model.get_schedule_state.return_value = {"gaps": [], "overdue_commitments": []}
        engine.mind_model.get_pending_signal_count.return_value = 0

        await engine._load_graph_context()

        # Verify all loaders were called (mind model methods should be called)
        engine.mind_model.get_health_state.assert_called_once()
        engine.mind_model.get_schedule_state.assert_called_once()
        engine.mind_model.get_pending_signal_count.assert_called_once()

    @pytest.mark.asyncio
    async def test_load_graph_context_caching(self, engine):
        """Test that rapid calls use cache."""
        engine.graph._records = []
        engine.mind_model.get_health_state.return_value = {}
        engine.mind_model.get_schedule_state.return_value = {"gaps": [], "overdue_commitments": []}
        engine.mind_model.get_pending_signal_count.return_value = 0

        # First call should load
        await engine._load_graph_context()
        call_count_after_first = engine.graph.driver.session.call_count

        # Second immediate call should use cache
        await engine._load_graph_context()
        call_count_after_second = engine.graph.driver.session.call_count

        # Should not have made additional driver calls
        assert call_count_after_first == call_count_after_second

    @pytest.mark.asyncio
    async def test_clear_context_resets_graph_load(self, engine):
        """Test that clear_context() resets _last_graph_load (Bug 37)."""
        engine.graph._records = []
        engine.mind_model.get_health_state.return_value = {}
        engine.mind_model.get_schedule_state.return_value = {"gaps": [], "overdue_commitments": []}
        engine.mind_model.get_pending_signal_count.return_value = 0

        # First call should set _last_graph_load
        await engine._load_graph_context()
        assert engine._last_graph_load is not None
        call_count_after_first = engine.graph.driver.session.call_count

        # clear_context should reset _last_graph_load
        engine.clear_context()
        assert engine._last_graph_load is None

        # Next call should reload (not use cache)
        await engine._load_graph_context()
        call_count_after_second = engine.graph.driver.session.call_count
        assert call_count_after_second > call_count_after_first

    @pytest.mark.asyncio
    async def test_load_graph_context_respects_manual_context(self, engine):
        """Test that graph loading skips categories with manually-fed context."""
        # Manually feed some context
        engine._context["pending_tasks"] = [{"description": "Manual task", "days_pending": 1}]
        engine._context["neglected_contacts"] = [{"name": "Manual", "days_since_contact": 5}]
        
        # Set up graph to return data (should be ignored)
        engine.graph._records = [{"id": "graph-1", "title": "Graph task"}]
        engine.mind_model.get_health_state.return_value = {}
        engine.mind_model.get_schedule_state.return_value = {"gaps": [], "overdue_commitments": []}
        engine.mind_model.get_pending_signal_count.return_value = 0

        await engine._load_graph_context()

        # Manual context should be preserved, not overwritten
        assert len(engine._context["pending_tasks"]) == 1
        assert engine._context["pending_tasks"][0]["description"] == "Manual task"
        assert len(engine._context["neglected_contacts"]) == 1
        assert engine._context["neglected_contacts"][0]["name"] == "Manual"
        
        # Health and scheduling should still load (no manual context)
        engine.mind_model.get_health_state.assert_called_once()
        engine.mind_model.get_schedule_state.assert_called_once()


class TestGenerateWithGraphContext:
    """Test that generate() properly uses loaded graph context."""

    @pytest.fixture
    def engine(self):
        mock_graph = MockGraph()
        mock_mind = AsyncMock()
        return InitiativeEngine(
            graph_client=mock_graph,
            event_bus=AsyncMock(),
            mind_model=mock_mind,
            config=InitiativeConfig(),
        )

    @pytest.mark.asyncio
    async def test_generate_populates_context(self, engine):
        """Test that generate() calls _load_graph_context()."""
        engine.graph._records = []
        engine.mind_model.get_health_state.return_value = {}
        engine.mind_model.get_schedule_state.return_value = {"gaps": [], "overdue_commitments": []}
        engine.mind_model.get_pending_signal_count.return_value = 0

        await engine.generate()

        # Should have attempted to load data
        engine.mind_model.get_health_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_creates_initiatives_from_context(self, engine):
        """Test that initiatives are generated from loaded context."""
        # Set up graph to return blocked goals
        engine.graph._records = [
            {
                "id": "goal-1",
                "title": "Fix bug",
                "blocked_at": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
                "priority": 0.9,
            }
        ]
        engine.mind_model.get_health_state.return_value = {}
        engine.mind_model.get_schedule_state.return_value = {"gaps": [], "overdue_commitments": []}
        engine.mind_model.get_pending_signal_count.return_value = 0

        initiatives = await engine.generate(min_priority=0.3)

        # Should generate follow-up initiative from blocked goal
        assert len(initiatives) >= 1
        assert any(i.type == InitiativeType.FOLLOW_UP for i in initiatives)

    @pytest.mark.asyncio
    async def test_generate_with_graph_failure(self, engine):
        """Test that generation works even if graph is down."""
        engine.graph.driver.session.side_effect = Exception("Neo4j down")
        engine.mind_model.get_health_state.return_value = {}
        engine.mind_model.get_schedule_state.return_value = {"gaps": [], "overdue_commitments": []}
        engine.mind_model.get_pending_signal_count.return_value = 0

        # Should not raise
        initiatives = await engine.generate()

        # Should return empty list (no context loaded)
        assert isinstance(initiatives, list)


class TestLifecycleMethods:
    """Test initiative lifecycle methods."""

    @pytest.fixture
    def engine(self):
        return InitiativeEngine(
            graph_client=MockGraph(),
            event_bus=AsyncMock(),
            mind_model=AsyncMock(),
            store=MagicMock(),
            goal_store=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_complete(self, engine):
        """Test completing an initiative."""
        engine._store.complete = MagicMock(return_value=None)
        engine._store.get = MagicMock(return_value=MagicMock(entity_id="goal-123"))
        engine._goal_store.complete_task = MagicMock()

        await engine.complete("init-1", result="Done!")

        engine._store.complete.assert_called_once()
        # Bug 47: Verify goal_store.complete_task called with entity_id, not initiative_id
        engine._goal_store.complete_task.assert_called_once_with("goal-123", result="Done!")

    @pytest.mark.asyncio
    async def test_complete_without_store(self):
        """Test completing when no store is available."""
        engine = InitiativeEngine(
            graph_client=MockGraph(),
            event_bus=AsyncMock(),
            mind_model=AsyncMock(),
            store=None,
        )

        # Should not raise
        await engine.complete("init-1", result="Done!")

    @pytest.mark.asyncio
    async def test_acknowledge(self, engine):
        """Test acknowledging an initiative."""
        engine._store.update = MagicMock(return_value=None)

        await engine.acknowledge("init-1")

        engine._store.update.assert_called_once()
        # Verify it was called with correct args
        call_args = engine._store.update.call_args
        assert call_args is not None
        # Check that initiative_id and status are in the call
        all_args = list(call_args[0]) + list(call_args[1].values())
        assert "init-1" in all_args
        assert "acknowledged" in all_args

    @pytest.mark.asyncio
    async def test_acknowledge_without_store(self):
        """Test acknowledging when no store is available."""
        engine = InitiativeEngine(
            graph_client=MockGraph(),
            event_bus=AsyncMock(),
            mind_model=AsyncMock(),
            store=None,
        )

        # Should not raise
        await engine.acknowledge("init-1")

    @pytest.mark.asyncio
    async def test_dismiss(self, engine):
        """Test dismissing an initiative."""
        engine._store.cancel = MagicMock()

        await engine.dismiss("init-1")

        engine._store.cancel.assert_called_once_with(
            "init-1",
            cancelled_by="initiative_engine",
            reason="dismissed",
        )


class TestInitiativeGeneration:
    """Test specific initiative generation from context."""

    @pytest.fixture
    def engine(self):
        return InitiativeEngine(
            graph_client=MockGraph(),
            event_bus=AsyncMock(),
            mind_model=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_follow_up_from_pending_task(self, engine):
        """Test generating follow-up from pending task context."""
        engine._context = {
            "pending_tasks": [
                {
                    "entity_id": "task-1",
                    "description": "Fix critical bug",
                    "days_pending": 3,
                    "priority": 0.9,  # High graph priority
                }
            ]
        }

        initiatives = await engine._generate_follow_ups()

        assert len(initiatives) == 1
        assert initiatives[0].type == InitiativeType.FOLLOW_UP
        assert "Fix critical bug" in initiatives[0].description
        # Bug 20: Priority should blend graph priority (0.9) with days (0.7)
        # Expected: 0.7*0.6 + 0.9*0.4 = 0.78
        assert initiatives[0].priority > 0.7
        assert initiatives[0].priority <= 1.0

    @pytest.mark.asyncio
    async def test_relationship_from_neglected_contact(self, engine):
        """Test generating relationship initiative from neglected contact."""
        engine._context = {
            "neglected_contacts": [
                {
                    "entity_id": "person-1",
                    "name": "Alice",
                    "days_since_contact": 10,
                }
            ]
        }

        initiatives = await engine._generate_relationship_suggestions()

        assert len(initiatives) == 1
        assert initiatives[0].type == InitiativeType.RELATIONSHIP
        assert "Alice" in initiatives[0].description
        assert initiatives[0].priority > 0.5  # 0.3 + 10*0.05 = 0.8

    @pytest.mark.asyncio
    async def test_health_from_alerts(self, engine):
        """Test generating health initiative from health alerts."""
        engine._context = {
            "health_alerts": [
                {
                    "metric": "sleep_score",
                    "value": 65,
                    "target": 70,
                    "rationale": "Sleep score low",
                }
            ]
        }

        initiatives = await engine._generate_health_suggestions()

        assert len(initiatives) == 1
        assert initiatives[0].type == InitiativeType.HEALTH
        assert "sleep" in initiatives[0].description.lower()
        # Bug 44: Verify entity_id and dedup_key are set
        assert initiatives[0].entity_id == "sleep_score"
        assert initiatives[0].dedup_key == "health:sleep_score"

    @pytest.mark.asyncio
    async def test_health_dedup_key_for_cooldown(self, engine):
        """Test that health initiatives have dedup_key for cooldown tracking."""
        engine._context = {
            "health_alerts": [
                {"metric": "sleep_score", "value": 65, "target": 70},
                {"metric": "hrv_trend", "value": -15, "target": 0},
            ]
        }

        initiatives = await engine._generate_health_suggestions()

        assert len(initiatives) == 2
        # Each should have unique dedup_key
        assert initiatives[0].dedup_key != initiatives[1].dedup_key
        assert initiatives[0].dedup_key.startswith("health:")
        assert initiatives[1].dedup_key.startswith("health:")

    @pytest.mark.asyncio
    async def test_scheduling_from_opportunities(self, engine):
        """Test generating scheduling initiative from opportunities."""
        engine._context = {
            "scheduling_opportunities": [
                {
                    "description": "Free block: 3 hours",
                    "priority": 0.5,
                    "rationale": "Good time for deep work",
                    "action_hint": "schedule",
                }
            ]
        }

        initiatives = await engine._generate_scheduling_suggestions()

        assert len(initiatives) == 1
        assert initiatives[0].type == InitiativeType.SCHEDULING
        assert "Free block" in initiatives[0].description
        # Bug 45: Verify dedup_key is set
        assert initiatives[0].dedup_key is not None
        assert initiatives[0].dedup_key.startswith("schedule:")
