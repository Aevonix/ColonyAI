"""Agent-as-sensor loop (v0.16.0).

Colony never calls external APIs: the agent observes through its own
Hermes connections and reports to the observation store; Colony's
generators read observations; the autonomy loop requests syncs when a
domain goes stale; volatile initiatives auto-close when a refresh shows
the condition cleared.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from colony_sidecar.initiatives.action_registry import (
    OBSERVATION_SYNC_ACTIONS,
    RiskTier,
    get_action,
)
from colony_sidecar.initiatives.store import InitiativeStore
from colony_sidecar.intelligence.components.initiative_engine import (
    InitiativeConfig,
    InitiativeEngine,
    InitiativeType,
)
from colony_sidecar.observations.store import (
    OBSERVATION_DOMAINS,
    OBSERVATION_SYNC_INTERVALS,
    ObservationStore,
)


@pytest.fixture
def obs_store(tmp_path: Path) -> ObservationStore:
    return ObservationStore(state_dir=tmp_path)


@pytest.fixture
def engine(obs_store):
    return InitiativeEngine(
        graph_client=None,
        event_bus=None,
        mind_model=None,
        config=InitiativeConfig(),
        observation_store=obs_store,
    )


# ---------------------------------------------------------------------------
# Observation store
# ---------------------------------------------------------------------------

class TestObservationStore:
    def test_record_and_get_round_trip(self, obs_store):
        obs_store.record("coding", "repo#1", {"title": "Fix bug", "ci_status": "failing"})
        obs = obs_store.get("coding", "repo#1")
        assert obs.payload["ci_status"] == "failing"
        assert obs.domain == "coding"

    def test_upsert_latest_snapshot_wins(self, obs_store):
        obs_store.record("coding", "repo#1", {"ci_status": "failing"})
        obs_store.record("coding", "repo#1", {"ci_status": "passing"})
        assert obs_store.get("coding", "repo#1").payload["ci_status"] == "passing"
        assert len(obs_store.list("coding")) == 1

    def test_batch_ingest_skips_missing_entity_id(self, obs_store):
        written = obs_store.record_batch(
            "task",
            [
                {"entity_id": "t1", "payload": {"title": "a"}},
                {"payload": {"title": "no id"}},
                {"entity_id": "t2", "payload": {"title": "b"}},
            ],
            reported_by="aeva",
        )
        assert written == 2
        assert obs_store.get("task", "t1").reported_by == "aeva"

    def test_domain_age(self, obs_store):
        assert obs_store.domain_age_seconds("system") is None  # never observed
        obs_store.record("system", "svc-1", {"status": "healthy"})
        age = obs_store.domain_age_seconds("system")
        assert age is not None and age < 5

    def test_summary_and_prune(self, obs_store):
        old = datetime.now(timezone.utc) - timedelta(days=60)
        obs_store.record("research", "paper-1", {"title": "x"}, observed_at=old)
        obs_store.record("research", "paper-2", {"title": "y"})
        assert obs_store.summary()["research"]["count"] == 2
        assert obs_store.prune(older_than_days=30) == 1
        assert obs_store.get("research", "paper-1") is None


# ---------------------------------------------------------------------------
# Ingestion API
# ---------------------------------------------------------------------------

class TestObservationAPI:
    @pytest.fixture
    def client(self, obs_store):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from colony_sidecar.api.routers import observations as obs_router

        app = FastAPI()
        app.include_router(obs_router.router)
        obs_router.set_observation_store(obs_store)
        yield TestClient(app)
        obs_router.set_observation_store(None)

    def test_ingest_and_list(self, client):
        resp = client.post(
            "/v1/host/observations",
            json={
                "domain": "coding",
                "reported_by": "aeva",
                "observations": [
                    {"entity_id": "repo#7", "payload": {"title": "PR 7", "ci_status": "failing"}},
                ],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["written"] == 1

        listed = client.get("/v1/host/observations/coding").json()
        assert listed["total"] == 1
        assert listed["observations"][0]["payload"]["ci_status"] == "failing"

        summary = client.get("/v1/host/observations").json()
        assert summary["domains"]["coding"]["count"] == 1

    def test_unknown_domain_rejected(self, client):
        resp = client.post(
            "/v1/host/observations",
            json={"domain": "weather", "observations": []},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Domain generators read observations
# ---------------------------------------------------------------------------

class TestDomainGenerators:
    @pytest.mark.asyncio
    async def test_coding_failing_ci_and_review(self, engine, obs_store):
        obs_store.record("coding", "repo#1", {"title": "Fix auth", "ci_status": "failing"})
        obs_store.record("coding", "repo#2", {"title": "Add docs", "review_requested": True})
        obs_store.record("coding", "repo#3", {"title": "WIP", "review_requested": True, "draft": True})
        obs_store.record("coding", "repo#4", {"title": "Green", "ci_status": "passing"})
        engine._load_observation_domains()
        initiatives = await engine._generate_coding_initiatives()
        by_entity = {i.entity_id: i for i in initiatives}
        assert set(by_entity) == {"repo#1", "repo#2"}
        assert "failing CI" in by_entity["repo#1"].description
        assert by_entity["repo#1"].dedup_key == "coding:repo#1"
        assert by_entity["repo#1"].type == InitiativeType.CODING

    @pytest.mark.asyncio
    async def test_calendar_prep_window(self, engine, obs_store):
        soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        far = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        obs_store.record("calendar", "evt-1", {"title": "Standup", "start_time": soon})
        obs_store.record("calendar", "evt-2", {"title": "Next week", "start_time": far})
        obs_store.record("calendar", "evt-3", {"title": "Done", "start_time": past})
        engine._load_observation_domains()
        initiatives = await engine._generate_calendar_initiatives()
        assert [i.entity_id for i in initiatives] == ["evt-1"]
        assert initiatives[0].description == "Prepare for: Standup"

    @pytest.mark.asyncio
    async def test_system_unhealthy_only(self, engine, obs_store):
        obs_store.record("system", "spark1", {"status": "degraded", "error_rate": 0.3})
        obs_store.record("system", "spark2", {"status": "healthy", "error_rate": 0.0})
        engine._load_observation_domains()
        initiatives = await engine._generate_system_initiatives()
        assert [i.entity_id for i in initiatives] == ["spark1"]
        assert initiatives[0].priority == 0.9

    @pytest.mark.asyncio
    async def test_task_stale_follow_up(self, engine, obs_store):
        obs_store.record("task", "iss-1", {"title": "Old task", "state": "open", "stale_days": 10})
        obs_store.record("task", "iss-2", {"title": "Fresh", "state": "open", "stale_days": 0})
        obs_store.record("task", "iss-3", {"title": "Closed", "state": "closed", "stale_days": 30})
        engine._load_observation_domains()
        initiatives = await engine._generate_task_initiatives()
        assert [i.entity_id for i in initiatives] == ["iss-1"]

    @pytest.mark.asyncio
    async def test_project_milestone_approaching(self, engine, obs_store):
        due_soon = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        obs_store.record("project", "m1", {"title": "v1.0", "due_on": due_soon, "open_issues": 4})
        obs_store.record("project", "m2", {"title": "done", "due_on": due_soon, "open_issues": 0})
        engine._load_observation_domains()
        initiatives = await engine._generate_project_initiatives()
        assert [i.entity_id for i in initiatives] == ["m1"]

    @pytest.mark.asyncio
    async def test_research_pending_check(self, engine, obs_store):
        old_check = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        obs_store.record("research", "p1", {"title": "Paper", "status": "watching", "last_checked": old_check})
        obs_store.record("research", "p2", {"title": "Shipped", "status": "published"})
        engine._load_observation_domains()
        initiatives = await engine._generate_research_initiatives()
        assert [i.entity_id for i in initiatives] == ["p1"]

    @pytest.mark.asyncio
    async def test_manually_fed_context_not_clobbered(self, engine, obs_store):
        obs_store.record("system", "svc", {"status": "degraded"})
        engine.add_context("system", [{"entity_id": "manual", "status": "down"}])
        engine._load_observation_domains()
        assert [i["entity_id"] for i in engine._context["system"]] == ["manual"]


# ---------------------------------------------------------------------------
# Per-entity rebuild + condition-cleared auto-close
# ---------------------------------------------------------------------------

class TestObservationRebuild:
    @pytest.mark.asyncio
    async def test_rebuild_returns_freshest_snapshot(self, engine, obs_store):
        obs_store.record("coding", "repo#1", {"ci_status": "failing"})
        ctx = await engine.rebuild_context("coding", "repo#1")
        assert ctx["coding"]["ci_status"] == "failing"
        assert ctx["condition_cleared"] is False
        assert "context_captured_at" in ctx

    @pytest.mark.asyncio
    async def test_condition_cleared_when_ci_green(self, engine, obs_store):
        obs_store.record("coding", "repo#1", {"ci_status": "passing", "review_requested": False})
        ctx = await engine.rebuild_context("coding", "repo#1")
        assert ctx["condition_cleared"] is True

    @pytest.mark.asyncio
    async def test_condition_cleared_per_domain(self, engine, obs_store):
        obs_store.record("system", "svc", {"status": "healthy", "error_rate": 0.0})
        assert (await engine.rebuild_context("system", "svc"))["condition_cleared"] is True
        obs_store.record("task", "t", {"state": "closed"})
        assert (await engine.rebuild_context("task", "t"))["condition_cleared"] is True
        obs_store.record("project", "m", {"open_issues": 0})
        assert (await engine.rebuild_context("project", "m"))["condition_cleared"] is True

    @pytest.mark.asyncio
    async def test_refresh_endpoint_auto_closes(self, engine, obs_store, tmp_path):
        from colony_sidecar.api.routers import host as host_mod

        init_store = InitiativeStore(state_dir=tmp_path / "init")
        created = init_store.create(
            type="coding",
            description="Investigate failing CI on Fix auth",
            priority=0.85,
            entity_id="repo#1",
            dedup_key="coding:repo#1",
            context={"ci_status": "failing"},
        )
        # Condition has cleared since the initiative was generated
        obs_store.record("coding", "repo#1", {"ci_status": "passing", "review_requested": False})

        prev_store, prev_loop = host_mod._initiative_store, host_mod._autonomy_loop
        host_mod.set_initiative_store(init_store)
        host_mod.set_autonomy_loop(
            SimpleNamespace(_registry=SimpleNamespace(initiative_engine=engine))
        )
        try:
            resp = await host_mod.refresh_initiative_context(created.id)
        finally:
            host_mod.set_initiative_store(prev_store)
            host_mod.set_autonomy_loop(prev_loop)

        assert resp.status == "cancelled"
        closed = init_store.get(created.id)
        assert closed.stale_reason == "condition_cleared"
        assert closed.context["coding"]["ci_status"] == "passing"


# ---------------------------------------------------------------------------
# Autonomy loop requests syncs for stale domains
# ---------------------------------------------------------------------------

class TestObservationSyncPhase:
    def _loop(self, obs_store):
        from colony_sidecar.api.routers import observations as obs_router
        from colony_sidecar.autonomy.config import AutonomyConfig
        from colony_sidecar.autonomy.loop import AutonomyLoop

        obs_router.set_observation_store(obs_store)
        registry = MagicMock()
        registry.task_queue = MagicMock()
        registry.task_queue.submit = AsyncMock(return_value={"id": "job-1"})
        return AutonomyLoop(registry=registry, config=AutonomyConfig()), registry

    @pytest.mark.asyncio
    async def test_never_observed_domains_get_sync_jobs(self, obs_store):
        from colony_sidecar.api.routers import observations as obs_router

        loop, registry = self._loop(obs_store)
        try:
            await loop._phase_observation_sync()
            hints = {
                call.kwargs["params"]["action_hint"]
                for call in registry.task_queue.submit.call_args_list
            }
            assert hints == set(OBSERVATION_SYNC_ACTIONS.values())
        finally:
            obs_router.set_observation_store(None)

    @pytest.mark.asyncio
    async def test_fresh_domain_not_synced_and_no_respam(self, obs_store):
        from colony_sidecar.api.routers import observations as obs_router

        for domain in OBSERVATION_DOMAINS:
            obs_store.record(domain, "e1", {"status": "healthy"})
        loop, registry = self._loop(obs_store)
        try:
            await loop._phase_observation_sync()
            assert registry.task_queue.submit.call_count == 0

            # Make one domain stale → exactly one sync request
            stale = datetime.now(timezone.utc) - timedelta(
                seconds=OBSERVATION_SYNC_INTERVALS["system"] + 60
            )
            obs_store.record("system", "e1", {"status": "healthy"}, observed_at=stale)
            await loop._phase_observation_sync()
            assert registry.task_queue.submit.call_count == 1

            # Same tick again: gated by _last_sync_request, no spam
            await loop._phase_observation_sync()
            assert registry.task_queue.submit.call_count == 1
        finally:
            obs_router.set_observation_store(None)

    @pytest.mark.asyncio
    async def test_sync_domains_env_filter(self, obs_store, monkeypatch):
        from colony_sidecar.api.routers import observations as obs_router

        monkeypatch.setenv("COLONY_SYNC_DOMAINS", "system")
        loop, registry = self._loop(obs_store)
        try:
            await loop._phase_observation_sync()
            assert registry.task_queue.submit.call_count == 1
            hint = registry.task_queue.submit.call_args.kwargs["params"]["action_hint"]
            assert hint == "agent_sync_system"
        finally:
            obs_router.set_observation_store(None)


# ---------------------------------------------------------------------------
# Registry + framing
# ---------------------------------------------------------------------------

class TestSensorRegistration:
    def test_all_domains_have_read_only_sync_actions(self):
        assert set(OBSERVATION_SYNC_ACTIONS) == set(OBSERVATION_DOMAINS)
        for action_name in OBSERVATION_SYNC_ACTIONS.values():
            spec = get_action(action_name)
            assert spec is not None, action_name
            assert spec.risk == RiskTier.READ_ONLY, action_name

    def test_no_agent_name_hardcoded_in_sidecar(self):
        # Colony is a public project: every deployment names its own
        # agent. Identity comes from COLONY_AGENT_NAME /
        # COLONY_WORKER_NODE_ID, never from code or defaults.
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "colony_sidecar"
        offenders = [
            str(path)
            for path in src.rglob("*.py")
            if "aeva" in path.read_text(errors="ignore").lower()
        ]
        assert offenders == []

    def test_no_notification_relay_defaults_remain(self):
        # The agent decides dispositions; Colony must not default to
        # "notify the owner" framing anywhere in the pipeline.
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "colony_sidecar"
        offenders = []
        for path in src.rglob("*.py"):
            text = path.read_text(errors="ignore")
            if '"notify_user"' in text or "'notify_user'" in text:
                offenders.append(str(path))
        assert offenders == []
