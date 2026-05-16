"""Colony sidecar FastAPI server.

Intelligence sidecar server mounted by agent frameworks (OpenClaw, Hermes,
etc.) as a plugin via the ``/v1/host`` API surface.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

# Load ~/.colony/.env before any config reads (mirrors CLI behaviour for
# service/standalone launches that skip the CLI entrypoint).
_env_loaded = False
if not _env_loaded:
    for _env_path in (Path.home() / ".colony" / ".env", Path.cwd() / ".env"):
        if _env_path.exists():
            with open(_env_path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#"):
                        continue
                    if "=" in _line:
                        _k, _v = _line.split("=", 1)
                        _k = _k.strip()
                        _v = _v.strip()
                        if _k not in os.environ:
                            os.environ[_k] = _v
            break
    _env_loaded = True

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
    set_reranker,
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
    set_insight_store,
    set_learner,
    set_skills_registry,
    set_skill_executor,
    set_secrets_manager,
    set_session_store,
    set_task_queue,
    set_commitment_store,
    set_affect_store,
    set_facts_store,
    set_pattern_store,
    set_surprise_store,
    set_tom_extractor,
    # Multi-Agent v0.7.0
    set_agent_store,
    set_invite_store,
    set_initiative_store,
    set_assignment_engine,
    set_websocket_manager,
    set_telemetry,
    supported_capabilities,
)

from colony_sidecar import get_state_dir

logger = logging.getLogger(__name__)


def _state_dir() -> Path:
    """Resolve the Colony state directory (wrapper for get_state_dir)."""
    return get_state_dir()


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
                if hw.gpu_type == "cuda":
                    embed_provider = embed_provider or "cuda"
                elif hw.gpu_type == "mlx":
                    # Prefer native MLX when the package is available
                    try:
                        import mlx_embeddings  # noqa: F401
                        embed_provider = embed_provider or "native_mlx"
                    except ImportError:
                        embed_provider = embed_provider or "mlx"
                else:
                    embed_provider = embed_provider or "cpu"
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
        provider = make_provider(embed_config)
        if provider is None:
            # 'skip' provider — embeddings disabled entirely
            logger.info("EmbeddingPipeline skipped (provider=skip) — embeddings disabled")
        else:
            pipeline = EmbeddingPipeline(provider)

            # Wire up multimodal if enabled
            multimodal_enabled = os.environ.get("COLONY_MULTIMODAL", "false").lower() == "true"
            if multimodal_enabled:
                try:
                    from colony_sidecar.vector.multimodal_provider import make_multimodal_provider
                    from colony_sidecar.vector.image_store import make_image_store

                    mm_config = EmbeddingConfig(
                        provider=embed_provider,
                        model_id=embed_model,
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
    if reranker_model and reranker_model.lower() not in ("none", "", "null"):
        try:
            from colony_sidecar.vector.reranker import (
                NativeMLXRerankerProvider,
                MLXRerankerProvider,
                CPURerankerProvider,
                CUDARerankerProvider,
            )
            from colony_sidecar.vector.scanner import scan
            hw = scan()
            if hw.gpu_type == "mlx":
                # Prefer native MLX when the package is available
                try:
                    import mlx_lm  # noqa: F401
                    reranker_provider = NativeMLXRerankerProvider(reranker_model)
                except ImportError:
                    reranker_provider = MLXRerankerProvider(reranker_model)
            elif hw.gpu_type == "cuda":
                reranker_provider = CUDARerankerProvider(reranker_model)
            else:
                reranker_provider = CPURerankerProvider(reranker_model)
            await reranker_provider.warmup()
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

    # --- 7b. Commitment Store ---
    try:
        from colony_sidecar.commitments.store import CommitmentStore

        commitments_db = state_dir / "colony-commitments.db"
        commitment_store = CommitmentStore(db_path=commitments_db)
        set_commitment_store(commitment_store)
        logger.info("CommitmentStore initialized (db=%s)", commitments_db)
    except Exception as exc:
        logger.warning("CommitmentStore init failed: %s", exc)

    # --- 7c. Theory of Mind ---
    try:
        from colony_sidecar.tom.affect import AffectStore
        from colony_sidecar.tom.facts import SharedFactsStore

        affect_db = state_dir / "colony-affect.db"
        affect_store = AffectStore(db_path=affect_db)
        set_affect_store(affect_store)
        logger.info("AffectStore initialized (db=%s)", affect_db)

        facts_db = state_dir / "colony-facts.db"
        facts_store = SharedFactsStore(db_path=facts_db)
        set_facts_store(facts_store)
        logger.info("SharedFactsStore initialized (db=%s)", facts_db)
    except Exception as exc:
        logger.warning("Theory of Mind init failed: %s", exc)

    # --- Pattern Extraction + Surprise ---
    try:
        from colony_sidecar.patterns.store import PatternStore
        from colony_sidecar.surprise.store import SurpriseStore

        patterns_db = state_dir / "colony-patterns.db"
        pattern_store = PatternStore(db_path=patterns_db)
        set_pattern_store(pattern_store)
        logger.info("PatternStore initialized (db=%s)", patterns_db)

        surprise_db = state_dir / "colony-surprise.db"
        surprise_store = SurpriseStore(db_path=surprise_db)
        set_surprise_store(surprise_store)
        logger.info("SurpriseStore initialized (db=%s)", surprise_db)
    except Exception as exc:
        logger.warning("Pattern/Surprise init failed: %s", exc)

    # --- ToM LLM Extractor ---
    try:
        if llm_router is not None:
            from colony_sidecar.tom.extractor import TomExtractor
            tom_extractor = TomExtractor(llm_router)
            set_tom_extractor(tom_extractor)
            logger.info("ToM LLM Extractor initialized (router=%s)", type(llm_router).__name__)
        else:
            logger.info("ToM LLM Extractor skipped — no LLM router")
    except Exception as exc:
        logger.warning("ToM Extractor init failed: %s", exc)

    # --- 8. Contacts ---
    contacts_store = None
    try:
        from colony_sidecar.contacts.store import SQLiteContactStore
        contacts_store = SQLiteContactStore(graph=graph)
        await contacts_store.connect()
        set_contacts_store(contacts_store)
        logger.info("ContactsStore initialized")
    except Exception as exc:
        logger.warning("ContactsStore init failed: %s", exc)

    # --- 8b. Contact-World Model Bridge ---
    if contacts_store is not None and graph is not None:
        try:
            from colony_sidecar.contacts.world_bridge import WorldModelContactBridge
            bridge = WorldModelContactBridge(graph=graph, store=contacts_store)
            # Backfill all substantive Person nodes on startup
            backfill_stats = await bridge.backfill_all_people()
            logger.info(
                "WorldModelContactBridge initialized — backfill created=%d linked=%d skipped=%d",
                backfill_stats["created"], backfill_stats["linked"], backfill_stats["skipped"],
            )
            # Prune shadow contacts whose Person node no longer exists
            pruned = await bridge.prune_orphaned_shadows()
            if pruned:
                logger.info("Pruned %d orphaned shadow contacts", pruned)
        except Exception as exc:
            logger.warning("WorldModelContactBridge init failed: %s", exc)
    else:
        logger.info("WorldModelContactBridge skipped — contacts_store or graph unavailable")

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
        from colony_sidecar.world_model.config import WorldModelConfig
        _wm_backend = os.environ.get("WORLD_MODEL_BACKEND", "sqlite")
        world_store = WorldModelStore(WorldModelConfig(backend=_wm_backend))
        await world_store.connect()
        set_world_store(world_store)
        logger.info("WorldModelStore initialized and connected (backend=%s)", _wm_backend)

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
            llm_extract_fn = None
            if llm_router is not None:
                try:
                    from colony_sidecar.world_model.extraction.llm_extractor import (
                        build_llm_extract_fn,
                    )
                    llm_extract_fn = build_llm_extract_fn(llm_router)
                except Exception as llm_exc:
                    logger.warning("LLM extraction fallback disabled: %s", llm_exc)

            pipeline = ExtractionPipeline(
                extractors=extractors,
                llm_extract_fn=llm_extract_fn,
            )
            set_extraction_pipeline(pipeline)
            logger.info(
                "Extraction pipeline initialized (%d format extractors, llm_fallback=%s)",
                len(extractors),
                "on" if llm_extract_fn is not None else "off",
            )
        except Exception as eexc:
            logger.warning("Extraction pipeline init skipped: %s", eexc)
    except Exception as exc:
        logger.warning("WorldModelStore init failed: %s", exc)
        # Try without connect() — some operations work without it
        try:
            world_store = WorldModelStore(WorldModelConfig(backend=_wm_backend))
            set_world_store(world_store)
            logger.info("WorldModelStore initialized (without connect)")
        except Exception as exc2:
            logger.error("WorldModelStore fallback init also failed: %s", exc2)

    # --- 11. Cognition (CognitionPipeline) ---
    cognition_pipeline = None
    try:
        from colony_sidecar.intelligence.cognition.registry import CognitionPipeline
        from colony_sidecar.events.bus import EventBus
        
        if graph is not None:
            # Create EventBus for real-time metrics
            event_bus = EventBus()
            
            cognition_pipeline = CognitionPipeline(
                graph=graph,
                event_bus=event_bus,
            )
            set_metalearner(cognition_pipeline.meta_learner)
            logger.info("CognitionPipeline initialized with all components wired")
        else:
            logger.warning("CognitionPipeline skipped — ColonyGraph not available")
    except Exception as exc:
        logger.warning("CognitionPipeline init failed: %s", exc, exc_info=True)

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
            # Zero-config fallback so web_search works out of the box.
            from colony_sidecar.research.search.duckduckgo import DuckDuckGoProvider
            search_orchestrator.add_provider(DuckDuckGoProvider())
            logger.info("Search provider: DuckDuckGo (default fallback)")

        set_search_orchestrator(search_orchestrator)

        # Register native tools with the ToolExecutor
        try:
            import colony_sidecar.api.routers.host as _host_router
            te = _host_router._tool_executor
        except Exception:
            te = None
        if te is not None:
            sandbox_dir = os.environ.get("COLONY_SANDBOX_DIR", str(state_dir / "sandbox"))
            # Ensure the sandbox directory exists so file_ops don't fail on first call.
            Path(sandbox_dir).mkdir(parents=True, exist_ok=True)
            te.register_native_tools(
                search_orchestrator=search_orchestrator,
                sandbox_dir=sandbox_dir,
            )
            logger.info(
                "Native tools registered (calculate, web_search, file_ops; sandbox=%s)",
                sandbox_dir,
            )

        research = ResearchPipeline()
        set_research_pipeline(research)
        logger.info("ResearchPipeline initialized")
    except Exception as exc:
        logger.warning("ResearchPipeline init failed: %s", exc, exc_info=True)

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

    # Insight overlay store (tracks dismissed-insight IDs).
    try:
        from colony_sidecar.intelligence.synthesis.insight_store import InsightStore
        insight_store = InsightStore(state_dir / "insights.db")
        set_insight_store(insight_store)
        logger.info("InsightStore initialized")
    except Exception as exc:
        logger.warning("InsightStore init failed: %s", exc)

    # --- 15. Continuous learner ---
    try:
        from colony_sidecar.intelligence.learning.continuous_learner import ContinuousLearner
        learner = ContinuousLearner()
        set_learner(learner)
        logger.info("ContinuousLearner initialized")
    except Exception as exc:
        logger.warning("ContinuousLearner init failed: %s", exc)

    # --- 16. Skills registry + executor ---
    skills_registry = None
    try:
        from colony_sidecar.skills.registry import SkillRegistry
        skills_db_path = state_dir / "skills.db"
        skills_registry = SkillRegistry(db_path=skills_db_path)
        skills_registry.open()
        set_skills_registry(skills_registry)
        logger.info("SkillRegistry initialized (db=%s)", skills_db_path)

        try:
            from colony_sidecar.skills.executor import SkillExecutor
            from colony_sidecar.skills.security.guards import CapabilityGuard
            from colony_sidecar.skills.security.scanner import ASTScanner
            skill_executor = SkillExecutor(
                registry=skills_registry,
                guard=CapabilityGuard(),
                scanner=ASTScanner(),
            )
            set_skill_executor(skill_executor)
            logger.info("SkillExecutor initialized")
        except Exception as sexc:
            logger.warning("SkillExecutor init failed: %s", sexc)
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
    task_queue = None
    try:
        from colony_sidecar.task_queue.queue_manager import TaskQueueManager
        task_queue = await TaskQueueManager.initialize(
            db_path=state_dir / "task_queue.db",
        )
        set_task_queue(task_queue)
        logger.info("TaskQueueManager initialized")
    except Exception as exc:
        logger.warning("TaskQueueManager init failed: %s", exc)

    # --- 20b. Worker node (executes queued jobs) ---
    worker_task = None
    if task_queue is not None:
        try:
            import asyncio as _asyncio
            from colony_sidecar.task_queue.worker import WorkerNode
            from colony_sidecar.task_queue.handlers.registry import build_default_handlers
            from colony_sidecar.chain.node import get_or_create_node_id
            import colony_sidecar.api.routers.host as _host_mod

            worker_node_id = get_or_create_node_id(state_dir)
            handlers = build_default_handlers(
                router=_host_mod._llm_router,
                world_model_store=_host_mod._world_store,
                contact_store=_host_mod._contacts_store,
                response_gate=_host_mod._response_gate,
                node_id=worker_node_id,
            )
            worker = WorkerNode(
                node_id=worker_node_id,
                queue=task_queue.queue,
                handlers=handlers,
            )
            worker_task = _asyncio.create_task(worker.start())
            app.state.worker = worker
            app.state.worker_task = worker_task
            logger.info(
                "WorkerNode started (node=%s, handlers=%s)",
                worker_node_id, [jt.value for jt in handlers.keys()],
            )
        except Exception as exc:
            logger.warning("WorkerNode init failed — queued jobs will not execute: %s", exc, exc_info=True)

    # --- 20c. Multi-Agent System (v0.7.0) ---
    try:
        from colony_sidecar.agents.store import AgentStore, InviteStore
        from colony_sidecar.initiatives.store import InitiativeStore
        from colony_sidecar.initiatives.assignment import AssignmentEngine
        from colony_sidecar.agents.websocket import WebSocketManager

        agent_store = AgentStore(state_dir=state_dir)
        invite_store = InviteStore(state_dir=state_dir)
        set_agent_store(agent_store)
        set_invite_store(invite_store)
        logger.info("AgentStore initialized (state_dir=%s)", state_dir)

        initiative_store = InitiativeStore(state_dir=state_dir)
        set_initiative_store(initiative_store)
        logger.info("InitiativeStore initialized (state_dir=%s)", state_dir)

        assignment_engine = AssignmentEngine(
            agent_store=agent_store,
            initiative_store=initiative_store,
        )
        set_assignment_engine(assignment_engine)
        logger.info("AssignmentEngine initialized")

        websocket_manager = WebSocketManager(
            agent_store=agent_store,
            initiative_store=initiative_store,
        )
        set_websocket_manager(websocket_manager)
        logger.info("WebSocketManager initialized")
    except Exception as exc:
        logger.warning("Multi-Agent System init failed: %s", exc)

    # --- 21. Autonomy loop ---
    autonomy_config = None
    registry = None
    scheduler = None
    autonomy_loop = None
    try:
        from colony_sidecar.autonomy.loop import AutonomyLoop
        from colony_sidecar.autonomy.config import AutonomyConfig
        from colony_sidecar.autonomy.registry import SubsystemRegistry
        from colony_sidecar.autonomy.scheduler import AutonomyScheduler
        autonomy_config = AutonomyConfig.from_env()
        registry = SubsystemRegistry()

        # Wire scheduler BEFORE the loop so the loop gets a direct reference.
        scheduler = AutonomyScheduler(db_path=str(state_dir / "schedules.db"))
        set_scheduler(scheduler)
        logger.info("AutonomyScheduler initialized")

        autonomy_loop = AutonomyLoop(
            registry=registry,
            config=autonomy_config,
            scheduler=scheduler,
        )
        set_autonomy_loop(autonomy_loop)

        # Register default periodic tasks
        scheduler.register("health_check", lambda: {"status": "ok"}, interval_seconds=300, metadata={"description": "Subsystem health check"})
        scheduler.register("signal_ingest", lambda: {"status": "ok"}, interval_seconds=600, metadata={"description": "Process queued behavioral signals"})
        scheduler.register("briefing_generate", lambda: {"status": "ok"}, interval_seconds=1800, metadata={"description": "Generate proactive briefings"})

        async def _run_memory_consolidate():
            from colony_sidecar.api.routers.host import _consolidator as c
            if c is None:
                return {"status": "skipped", "reason": "consolidator_not_wired"}
            result = await c.run()
            return {"status": "ok", "merged": getattr(result, "merged_count", 0)}

        scheduler.register("memory_consolidate", _run_memory_consolidate, interval_seconds=3600, metadata={"description": "Deduplicate and merge near-duplicate memories"})
        scheduler.register("cpi_track", lambda: {"status": "ok"}, interval_seconds=86400, metadata={"description": "Calculate Cognitive Performance Index"})
        scheduler.register("world_model_prune", lambda: {"status": "ok"}, interval_seconds=86400, metadata={"description": "Remove stale world model entities"})

        async def _run_digest_flush():
            from colony_sidecar.api.routers.host import _delivery_bridge as bridge
            if bridge is None:
                return {"status": "skipped", "reason": "delivery_bridge_not_wired"}
            header = os.environ.get("COLONY_DIGEST_HEADER", "Daily digest")
            return await bridge.flush_digests_to_gateway(header=header)

        digest_interval = int(os.environ.get("COLONY_DIGEST_INTERVAL_SECONDS", "86400"))
        scheduler.register(
            "digest_flush",
            _run_digest_flush,
            interval_seconds=digest_interval,
            metadata={"description": "Bundle and deliver accumulated DIGEST-channel items"},
        )

        logger.info(
            "AutonomyLoop initialized (tick=%ds, scheduler=%d tasks)",
            autonomy_config.tick_interval_secs,
            len(scheduler.list_schedules()),
        )

        # Auto-start the autonomy loop as a background task
        asyncio.create_task(autonomy_loop.start())
        logger.info("AutonomyLoop auto-start scheduled")
    except Exception as exc:
        logger.warning("AutonomyLoop init failed: %s", exc)

    from colony_sidecar.telemetry import TelemetryStore
    telemetry = TelemetryStore()
    telemetry.started_at = datetime.now(timezone.utc)
    app.state.telemetry = telemetry
    set_telemetry(telemetry)
    logger.info("TelemetryStore initialized")

    # Register owner check-in task (silence-triggered proactive outreach)
    # Must be after telemetry is initialized since it queries silence hours.
    try:
        if (
            autonomy_config is not None
            and autonomy_config.owner_check_in_enabled
            and registry is not None
            and scheduler is not None
        ):
            from colony_sidecar.autonomy.checkin import OwnerCheckInTask
            _checkin_task = OwnerCheckInTask(
                registry=registry,
                config=autonomy_config,
                event_bus=autonomy_loop.events if autonomy_loop is not None and hasattr(autonomy_loop, "events") else None,
                telemetry=telemetry,
            )
            # Run every 30 minutes (half the default silence threshold of 1 hour)
            checkin_interval = max(600, int(autonomy_config.owner_check_in_silent_hours * 1800))
            scheduler.register(
                "owner_check_in",
                _checkin_task.run,
                interval_seconds=checkin_interval,
                metadata={"description": "Check for initiative silence and reach out to owner if needed"},
            )
            logger.info(
                "Owner check-in registered (silent_threshold=%.1fh, cooldown=%.1fh, interval=%ds)",
                autonomy_config.owner_check_in_silent_hours,
                autonomy_config.owner_check_in_cooldown_hours,
                checkin_interval,
            )
    except Exception as exc:
        logger.warning("Owner check-in registration failed: %s", exc)

    # Register conversation synthesis task (periodic memory scan for goals)
    try:
        if (
            autonomy_config is not None
            and getattr(autonomy_config, "conversation_synthesis_enabled", True)
            and registry is not None
            and scheduler is not None
        ):
            from colony_sidecar.autonomy.synthesis import ConversationSynthesisTask
            _synthesis_task = ConversationSynthesisTask(
                registry=registry,
                lookback_hours=getattr(autonomy_config, "conversation_synthesis_lookback_hours", 2.0),
                min_confidence=getattr(autonomy_config, "conversation_synthesis_min_confidence", 0.35),
            )
            synthesis_interval = int(getattr(autonomy_config, "conversation_synthesis_interval_secs", 1800.0))
            scheduler.register(
                "conversation_synthesis",
                _synthesis_task.run,
                interval_seconds=synthesis_interval,
                metadata={"description": "Scan conversation memories for implicit goals and commitments"},
            )
            logger.info(
                "Conversation synthesis registered (lookback=%.1fh, interval=%ds, min_conf=%.2f)",
                getattr(autonomy_config, "conversation_synthesis_lookback_hours", 2.0),
                synthesis_interval,
                getattr(autonomy_config, "conversation_synthesis_min_confidence", 0.35),
            )
    except Exception as exc:
        logger.warning("Conversation synthesis registration failed: %s", exc)

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
    set_commitment_store(None)
    set_affect_store(None)
    set_facts_store(None)
    set_pattern_store(None)
    set_surprise_store(None)
    set_tom_extractor(None)
    set_chain_manager(None)
    set_secrets_manager(None)
    set_session_store(None)
    # Stop worker node (before queue so in-flight jobs can drain).
    try:
        worker = getattr(app.state, "worker", None)
        if worker is not None:
            await worker.stop(drain_timeout=10.0)
        worker_task = getattr(app.state, "worker_task", None)
        if worker_task is not None:
            worker_task.cancel()
    except Exception:
        logger.debug("Worker shutdown error", exc_info=True)
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
    # Multi-Agent cleanup
    set_agent_store(None)
    set_invite_store(None)
    set_initiative_store(None)
    set_assignment_engine(None)
    set_websocket_manager(None)
    logger.info("Sidecar shutdown complete")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="Colony Intelligence Sidecar",
        version="0.1.0",
        lifespan=lifespan,
    )

    # API key authentication (skips health/docs; open access if no key set)
    from colony_sidecar.api.middleware import ApiKeyMiddleware, BodySizeLimitMiddleware

    # Body-size cap runs before auth so oversized payloads are rejected with
    # 413 regardless of the auth state.
    try:
        max_body = int(os.environ.get("COLONY_MAX_BODY_BYTES", "") or 10 * 1024 * 1024)
    except ValueError:
        max_body = 10 * 1024 * 1024
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body)

    api_key = os.environ.get("COLONY_API_KEY")
    app.add_middleware(ApiKeyMiddleware, api_key=api_key)
    if api_key:
        logger.info("API key authentication enabled")
    else:
        logger.warning("No COLONY_API_KEY set — API is open (dev mode)")

    app.include_router(host_router)

    # MCP streamable HTTP endpoint
    try:
        from colony_sidecar.mcp.server import create_server
        mcp_server = create_server()
        # Mount MCP ASGI app at /mcp
        mcp_asgi = mcp_server.streamable_http_app()
        app.mount("/mcp", mcp_asgi)
        logger.info("MCP endpoint mounted at /mcp (streamable HTTP)")
    except ImportError:
        logger.debug("MCP SDK not installed — /mcp endpoint not available (install colonyai[mcp])")
    except Exception as exc:
        logger.warning("Could not mount MCP endpoint: %s", exc)

    return app


# Uvicorn entry point
app = create_app()
