"""Colony sidecar FastAPI server.

Intelligence sidecar server mounted by agent frameworks (OpenClaw, Hermes,
etc.) as a plugin via the ``/v1/host`` API surface.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from colony_sidecar.api.routers.host import (
    router as host_router,
    set_llm_router,
    set_autonomy_loop,
    set_scheduler,
    set_chain_manager,
    set_reasoning_loop,
    set_tool_executor,
    set_graph,
    set_consolidator,
    set_response_gate,
    set_signal_collector,
    set_embedder,
    set_goals_engine,
    set_contacts_store,
    set_briefings_engine,
    set_world_store,
    set_extraction_pipeline,
    set_metalearner,
    set_research_pipeline,
    set_search_orchestrator,
    set_delivery_bridge,
    set_connection_discoverer,
    set_learner,
    set_skills_registry,
    set_secrets_manager,
    set_session_store,
    set_task_queue,
    supported_capabilities,
)

logger = logging.getLogger(__name__)


def _state_dir() -> Path:
    """Resolve the Colony state directory."""
    return Path(os.environ.get("COLONY_STATE_DIR", ".")).resolve()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize subsystems on startup, tear down on shutdown."""
    state_dir = _state_dir()

    # --- 1. LLM Router ---
    llm_router = None
    try:
        from colony_sidecar.router.router import LLMRouter
        from colony_sidecar.router.tiers import build_tiers_from_host
        import json as _json

        config_path = state_dir / ".colony-llm-config.json"
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

    if llm_router is not None:
        set_llm_router(llm_router)

    # --- 2. Reasoning loop ---
    if llm_router is not None:
        try:
            from colony_sidecar.reasoning import ReasoningLoop, ToolExecutor
            tool_executor = ToolExecutor()
            reasoning_loop = ReasoningLoop(model=llm_router, tools=tool_executor)
            set_reasoning_loop(reasoning_loop)
            # Native tools will be registered after search orchestrator is wired
            set_tool_executor(tool_executor)
            logger.info("ReasoningLoop initialized")
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
        # Neo4j Community Edition only has the "neo4j" database.
        # Enterprise users can override via NEO4J_DATABASE.
        neo4j_db = os.environ.get("NEO4J_DATABASE", "neo4j")
        graph_config = GraphConfig(
            uri=neo4j_uri,
            auth=(neo4j_user, SecretStr(neo4j_pass)) if neo4j_pass else None,
            database=neo4j_db,
        )
        graph = ColonyGraph(graph_config)
        set_graph(graph)
        logger.info("ColonyGraph initialized (uri=%s db=%s)", neo4j_uri, neo4j_db)

        # Wire consolidator
        try:
            from colony_sidecar.intelligence.graph.consolidator import MemoryConsolidator
            consolidator = MemoryConsolidator(graph)
            set_consolidator(consolidator)
            logger.info("MemoryConsolidator initialized")
        except Exception as cexc:
            logger.warning("MemoryConsolidator init skipped: %s", cexc)
    except Exception as exc:
        logger.warning("ColonyGraph init failed — memory endpoints will be degraded: %s", exc)

    # --- 4. Response Gate (safety pipeline) ---
    _gate_ref = None
    _gate_config = None
    _gate_audit = None
    try:
        from colony_sidecar.gate import ResponseGate, GateConfig
        from colony_sidecar.gate.audit import InMemoryAuditLog
        gate_config = GateConfig(send_delay_seconds=0.0)
        gate_audit = InMemoryAuditLog()
        gate = ResponseGate(gate_config, session_store=None, audit_log=gate_audit)
        set_response_gate(gate)
        # Stash refs for re-wiring after session store is available
        _gate_ref = gate
        _gate_config = gate_config
        _gate_audit = gate_audit
        logger.info("ResponseGate initialized")

        # Re-wire ResponseGate with session store once available
    except Exception as exc:
        logger.warning("ResponseGate init failed — safety checks will pass-through: %s", exc)

    # --- 5. Signal Collector ---
    signal_collector = None
    if graph is not None:
        try:
            from colony_sidecar.intelligence.mind_model.graph_baseline import GraphBaselineStore
            from colony_sidecar.intelligence.mind_model.signal_collector import SignalCollector
            baseline_store = GraphBaselineStore(graph)
            signal_collector = SignalCollector(baseline_store=baseline_store, graph=graph)
            set_signal_collector(signal_collector)
            logger.info("SignalCollector initialized (GraphBaselineStore backed by Neo4j)")
        except Exception as exc:
            logger.warning("SignalCollector init failed: %s", exc)
    else:
        logger.warning("SignalCollector skipped — ColonyGraph not available")

    # --- 6. Embedding pipeline ---
    embed_provider = os.environ.get("COLONY_EMBED_PROVIDER", "")
    embed_model = os.environ.get("COLONY_EMBED_MODEL", "")
    embed_dims = os.environ.get("COLONY_EMBED_DIMS", "")
    reranker_model = os.environ.get("COLONY_RERANKER_MODEL", "")

    # Auto-detect tier if not explicitly configured
    if not embed_provider or not embed_model:
        try:
            from colony_sidecar.vector.scanner import scan
            from colony_sidecar.vector.tiers import get_tier_by_memory
            hw = scan()
            tier = get_tier_by_memory(hw.vram_gb, hw.ram_gb)
            spec = tier.text_embedder
            if spec:
                embed_provider = embed_provider or ("cuda" if hw.gpu_type == "cuda" else "cpu")
                embed_model = embed_model or spec.model_id
                embed_dims = embed_dims or str(spec.dims)
                reranker_model = reranker_model or (tier.text_reranker.model_id if tier.text_reranker else "")
                logger.info(
                    "Auto-detected embedding tier: %s (GPU=%s %dGB, RAM=%dGB) -> %s",
                    tier.label, hw.gpu_name, hw.vram_gb, hw.ram_gb, spec.model_id,
                )
        except Exception as exc:
            logger.warning("Hardware scan failed, using defaults: %s", exc)
            embed_provider = embed_provider or "cpu"
            embed_model = embed_model or "sentence-transformers/all-MiniLM-L6-v2"
            embed_dims = embed_dims or "384"

    try:
        from colony_sidecar.vector.embedder import EmbeddingPipeline
        from colony_sidecar.vector.config import EmbeddingConfig
        embed_config = EmbeddingConfig(
            provider=embed_provider,
            model_id=embed_model,
            dimensions=int(embed_dims) if embed_dims else 384,
        )
        from colony_sidecar.vector.embedder import make_provider
        pipeline = EmbeddingPipeline(make_provider(embed_config))

        # Wire up multimodal if enabled
        multimodal_enabled = os.environ.get("COLONY_MULTIMODAL", "false").lower() == "true"
        if multimodal_enabled:
            try:
                from colony_sidecar.vector.multimodal_provider import make_multimodal_provider
                from colony_sidecar.vector.image_store import make_image_store

                mm_config = EmbeddingConfig(
                    provider=embed_provider,
                    model_id=embed_model,  # Already set to multimodal model by activate-multimodal or init
                    dimensions=int(embed_dims) if embed_dims else 1024,
                    base_url=os.environ.get("COLONY_EMBED_BASE_URL"),
                    api_key=os.environ.get("COLONY_EMBED_API_KEY"),
                )
                mm_provider = make_multimodal_provider(mm_config)
                img_store = make_image_store(
                    mode=os.environ.get("COLONY_IMAGE_STORAGE", "local"),
                    state_dir=os.environ.get("COLONY_STATE_DIR", "."),
                )
                pipeline = EmbeddingPipeline(
                    provider=make_provider(embed_config),
                    multimodal_provider=mm_provider,
                    image_store=img_store,
                )
                logger.info("Multimodal enabled (model=%s, storage=%s)", embed_model, os.environ.get("COLONY_IMAGE_STORAGE", "local"))
            except Exception as exc:
                logger.warning("Multimodal init failed, falling back to text-only: %s", exc)

        await pipeline.warmup()
        set_embedder(pipeline)
        logger.info("EmbeddingPipeline initialized (provider=%s model=%s)", embed_provider, embed_model)

        # Wire embedding pipeline into ColonyGraph for vector-backed recall
        try:
            graph.set_embed_fn(pipeline.embed)
            from colony_sidecar.vector.store import VectorStore
            vector_db_path = os.path.join(state_dir, "lancedb")
            vs = VectorStore(data_dir=vector_db_path)
            embed_dims = int(os.environ.get("COLONY_EMBED_DIMS", pipeline.dimensions or 384))
            await vs.connect(dimensions=embed_dims)
            await vs.ensure_collections(dimensions=embed_dims)
            graph.set_vector_store(vs)
            logger.info("ColonyGraph wired to vector store (path=%s)", vector_db_path)

            # Verify end-to-end wiring
            if graph._embed_fn and graph._vector_store:
                logger.info("ColonyGraph fully operational (Neo4j + embeddings + vector store)")
            else:
                logger.warning("ColonyGraph partially wired — memory may be degraded")
        except Exception as vexc:
            logger.warning("Vector store wiring failed (recall will use keyword fallback): %s", vexc)

        # Pass LLM config to pipeline for auto-captioning
        llm_config_path = Path(os.environ.get("COLONY_STATE_DIR", ".")) / ".colony-llm-config.json"
        if llm_config_path.exists() and hasattr(pipeline, "set_llm_config"):
            try:
                llm_cfg = _json.loads(llm_config_path.read_text())
                pipeline.set_llm_config(llm_cfg)
                logger.info("LLM config passed to EmbeddingPipeline for auto-captioning")
            except Exception as exc:
                logger.debug("Could not pass LLM config to pipeline: %s", exc)

        # Health check + model mismatch detection
        try:
            hc = await pipeline.health_check()
            if hc.get("status") != "ok":
                logger.warning("Embedder health check failed: %s", hc.get("error", "unknown"))
            else:
                logger.info("Embedder health check passed (latency=%.1fms)", hc.get("latency_ms", 0))
        except Exception as exc:
            logger.warning("Embedder health check exception: %s", exc)
    except Exception as exc:
        logger.warning("EmbeddingPipeline init failed: %s", exc)

    # --- 6b. Reranker pipeline ---
    if reranker_model:
        try:
            from colony_sidecar.vector.reranker import make_reranker_provider
            from colony_sidecar.vector.scanner import scan
            hw = scan()
            reranker_provider = make_reranker_provider(
                spec=None,  # We only have the model_id, not the full spec
                gpu_type=hw.gpu_type,
            )
            # Override model_id since we only stored the string
            if reranker_provider:
                reranker_provider._model_id = reranker_model
                set_reranker(reranker_provider)
                logger.info("Reranker initialized (model=%s)", reranker_model)
        except Exception as exc:
            logger.warning("Reranker init failed: %s", exc)
    else:
        logger.info("No reranker configured for this tier")

    # --- 7. Goals engine ---
    try:
        from colony_sidecar.goals.engine import GoalEngine
        from colony_sidecar.goals.store import GoalStore
        goals_db = os.path.join(state_dir, "colony-goals.db")
        goals_store = GoalStore(db_path=goals_db)
        goals_engine = GoalEngine(store=goals_store)
        set_goals_engine(goals_engine)
        logger.info("GoalEngine initialized (db=%s)", goals_db)
    except Exception as exc:
        logger.warning("GoalEngine init failed: %s", exc)

    # --- 8. Contacts ---
    try:
        from colony_sidecar.contacts.store import SQLiteContactStore
        contacts_store = SQLiteContactStore()
        await contacts_store.connect()
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
    world_store = None
    try:
        from colony_sidecar.world_model.store import WorldModelStore
        world_store = WorldModelStore()
        await world_store.connect()
        set_world_store(world_store)
        logger.info("WorldModelStore initialized and connected")

        # Wire extraction pipeline
        try:
            from colony_sidecar.world_model.extraction.pipeline import ExtractionPipeline
            from colony_sidecar.world_model.extraction.formats import (
                TextExtractor, JSONExtractor, CSVExtractor,
                PDFExtractor, HTMLExtractor,
            )
            extractors = [TextExtractor(), JSONExtractor(), CSVExtractor()]
            if PDFExtractor:
                extractors.append(PDFExtractor())
            if HTMLExtractor:
                extractors.append(HTMLExtractor())
            pipeline = ExtractionPipeline(extractors=extractors)
            set_extraction_pipeline(pipeline)
            logger.info("Extraction pipeline initialized (%d format extractors)", len(extractors))
        except Exception as eexc:
            logger.warning("Extraction pipeline init skipped: %s", eexc)
    except Exception as exc:
        logger.warning("WorldModelStore init failed: %s", exc)
        # Try without connect() — some operations work without it
        try:
            world_store = WorldModelStore()
            set_world_store(world_store)
            logger.info("WorldModelStore initialized (without connect)")
        except Exception:
            pass

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
        from colony_sidecar.research.search.orchestrator import SearchOrchestrator

        # Wire search orchestrator
        search_orchestrator = SearchOrchestrator()
        search_provider = os.environ.get("COLONY_SEARCH_PROVIDER", "")
        if search_provider == "tavily" and os.environ.get("TAVILY_API_KEY"):
            from colony_sidecar.research.search.tavily import TavilyProvider
            search_orchestrator.add_provider(TavilyProvider(os.environ["TAVILY_API_KEY"]))
            logger.info("Search provider: Tavily")
        elif search_provider == "serpapi" and os.environ.get("SERPAPI_KEY"):
            from colony_sidecar.research.search.serpapi import SerpAPIProvider
            search_orchestrator.add_provider(SerpAPIProvider(os.environ["SERPAPI_KEY"]))
            logger.info("Search provider: SerpAPI")
        elif search_provider == "brave" and os.environ.get("BRAVE_API_KEY"):
            from colony_sidecar.research.search.brave import BraveSearchProvider
            search_orchestrator.add_provider(BraveSearchProvider(os.environ["BRAVE_API_KEY"]))
            logger.info("Search provider: Brave")
        else:
            logger.info("No search provider configured — web search unavailable")

        set_search_orchestrator(search_orchestrator)

        # Register native tools with the ToolExecutor
        if _tool_executor is not None:
            sandbox_dir = os.environ.get("COLONY_SANDBOX_DIR", str(state_dir / "sandbox"))
            _tool_executor.register_native_tools(
                search_orchestrator=search_orchestrator,
                sandbox_dir=sandbox_dir,
            )
            logger.info("Native tools registered (calculate, web_search, file_ops)")

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
    skills_registry = None
    try:
        from colony_sidecar.skills.registry import SkillRegistry
        skills_db_path = state_dir / "skills.db"
        skills_registry = SkillRegistry(db_path=skills_db_path)
        skills_registry.open()
        set_skills_registry(skills_registry)
        logger.info("SkillRegistry initialized (db=%s)", skills_db_path)
    except Exception as exc:
        logger.warning("SkillRegistry init failed: %s", exc)

    # --- 17. Chain / Identity ---
    try:
        from colony_sidecar.chain.identity import get_or_create_colony_id, load_genesis_manifest
        colony_id = get_or_create_colony_id(state_dir)

        # Load Genesis manifest
        genesis_path = Path(state_dir) / "genesis.json"
        if not genesis_path.exists():
            # Also check package directory (bundled manifest)
            pkg_genesis = Path(__file__).parent / "genesis.json"
            if pkg_genesis.exists():
                genesis_path = pkg_genesis
        load_genesis_manifest(genesis_path)

        from colony_sidecar.chain.manager import ChainManager
        chain = ChainManager(
            db_path=state_dir / "chain.db",
            colony_id=colony_id,
        )
        set_chain_manager(chain)
        logger.info("ChainManager initialized (colony_id=%s)", colony_id)

        # Wire local key manager
        try:
            from colony_sidecar.chain.local_keys import LocalKeyManager
            keys_dir = state_dir / "colony-keys"
            key_passphrase = os.environ.get("COLONY_KEY_PASSPHRASE", "")
            passphrase = key_passphrase.encode() if key_passphrase else None

            if (keys_dir / "private.pem").exists():
                key_mgr = LocalKeyManager(keys_dir=keys_dir, colony_id=colony_id, passphrase=passphrase)
                logger.info("LocalKeyManager loaded (public_key=%s...)", key_mgr.public_key_hex()[:16])
            else:
                key_mgr = LocalKeyManager.generate(keys_dir=keys_dir, colony_id=colony_id, passphrase=passphrase)
                logger.info("LocalKeyManager generated new keypair for colony %s", colony_id)

            chain._key_manager = key_mgr  # Attach to chain for access
        except Exception as kexc:
            logger.warning("LocalKeyManager init skipped: %s", kexc)

        # Initialize node identity
        try:
            from colony_sidecar.chain.node import get_or_create_node_id, ensure_node_keypair, create_node_certificate, load_node_certificate
            node_id = get_or_create_node_id(state_dir)
            node_km = ensure_node_keypair(state_dir)
            logger.info("Node identity: %s (public_key=%s...)", node_id, node_km.public_key_hex()[:16])

            # Create node certificate if missing
            cert_path = Path(state_dir) / "node-cert.json"
            if not cert_path.exists():
                cert = create_node_certificate(state_dir, colony_key_manager=key_mgr)
                logger.info("Node certificate created and signed by Colony key")
            else:
                logger.info("Node certificate exists")
        except Exception as nexc:
            logger.warning("Node identity init skipped: %s", nexc)

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

    # --- 19. Session store ---
    try:
        from colony_sidecar.sessions.store import InMemorySessionStore
        session_store = InMemorySessionStore()
        set_session_store(session_store)
        logger.info("InMemorySessionStore initialized")

        # Re-wire ResponseGate now that session store is available
        if _gate_ref is not None:
            from colony_sidecar.gate import ResponseGate
            new_gate = ResponseGate(_gate_config, session_store=session_store, audit_log=_gate_audit)
            set_response_gate(new_gate)
            logger.info("ResponseGate re-wired with SessionStore")
    except Exception as exc:
        logger.warning("SessionStore init failed: %s", exc)

    # --- 20. Task queue ---
    try:
        from colony_sidecar.task_queue.queue_manager import TaskQueueManager
        task_queue = await TaskQueueManager.initialize(
            db_path=state_dir / "task_queue.db",
        )
        set_task_queue(task_queue)
        logger.info("TaskQueueManager initialized")
    except Exception as exc:
        logger.warning("TaskQueueManager init failed: %s", exc)

    # --- 21. Autonomy loop ---
    try:
        from colony_sidecar.autonomy.loop import AutonomyLoop
        from colony_sidecar.autonomy.config import AutonomyConfig
        from colony_sidecar.autonomy.registry import SubsystemRegistry
        from colony_sidecar.autonomy.scheduler import AutonomyScheduler
        autonomy_config = AutonomyConfig.from_env()
        registry = SubsystemRegistry()
        autonomy_loop = AutonomyLoop(registry=registry, config=autonomy_config)
        set_autonomy_loop(autonomy_loop)

        # Wire scheduler
        scheduler = AutonomyScheduler(db_path=str(state_dir / "schedules.db"))
        set_scheduler(scheduler)
        logger.info("AutonomyScheduler initialized")

        # Register default periodic tasks
        scheduler.register("health_check", lambda: {"status": "ok"}, interval_seconds=300, metadata={"description": "Subsystem health check"})
        scheduler.register("signal_ingest", lambda: {"status": "ok"}, interval_seconds=600, metadata={"description": "Process queued behavioral signals"})
        scheduler.register("briefing_generate", lambda: {"status": "ok"}, interval_seconds=1800, metadata={"description": "Generate proactive briefings"})
        scheduler.register("memory_consolidate", lambda: {"status": "ok"}, interval_seconds=3600, metadata={"description": "Deduplicate and merge near-duplicate memories"})
        scheduler.register("cpi_track", lambda: {"status": "ok"}, interval_seconds=86400, metadata={"description": "Calculate Cognitive Performance Index"})
        scheduler.register("world_model_prune", lambda: {"status": "ok"}, interval_seconds=86400, metadata={"description": "Remove stale world model entities"})

        logger.info(
            "AutonomyLoop initialized (tick=%ds, scheduler=%d tasks)",
            autonomy_config.tick_interval_secs,
            len(scheduler.list_schedules()),
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
    if world_store is not None:
        try:
            await world_store.close()
        except Exception:
            pass
    if skills_registry is not None:
        try:
            skills_registry.close()
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
    set_session_store(None)
    # Stop task queue
    try:
        from colony_sidecar.api.routers.host import _task_queue
        if _task_queue is not None:
            await _task_queue.queue.stop()
    except Exception:
        pass
    set_task_queue(None)
    # Stop autonomy loop if running
    try:
        from colony_sidecar.api.routers.host import _autonomy_loop
        if _autonomy_loop is not None and _autonomy_loop.is_running:
            await _autonomy_loop.stop()
    except Exception:
        pass
    set_autonomy_loop(None)
    set_session_store(None)
    set_task_queue(None)
    logger.info("Sidecar shutdown complete")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="Colony Intelligence Sidecar",
        version="0.1.0",
        lifespan=lifespan,
    )

    # API key authentication (skips health/docs; open access if no key set)
    from colony_sidecar.api.middleware import ApiKeyMiddleware
    api_key = os.environ.get("COLONY_API_KEY")
    app.add_middleware(ApiKeyMiddleware, api_key=api_key)
    if api_key:
        logger.info("API key authentication enabled")
    else:
        logger.warning("No COLONY_API_KEY set — API is open (dev mode)")

    app.include_router(host_router)
    return app


# Uvicorn entry point
app = create_app()
