"""Colony sidecar FastAPI server.

Intelligence sidecar server mounted by (OpenClaw, future shims) mount
as a plugin via the ``/v1/host`` API surface.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from colony_sidecar.api.routers.host import (
    router as host_router,
    set_llm_router,
    set_autonomy_loop,
    set_chain_manager,
    set_reasoning_loop,
    set_graph,
    set_response_gate,
    set_signal_collector,
    set_embedder,
    set_goals_engine,
    set_contacts_store,
    set_briefings_engine,
    set_world_store,
    set_metalearner,
    set_research_pipeline,
    set_delivery_bridge,
    set_connection_discoverer,
    set_learner,
    set_skills_registry,
    set_secrets_manager,
    supported_capabilities,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize subsystems on startup, tear down on shutdown."""
    state_dir = os.environ.get("COLONY_STATE_DIR", ".")

    # --- 1. LLM Router ---
    llm_router = None
    try:
        from colony_sidecar.router.router import LLMRouter
        from colony_sidecar.router.tiers import build_tiers_from_host
        import json as _json
        from pathlib import Path as _Path

        config_path = _Path(state_dir) / ".colony-llm-config.json"
        if config_path.exists():
            try:
                host_llm_config = _json.loads(config_path.read_text())
                tiers = build_tiers_from_host(host_llm_config)
                llm_router = LLMRouter(tiers=tiers)
                logger.info(
                    "LLMRouter initialized from persisted host config (provider=%s)",
                    host_llm_config.get("provider", "unknown"),
                )
            except Exception as cfg_exc:
                logger.warning("Failed to load persisted LLM config, using defaults: %s", cfg_exc)
                llm_router = LLMRouter()
                logger.info("LLMRouter initialized with default tiers")
        else:
            llm_router = LLMRouter()
            logger.info("LLMRouter initialized with default tiers (no host config yet)")
    except Exception as exc:
        logger.warning("LLMRouter init failed — reasoning will not be available: %s", exc)

    # Register LLM router with the host router for /configure endpoint
    if llm_router is not None:
        set_llm_router(llm_router)

    # --- 2. Reasoning loop ---
    if llm_router is not None:
        try:
            from colony_sidecar.reasoning import ReasoningLoop, ToolExecutor
            reasoning_loop = ReasoningLoop(model=llm_router, tools=ToolExecutor())
            set_reasoning_loop(reasoning_loop)
            logger.info("ReasoningLoop initialized (max_iterations=%d)", reasoning_loop._config.max_iterations)
        except Exception as exc:
            logger.warning("ReasoningLoop init failed: %s", exc)

    # --- 3. Neo4j Graph memory ---
    graph = None
    try:
        from colony_sidecar.intelligence.graph.client import ColonyGraph, GraphConfig
        from pydantic import SecretStr
        neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
        neo4j_pass = os.environ.get("NEO4J_PASSWORD", "")
        graph_config = GraphConfig(
            uri=neo4j_uri,
            auth=(neo4j_user, SecretStr(neo4j_pass)) if neo4j_pass else None,
        )
        graph = ColonyGraph(graph_config)
        set_graph(graph)
        logger.info("ColonyGraph initialized (uri=%s)", neo4j_uri)
    except Exception as exc:
        logger.warning("ColonyGraph init failed — memory endpoints will be degraded: %s", exc)

    # --- 4. Response Gate (safety pipeline) ---
    try:
        from colony_sidecar.gate import ResponseGate, GateConfig
        from colony_sidecar.gate.audit import InMemoryAuditLog
        gate_config = GateConfig(send_delay_seconds=0.0)
        gate_audit = InMemoryAuditLog()
        gate = ResponseGate(gate_config, session_store=None, audit_log=gate_audit)
        set_response_gate(gate)
        logger.info("ResponseGate initialized (sensitivity=%s)", gate_config.sensitivity)
    except Exception as exc:
        logger.warning("ResponseGate init failed — safety checks will pass-through: %s", exc)

    # --- 5. Signal Collector ---
    try:
        from colony_sidecar.intelligence.mind_model.signal_collector import SignalCollector
        # BaselineStore is a Protocol — needs a concrete implementation from the host.
        # Skip for now; can be wired later via /v1/host/configure.
        logger.info("SignalCollector skipped — requires host-provided BaselineStore")
    except Exception as exc:
        logger.warning("SignalCollector init failed: %s", exc)

    # --- 6. Embedding pipeline ---
    try:
        from colony_sidecar.vector.embedder import EmbeddingPipeline
        from colony_sidecar.vector.config import EmbeddingConfig
        embed_config = EmbeddingConfig(
            provider=os.environ.get("COLONY_EMBED_PROVIDER", "openai"),
            model_id=os.environ.get("COLONY_EMBED_MODEL", "text-embedding-3-small"),
            dimensions=int(os.environ.get("COLONY_EMBED_DIMS", "1536")),
        )
        pipeline = EmbeddingPipeline(embed_config)
        set_embedder(pipeline)
        logger.info("EmbeddingPipeline initialized (model=%s)", embed_config.model_id)
    except Exception as exc:
        logger.warning("EmbeddingPipeline init failed — memory/embed returns 501: %s", exc)

    # --- 7. Goals engine ---
    try:
        from colony_sidecar.goals.engine import GoalEngine
        goals_engine = GoalEngine()
        set_goals_engine(goals_engine)
        logger.info("GoalEngine initialized")
    except Exception as exc:
        logger.warning("GoalEngine init failed: %s", exc)

    # --- 8. Contacts ---
    try:
        from colony_sidecar.contacts.store import SQLiteContactStore
        contacts_store = SQLiteContactStore()
        set_contacts_store(contacts_store)
        logger.info("ContactsStore initialized")
    except Exception as exc:
        logger.warning("ContactsStore init failed: %s", exc)

    # --- 9. Briefings ---
    try:
        from colony_sidecar.briefings.engine import BriefingEngine
        briefings = BriefingEngine()
        set_briefings_engine(briefings)
        logger.info("BriefingEngine initialized")
    except Exception as exc:
        logger.warning("BriefingEngine init failed: %s", exc)

    # --- 10. World model ---
    try:
        from colony_sidecar.world_model.store import WorldModelStore
        world_store = WorldModelStore()
        set_world_store(world_store)
        logger.info("WorldModelStore initialized")
    except Exception as exc:
        logger.warning("WorldModelStore init failed: %s", exc)

    # --- 11. Cognition (MetaLearner) ---
    try:
        from colony_sidecar.intelligence.cognition.metalearner import MetaLearner
        if graph is not None:
            metalearner = MetaLearner(graph=graph)
            set_metalearner(metalearner)
            logger.info("MetaLearner initialized")
        else:
            logger.warning("MetaLearner skipped — ColonyGraph not available")
    except Exception as exc:
        logger.warning("MetaLearner init failed: %s", exc)

    # --- 12. Research pipeline ---
    try:
        from colony_sidecar.research.pipeline import ResearchPipeline
        research = ResearchPipeline()
        set_research_pipeline(research)
        logger.info("ResearchPipeline initialized")
    except Exception as exc:
        logger.warning("ResearchPipeline init failed: %s", exc)

    # --- 13. Delivery bridge ---
    try:
        from colony_sidecar.delivery.bridge import ProactiveDeliveryBridge
        delivery = ProactiveDeliveryBridge()
        set_delivery_bridge(delivery)
        logger.info("ProactiveDeliveryBridge initialized")
    except Exception as exc:
        logger.warning("ProactiveDeliveryBridge init failed: %s", exc)

    # --- 14. Synthesis (ConnectionDiscoverer) ---
    try:
        from colony_sidecar.intelligence.synthesis.connection_discoverer import ConnectionDiscoverer
        if graph is not None:
            discoverer = ConnectionDiscoverer(graph_client=graph)
            set_connection_discoverer(discoverer)
            logger.info("ConnectionDiscoverer initialized")
        else:
            logger.warning("ConnectionDiscoverer skipped — ColonyGraph not available")
    except Exception as exc:
        logger.warning("ConnectionDiscoverer init failed: %s", exc)

    # --- 15. Continuous learner ---
    try:
        from colony_sidecar.intelligence.learning.continuous_learner import ContinuousLearner
        learner = ContinuousLearner()
        set_learner(learner)
        logger.info("ContinuousLearner initialized")
    except Exception as exc:
        logger.warning("ContinuousLearner init failed: %s", exc)

    # --- 16. Skills registry ---
    try:
        from colony_sidecar.skills.registry import SkillRegistry
        from pathlib import Path
        skills = SkillRegistry(db_path=Path(state_dir) / "skills.db")
        set_skills_registry(skills)
        logger.info("SkillRegistry initialized")
    except Exception as exc:
        logger.warning("SkillRegistry init failed: %s", exc)

    # --- 17. Chain / Identity ---
    try:
        from colony_sidecar.chain.manager import ChainManager
        from pathlib import Path
        chain = ChainManager(
            db_path=Path(state_dir) / "chain.db",
            colony_id=os.environ.get("COLONY_ID", "colony-default"),
        )
        set_chain_manager(chain)
        logger.info("ChainManager initialized")
    except Exception as exc:
        logger.warning("ChainManager init failed: %s", exc)

    # --- 18. Secrets ---
    try:
        from colony_sidecar.secrets.manager import SecretsManager
        secrets = SecretsManager()
        set_secrets_manager(secrets)
        logger.info("SecretsManager initialized")
    except Exception as exc:
        logger.warning("SecretsManager init failed: %s", exc)

    # --- 19. Autonomy loop ---
    try:
        from colony_sidecar.autonomy.loop import AutonomyLoop
        from colony_sidecar.autonomy.config import AutonomyConfig
        from colony_sidecar.autonomy.registry import SubsystemRegistry
        autonomy_config = AutonomyConfig.from_env()
        registry = SubsystemRegistry()
        autonomy_loop = AutonomyLoop(registry=registry, config=autonomy_config)
        set_autonomy_loop(autonomy_loop)
        logger.info(
            "AutonomyLoop initialized (tick=%ds, not started — use /v1/host/autonomy/start)",
            autonomy_config.tick_interval_secs,
        )
    except Exception as exc:
        logger.warning("AutonomyLoop init failed: %s", exc)

    logger.info("Sidecar capabilities: %s", supported_capabilities())
    yield

    # Shutdown — close connections
    if graph is not None:
        try:
            await graph.close()
        except Exception:
            pass
    set_llm_router(None)
    set_reasoning_loop(None)
    set_graph(None)
    set_response_gate(None)
    set_signal_collector(None)
    set_embedder(None)
    set_goals_engine(None)
    set_contacts_store(None)
    set_briefings_engine(None)
    set_world_store(None)
    set_metalearner(None)
    set_research_pipeline(None)
    set_delivery_bridge(None)
    set_connection_discoverer(None)
    set_learner(None)
    set_skills_registry(None)
    set_chain_manager(None)
    set_secrets_manager(None)
    # Stop autonomy loop if running
    try:
        from colony_sidecar.api.routers.host import _autonomy_loop
        if _autonomy_loop is not None and _autonomy_loop.is_running:
            await _autonomy_loop.stop()
    except Exception:
        pass
    set_autonomy_loop(None)
    logger.info("Sidecar shutdown complete")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="Colony Intelligence Sidecar",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(host_router)
    return app


# Uvicorn entry point
app = create_app()
