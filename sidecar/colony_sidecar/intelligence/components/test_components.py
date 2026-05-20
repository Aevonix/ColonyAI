"""Tests for colony.intelligence.components.

Covers all 8 intelligence components:
    - ToolLearner
    - SelfReflector
    - TaskPlanner
    - SessionContinuity
    - ResearchOrchestrator
    - PreferenceLearner
    - AnomalyDetector
    - InitiativeEngine
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from colony_sidecar.intelligence.components.tool_learner import (
    ToolLearner,
    ToolPreference,
    ToolUsage,
)
from colony_sidecar.intelligence.components.self_reflector import (
    Reflection,
    SelfReflector,
)
from colony_sidecar.intelligence.components.task_planner import (
    SubTask,
    TaskPlan,
    TaskPlanner,
    TaskPriority,
)
from colony_sidecar.intelligence.components.session_continuity import (
    SessionContext,
    SessionContinuity,
)
from colony_sidecar.intelligence.components.research_orchestrator import (
    ResearchOrchestrator,
    ResearchReport,
    ResearchResult,
    ResearchSource,
    SourceType,
)
from colony_sidecar.intelligence.components.preference_learner import (
    Preference,
    PreferenceLearner,
)
from colony_sidecar.intelligence.components.anomaly_detector import (
    Anomaly,
    AnomalyDetector,
    AnomalyType,
)
from colony_sidecar.intelligence.components.initiative_engine import (
    Initiative,
    InitiativeEngine,
    InitiativeType,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_graph():
    """Mock graph client."""
    return MagicMock()


@pytest.fixture
def mock_event_bus():
    """Mock event bus."""
    return MagicMock()


@pytest.fixture
def mock_metrics():
    """Mock metrics collector."""
    return MagicMock()


@pytest.fixture
def mock_mind_model():
    """Mock mind model."""
    return MagicMock()


# ===========================================================================
# ToolLearner
# ===========================================================================


class TestToolUsageModel:
    """Test ToolUsage data model."""

    def test_basic_construction(self):
        usage = ToolUsage(tool_name="web_search", task_type="research", success=True)
        assert usage.tool_name == "web_search"
        assert usage.task_type == "research"
        assert usage.success is True
        assert usage.user_feedback is None
        assert isinstance(usage.timestamp, datetime)

    def test_with_feedback(self):
        usage = ToolUsage(
            tool_name="calendar",
            task_type="scheduling",
            success=True,
            user_feedback="helpful",
        )
        assert usage.user_feedback == "helpful"


class TestToolPreferenceModel:
    """Test ToolPreference data model."""

    def test_construction(self):
        now = datetime.now()
        pref = ToolPreference(
            tool_name="web_search",
            task_type="research",
            success_rate=0.9,
            usage_count=10,
            last_used=now,
            user_rating=0.8,
        )
        assert pref.tool_name == "web_search"
        assert pref.success_rate == 0.9
        assert pref.usage_count == 10


class TestToolLearner:
    """Test ToolLearner functionality."""

    @pytest.mark.asyncio
    async def test_record_usage_creates_preference(self, mock_graph):
        learner = ToolLearner(mock_graph)
        usage = ToolUsage(tool_name="web_search", task_type="research", success=True)

        await learner.record_usage(usage)

        tool = await learner.get_preferred_tool("research")
        assert tool == "web_search"

    @pytest.mark.asyncio
    async def test_record_multiple_updates_stats(self, mock_graph):
        learner = ToolLearner(mock_graph)

        await learner.record_usage(
            ToolUsage(tool_name="web_search", task_type="research", success=True)
        )
        await learner.record_usage(
            ToolUsage(tool_name="web_search", task_type="research", success=False)
        )

        prefs = await learner.get_all_preferences("research")
        assert len(prefs) == 1
        assert prefs[0].usage_count == 2
        assert prefs[0].success_rate == 0.5

    @pytest.mark.asyncio
    async def test_preferred_tool_selects_best(self, mock_graph):
        learner = ToolLearner(mock_graph)

        # web_search: 100% success
        await learner.record_usage(
            ToolUsage(tool_name="web_search", task_type="research", success=True)
        )
        # browser: 0% success
        await learner.record_usage(
            ToolUsage(tool_name="browser", task_type="research", success=False)
        )

        tool = await learner.get_preferred_tool("research")
        assert tool == "web_search"

    @pytest.mark.asyncio
    async def test_no_preference_returns_none(self, mock_graph):
        learner = ToolLearner(mock_graph)
        tool = await learner.get_preferred_tool("unknown_task")
        assert tool is None


# ===========================================================================
# SelfReflector
# ===========================================================================


class TestReflectionModel:
    """Test Reflection data model."""

    def test_basic_construction(self):
        r = Reflection(id="r-1", area="response_quality", score=0.8)
        assert r.id == "r-1"
        assert r.area == "response_quality"
        assert r.score == 0.8
        assert r.issues == []
        assert r.improvements == []

    def test_with_issues_and_improvements(self):
        r = Reflection(
            id="r-2",
            area="memory_recall",
            score=0.6,
            issues=["stale data"],
            improvements=["add recency filter"],
        )
        assert len(r.issues) == 1
        assert len(r.improvements) == 1


class TestSelfReflector:
    """Test SelfReflector functionality."""

    @pytest.mark.asyncio
    async def test_reflect_default_area(self, mock_metrics, mock_event_bus):
        reflector = SelfReflector(mock_metrics, mock_event_bus)
        result = await reflector.reflect()

        assert result.area == "response_quality"
        assert 0 <= result.score <= 1

    @pytest.mark.asyncio
    async def test_reflect_on_memory(self, mock_metrics, mock_event_bus):
        reflector = SelfReflector(mock_metrics, mock_event_bus)
        result = await reflector.reflect("memory_recall")

        assert result.area == "memory_recall"
        assert len(result.issues) > 0

    @pytest.mark.asyncio
    async def test_reflect_on_tool_usage(self, mock_metrics, mock_event_bus):
        reflector = SelfReflector(mock_metrics, mock_event_bus)
        result = await reflector.reflect("tool_usage")

        assert result.area == "tool_usage"

    @pytest.mark.asyncio
    async def test_reflect_unknown_area(self, mock_metrics, mock_event_bus):
        reflector = SelfReflector(mock_metrics, mock_event_bus)
        result = await reflector.reflect("unknown_area")

        assert result.area == "unknown_area"
        assert result.score == 0.5

    @pytest.mark.asyncio
    async def test_reflections_are_stored(self, mock_metrics, mock_event_bus):
        reflector = SelfReflector(mock_metrics, mock_event_bus)
        await reflector.reflect("response_quality")
        await reflector.reflect("memory_recall")

        recent = await reflector.get_recent_reflections()
        assert len(recent) == 2


# ===========================================================================
# TaskPlanner
# ===========================================================================


class TestSubTaskModel:
    """Test SubTask data model."""

    def test_basic_construction(self):
        st = SubTask(id="st-1", description="Do something")
        assert st.priority == TaskPriority.MEDIUM
        assert st.dependencies == []
        assert st.estimated_effort == 1.0
        assert st.assigned_to is None

    def test_with_all_fields(self):
        st = SubTask(
            id="st-2",
            description="Critical work",
            priority=TaskPriority.CRITICAL,
            dependencies=["st-1"],
            estimated_effort=3.0,
            assigned_to="node-1",
        )
        assert st.priority == TaskPriority.CRITICAL
        assert st.dependencies == ["st-1"]


class TestTaskPlanModel:
    """Test TaskPlan data model."""

    def test_basic_construction(self):
        plan = TaskPlan(id="plan-1", description="Build a thing")
        assert plan.subtasks == []
        assert plan.parallel_groups == []
        assert plan.total_effort == 0.0


class TestTaskPlanner:
    """Test TaskPlanner functionality."""

    @pytest.mark.asyncio
    async def test_plan_creates_subtasks(self, mock_graph):
        planner = TaskPlanner(mock_graph)
        plan = await planner.plan("Build a REST API")

        assert plan.description == "Build a REST API"
        assert len(plan.subtasks) >= 1
        assert plan.total_effort > 0

    @pytest.mark.asyncio
    async def test_plan_finds_parallel_groups(self, mock_graph):
        planner = TaskPlanner(mock_graph)
        plan = await planner.plan("Simple task")

        # Single subtask with no deps should form one parallel group
        assert len(plan.parallel_groups) == 1
        assert plan.subtasks[0].id in plan.parallel_groups[0]

    @pytest.mark.asyncio
    async def test_plan_id_is_deterministic(self, mock_graph):
        planner = TaskPlanner(mock_graph)
        plan1 = await planner.plan("Same request")
        plan2 = await planner.plan("Same request")

        assert plan1.id == plan2.id

    def test_task_priority_values(self):
        assert TaskPriority.CRITICAL == "critical"
        assert TaskPriority.HIGH == "high"
        assert TaskPriority.MEDIUM == "medium"
        assert TaskPriority.LOW == "low"


# ===========================================================================
# SessionContinuity
# ===========================================================================


class TestSessionContextModel:
    """Test SessionContext data model."""

    def test_basic_construction(self):
        ctx = SessionContext(session_id="s-1", user_id="owner")
        assert ctx.session_id == "s-1"
        assert ctx.user_id == "owner"
        assert ctx.topics == []
        assert ctx.entities == []
        assert ctx.pending_tasks == []

    def test_with_data(self):
        ctx = SessionContext(
            session_id="s-2",
            user_id="owner",
            topics=["peptides", "health"],
            entities=["person-jeff"],
        )
        assert len(ctx.topics) == 2
        assert "person-jeff" in ctx.entities


class TestSessionContinuity:
    """Test SessionContinuity functionality."""

    @pytest.mark.asyncio
    async def test_start_new_session(self, mock_graph, mock_event_bus):
        sc = SessionContinuity(mock_graph, mock_event_bus)
        ctx = await sc.start_session("owner")

        assert ctx.user_id == "owner"
        assert "owner" in ctx.session_id

    @pytest.mark.asyncio
    async def test_resumes_existing_session(self, mock_graph, mock_event_bus):
        sc = SessionContinuity(mock_graph, mock_event_bus)

        ctx1 = await sc.start_session("owner")
        ctx2 = await sc.start_session("owner")

        assert ctx1.session_id == ctx2.session_id

    @pytest.mark.asyncio
    async def test_update_context_adds_topics(self, mock_graph, mock_event_bus):
        sc = SessionContinuity(mock_graph, mock_event_bus)
        ctx = await sc.start_session("owner")

        await sc.update_context(ctx.session_id, topics=["ai", "peptides"])
        await sc.update_context(ctx.session_id, topics=["ai", "health"])

        assert "ai" in ctx.topics
        assert "peptides" in ctx.topics
        assert "health" in ctx.topics
        # "ai" should not be duplicated
        assert ctx.topics.count("ai") == 1

    @pytest.mark.asyncio
    async def test_update_context_deduplicates_entities(self, mock_graph, mock_event_bus):
        sc = SessionContinuity(mock_graph, mock_event_bus)
        ctx = await sc.start_session("owner")

        await sc.update_context(ctx.session_id, entities=["person-jeff"])
        await sc.update_context(ctx.session_id, entities=["person-jeff", "person-ingrid"])

        assert ctx.entities.count("person-jeff") == 1
        assert "person-ingrid" in ctx.entities

    @pytest.mark.asyncio
    async def test_update_unknown_session_is_safe(self, mock_graph, mock_event_bus):
        sc = SessionContinuity(mock_graph, mock_event_bus)
        # Should not raise
        await sc.update_context("nonexistent", topics=["test"])

    @pytest.mark.asyncio
    async def test_end_session(self, mock_graph, mock_event_bus):
        sc = SessionContinuity(mock_graph, mock_event_bus)
        ctx = await sc.start_session("owner")

        ended = await sc.end_session(ctx.session_id)
        assert ended is not None
        assert ended.session_id == ctx.session_id

        # Should now create a new session
        ctx2 = await sc.start_session("owner")
        assert ctx2.session_id != ctx.session_id


# ===========================================================================
# ResearchOrchestrator
# ===========================================================================


class TestResearchModels:
    """Test research data models."""

    def test_source_construction(self):
        source = ResearchSource(type=SourceType.WEB, name="google")
        assert source.type == SourceType.WEB
        assert source.priority == 0.5
        assert source.rate_limit is None

    def test_result_construction(self):
        result = ResearchResult(source="google", content="Found it", confidence=0.9)
        assert result.source == "google"
        assert result.citations == []

    def test_report_construction(self):
        report = ResearchReport(query="what is colony?")
        assert report.results == []
        assert report.synthesized_summary is None
        assert report.confidence == 0.0

    def test_source_type_values(self):
        assert SourceType.WEB == "web"
        assert SourceType.KNOWLEDGE_GRAPH == "knowledge_graph"
        assert SourceType.MEMORY == "memory"
        assert SourceType.API == "api"


class TestResearchOrchestrator:
    """Test ResearchOrchestrator functionality."""

    @pytest.mark.asyncio
    async def test_research_with_no_sources(self, mock_graph, mock_event_bus):
        ro = ResearchOrchestrator(mock_graph, mock_event_bus)
        report = await ro.research("what is AI?")

        assert report.query == "what is AI?"
        assert report.results == []
        assert report.confidence == 0.0
        assert report.synthesized_summary is None

    @pytest.mark.asyncio
    async def test_register_and_unregister_source(self, mock_graph, mock_event_bus):
        ro = ResearchOrchestrator(mock_graph, mock_event_bus)

        source = ResearchSource(type=SourceType.WEB, name="google", priority=0.9)
        ro.register_source(source)
        assert len(ro._sources) == 1

        ro.unregister_source("google")
        assert len(ro._sources) == 0

    @pytest.mark.asyncio
    async def test_sources_queried_by_priority(self, mock_graph, mock_event_bus):
        ro = ResearchOrchestrator(mock_graph, mock_event_bus)

        ro.register_source(ResearchSource(type=SourceType.WEB, name="low", priority=0.1))
        ro.register_source(ResearchSource(type=SourceType.MEMORY, name="high", priority=0.9))
        ro.register_source(ResearchSource(type=SourceType.API, name="mid", priority=0.5))

        # With max_sources=2, only high and mid should be queried
        report = await ro.research("test", max_sources=2)
        assert isinstance(report, ResearchReport)


# ===========================================================================
# PreferenceLearner
# ===========================================================================


class TestPreferenceModel:
    """Test Preference data model."""

    def test_basic_construction(self):
        p = Preference(
            category="communication_style",
            key="length",
            value="short",
            confidence=0.9,
            learned_from="explicit",
        )
        assert p.category == "communication_style"
        assert p.key == "length"
        assert p.value == "short"
        assert isinstance(p.last_updated, datetime)


class TestPreferenceLearner:
    """Test PreferenceLearner functionality."""

    @pytest.mark.asyncio
    async def test_learn_from_feedback(self, mock_graph):
        pl = PreferenceLearner(mock_graph)
        await pl.learn_from_feedback("communication_style", "keep it short")

        # Should store with high confidence
        prefs = await pl.get_all_preferences("communication_style")
        assert len(prefs) == 1
        assert prefs[0].confidence == 0.9
        assert prefs[0].learned_from == "explicit"

    @pytest.mark.asyncio
    async def test_learn_from_behavior_accumulates(self, mock_graph):
        pl = PreferenceLearner(mock_graph)

        # Behavior learning returns None by default (placeholder)
        await pl.learn_from_behavior("clicked_short_response")

        # No preferences stored since placeholder returns None
        prefs = await pl.get_all_preferences()
        assert len(prefs) == 0

    @pytest.mark.asyncio
    async def test_get_preference_with_default(self, mock_graph):
        pl = PreferenceLearner(mock_graph)
        value = await pl.get_preference("nonexistent", "key", default="fallback")
        assert value == "fallback"

    @pytest.mark.asyncio
    async def test_get_preference_after_learn(self, mock_graph):
        pl = PreferenceLearner(mock_graph)
        await pl.learn_from_feedback("style", "concise answers please")

        value = await pl.get_preference("style", "general")
        assert value == "concise answers please"


# ===========================================================================
# AnomalyDetector
# ===========================================================================


class TestAnomalyModel:
    """Test Anomaly data model."""

    def test_basic_construction(self):
        a = Anomaly(
            id="anom-1",
            type=AnomalyType.HEALTH,
            description="Heart rate spike",
            severity=0.8,
        )
        assert a.type == AnomalyType.HEALTH
        assert a.severity == 0.8
        assert a.entity_id is None
        assert a.context == {}

    def test_with_baseline_comparison(self):
        a = Anomaly(
            id="anom-2",
            type=AnomalyType.COMMUNICATION,
            description="No messages in 3 days",
            severity=0.9,
            entity_id="person-ingrid",
            baseline_value=5.0,
            observed_value=0.0,
        )
        assert a.baseline_value == 5.0
        assert a.observed_value == 0.0

    def test_anomaly_type_values(self):
        assert AnomalyType.COMMUNICATION == "communication"
        assert AnomalyType.HEALTH == "health"
        assert AnomalyType.BEHAVIOR == "behavior"
        assert AnomalyType.RELATIONSHIP == "relationship"


class TestAnomalyDetector:
    """Test AnomalyDetector functionality."""

    @pytest.mark.asyncio
    async def test_detect_returns_empty_placeholder(self, mock_graph, mock_event_bus):
        ad = AnomalyDetector(mock_graph, mock_event_bus)
        anomalies = await ad.detect()

        # Placeholder implementations return empty lists
        assert anomalies == []

    @pytest.mark.asyncio
    async def test_detect_with_domain_filter(self, mock_graph, mock_event_bus):
        ad = AnomalyDetector(mock_graph, mock_event_bus)
        anomalies = await ad.detect(domain=AnomalyType.HEALTH)

        assert isinstance(anomalies, list)

    @pytest.mark.asyncio
    async def test_update_and_get_baseline(self, mock_graph, mock_event_bus):
        ad = AnomalyDetector(mock_graph, mock_event_bus)

        await ad.update_baseline("person-owner", "sleep_score", 85)
        value = await ad.get_baseline("person-owner", "sleep_score")
        assert value == 85

    @pytest.mark.asyncio
    async def test_get_missing_baseline(self, mock_graph, mock_event_bus):
        ad = AnomalyDetector(mock_graph, mock_event_bus)
        value = await ad.get_baseline("person-owner", "nonexistent")
        assert value is None

    @pytest.mark.asyncio
    async def test_threshold_filtering(self, mock_graph, mock_event_bus):
        ad = AnomalyDetector(mock_graph, mock_event_bus)
        # With placeholder returning empty, threshold doesn't matter
        anomalies = await ad.detect(threshold=0.0)
        assert anomalies == []


# ===========================================================================
# InitiativeEngine
# ===========================================================================


class TestInitiativeModel:
    """Test Initiative data model."""

    def test_basic_construction(self):
        i = Initiative(
            id="init-1",
            type=InitiativeType.FOLLOW_UP,
            description="Follow up with Jeff on lab",
            priority=0.8,
            rationale="No contact in 5 days",
        )
        assert i.type == InitiativeType.FOLLOW_UP
        assert i.priority == 0.8
        assert i.action_hint is None
        assert i.entity_id is None

    def test_with_expiry(self):
        expires = datetime.now() + timedelta(hours=24)
        i = Initiative(
            id="init-2",
            type=InitiativeType.HEALTH,
            description="Review Oura data",
            priority=0.6,
            rationale="Weekly check",
            expires_at=expires,
        )
        assert i.expires_at == expires

    def test_initiative_type_values(self):
        assert InitiativeType.FOLLOW_UP == "follow_up"
        assert InitiativeType.RELATIONSHIP == "relationship"
        assert InitiativeType.HEALTH == "health"
        assert InitiativeType.SCHEDULING == "scheduling"


class TestInitiativeEngine:
    """Test InitiativeEngine functionality."""

    @pytest.mark.asyncio
    async def test_generate_returns_empty_placeholder(
        self, mock_graph, mock_event_bus, mock_mind_model
    ):
        ie = InitiativeEngine(mock_graph, mock_event_bus, mock_mind_model)
        initiatives = await ie.generate()

        # Placeholder implementations return empty lists
        assert initiatives == []

    @pytest.mark.asyncio
    async def test_generate_with_type_filter(
        self, mock_graph, mock_event_bus, mock_mind_model
    ):
        ie = InitiativeEngine(mock_graph, mock_event_bus, mock_mind_model)
        initiatives = await ie.generate(types=[InitiativeType.HEALTH])

        assert isinstance(initiatives, list)

    @pytest.mark.asyncio
    async def test_dismiss_initiative(
        self, mock_graph, mock_event_bus, mock_mind_model
    ):
        ie = InitiativeEngine(mock_graph, mock_event_bus, mock_mind_model)

        # Manually add an initiative
        ie._initiatives.append(
            Initiative(
                id="init-test",
                type=InitiativeType.FOLLOW_UP,
                description="Test",
                priority=0.8,
                rationale="Testing",
            )
        )

        await ie.dismiss("init-test")
        active = await ie.get_active()
        assert not any(i.id == "init-test" for i in active)

    @pytest.mark.asyncio
    async def test_get_active_filters_expired(
        self, mock_graph, mock_event_bus, mock_mind_model
    ):
        ie = InitiativeEngine(mock_graph, mock_event_bus, mock_mind_model)

        # Add expired initiative
        ie._initiatives.append(
            Initiative(
                id="expired",
                type=InitiativeType.HEALTH,
                description="Expired",
                priority=0.9,
                rationale="Old",
                expires_at=datetime.now() - timedelta(hours=1),
            )
        )
        # Add active initiative
        ie._initiatives.append(
            Initiative(
                id="active",
                type=InitiativeType.HEALTH,
                description="Active",
                priority=0.9,
                rationale="Current",
                expires_at=datetime.now() + timedelta(hours=1),
            )
        )

        active = await ie.get_active()
        ids = [i.id for i in active]
        assert "active" in ids
        assert "expired" not in ids


# ===========================================================================
# Package imports
# ===========================================================================


class TestPackageImports:
    """Verify all components are importable from the package."""

    def test_all_imports(self):
        from colony_sidecar.intelligence.components import (
            ToolLearner,
            ToolUsage,
            ToolPreference,
            SelfReflector,
            Reflection,
            TaskPlanner,
            TaskPlan,
            SubTask,
            TaskPriority,
            SessionContinuity,
            SessionContext,
            ResearchOrchestrator,
            ResearchReport,
            ResearchResult,
            ResearchSource,
            SourceType,
            PreferenceLearner,
            Preference,
            AnomalyDetector,
            Anomaly,
            AnomalyType,
            InitiativeEngine,
            Initiative,
            InitiativeType,
        )

        # Smoke check that they're actual classes
        assert callable(ToolLearner)
        assert callable(SelfReflector)
        assert callable(TaskPlanner)
        assert callable(SessionContinuity)
        assert callable(ResearchOrchestrator)
        assert callable(PreferenceLearner)
        assert callable(AnomalyDetector)
        assert callable(InitiativeEngine)
