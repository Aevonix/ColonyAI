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
    set_context_provenance_store,
    set_response_guard,
    set_engagement_store,
    set_comms_log,
    set_preference_learner,
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
    set_session_report_store,
    set_agent_bridge,
    set_initiative_executor,
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

    # --- 0. Adaptive parameters (meta-learning read-back path) ---
    # Created first so downstream consumers (consolidator, graph recall,
    # cognition pipeline) can take a handle; the ActionJournal is attached
    # in the self-model section once it exists.
    _adaptive_params = None
    try:
        from colony_sidecar.self_model.params import (
            AdaptiveParamStore, register_core_params,
        )
        _adaptive_params = AdaptiveParamStore(
            db_path=str(state_dir / "colony-params.db"))
        register_core_params(_adaptive_params)
        try:
            from colony_sidecar.api.routers.host import set_adaptive_params
            set_adaptive_params(_adaptive_params)
        except ImportError:
            pass
        logger.info("AdaptiveParamStore initialized (db=%s)",
                    state_dir / "colony-params.db")
    except Exception as exc:
        logger.warning("AdaptiveParamStore init failed: %s", exc)

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
        # Apply graph schema constraints/indexes before any queries run
        try:
            from colony_sidecar.intelligence.graph.migrations import run_migrations
            await run_migrations(graph.driver, database=neo4j_db)
        except Exception as exc:
            logger.warning("Graph migrations failed (queries may be degraded): %s", exc)
        set_graph(graph)
        # Wire graph into ToolExecutor for capability-gap detection
        try:
            import colony_sidecar.api.routers.host as _host_router
            te = _host_router._tool_executor
            if te is not None:
                te._graph = graph
        except Exception:
            logger.warning("ToolExecutor graph wiring failed (capability-gap detection degraded)")
        logger.info("ColonyGraph initialized (uri=%s db=%s)", neo4j_uri, neo4j_db)

        # Ensure Colony self-representation in graph (v0.11.0)
        try:
            await graph.ensure_colony_self()
        except Exception as self_exc:
            logger.warning("Colony self-representation setup skipped: %s", self_exc)

        # Wire consolidator (adaptive merge threshold when params wired)
        try:
            from colony_sidecar.intelligence.graph.consolidator import MemoryConsolidator
            consolidator = MemoryConsolidator(graph, params=_adaptive_params)
            set_consolidator(consolidator)
            logger.info("MemoryConsolidator initialized")
        except Exception as cexc:
            logger.warning("MemoryConsolidator init skipped: %s", cexc)
        if _adaptive_params is not None:
            try:
                graph.set_adaptive_params(_adaptive_params)
            except Exception:
                logger.debug("graph adaptive-params wiring failed", exc_info=True)
    except Exception as exc:
        logger.warning("ColonyGraph init failed — memory endpoints will be degraded: %s", exc)

    # --- 4. Response Gate (safety pipeline) ---
    _gate_ref = None
    _gate_config = None
    _gate_audit = None
    try:
        from colony_sidecar.gate import ResponseGate, GateConfig
        from colony_sidecar.gate.audit import InMemoryAuditLog
        # L7 send-delay (the cancel window) is env-tunable; default 0 = no
        # hold. Set COLONY_GATE_SEND_DELAY_SECS>0 to enable a real cancel
        # window on the request-path gate.
        try:
            _gate_delay = float(os.environ.get("COLONY_GATE_SEND_DELAY_SECS", "0"))
        except ValueError:
            _gate_delay = 0.0
        gate_config = GateConfig(send_delay_seconds=_gate_delay)
        gate_audit = InMemoryAuditLog()
        gate = ResponseGate(gate_config, session_store=None, audit_log=gate_audit)
        set_response_gate(gate)
        # Stash refs for re-wiring after session store is available
        _gate_ref = gate
        _gate_config = gate_config
        _gate_audit = gate_audit
        logger.info("ResponseGate initialized (send_delay=%.1fs, secondary_review=%s)",
                    _gate_delay, getattr(gate_config, "enable_secondary_review", False))

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
        if provider is not None and embed_provider == "openai_api" and hasattr(provider, "configure"):
            # openai_api needs explicit endpoint config (the text-only path
            # never called configure(); only the multimodal branch passed these).
            provider.configure(
                os.environ.get("COLONY_EMBED_BASE_URL", ""),
                os.environ.get("COLONY_EMBED_API_KEY", ""),
            )
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
    reranker_provider_name = os.environ.get("COLONY_RERANKER_PROVIDER", "")
    if reranker_model and reranker_model.lower() not in ("none", "", "null"):
        try:
            from colony_sidecar.vector.reranker import (
                OpenAIAPIRerankerProvider,
                NativeMLXRerankerProvider,
                MLXRerankerProvider,
                CPURerankerProvider,
                CUDARerankerProvider,
            )
            reranker_base_url = os.environ.get("COLONY_RERANKER_BASE_URL", "")
            reranker_api_key = os.environ.get("COLONY_RERANKER_API_KEY", "")
            if reranker_provider_name == "openai_api" or reranker_base_url:
                # Remote reranker over an OpenAI/Jina-compatible /v1/rerank
                # endpoint, mirroring the embedder's openai_api path so the
                # model stays off-box instead of loading in-process.
                # COLONY_RERANKER_PROMPT_STYLE=qwen3 applies the Qwen3-Reranker
                # instruction template, without which its scores are noise.
                reranker_provider = OpenAIAPIRerankerProvider(reranker_model)
                reranker_provider.configure(
                    reranker_base_url,
                    reranker_api_key,
                    os.environ.get("COLONY_RERANKER_PROMPT_STYLE", ""),
                )
            else:
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
            logger.info(
                "Reranker initialized (provider=%s model=%s)",
                reranker_provider_name or "local", reranker_model,
            )
        except Exception as exc:
            logger.warning("Reranker init failed: %s", exc)
    else:
        logger.info("No reranker configured for this tier")

    # --- 7. Goals engine ---
    goals_engine = None
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

        # Resolving a workspace concern raised from a commitment settles the
        # commitment itself — without this, the ingest loop re-raises the
        # concern from the still-open commitment and the resolve is cosmetic.
        from colony_sidecar.self_model.settlement import register_settler

        def _settle_commitment(source_id, *, outcome="done", note="",
                               resolved_by="owner", _cs=commitment_store):
            row = _cs.resolve(source_id, outcome=outcome, note=note,
                              resolved_by=resolved_by)
            return {"kind": "commitment", "status": row["status"]} if row else None

        register_settler("commitment", _settle_commitment)
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

        from colony_sidecar.gate.context_provenance import (
            ContextProvenanceStore, ProvenanceCrossContextGuard)
        provenance_db = state_dir / "colony-context-provenance.db"
        provenance_store = ContextProvenanceStore(db_path=str(provenance_db))
        set_context_provenance_store(provenance_store)
        logger.info("ContextProvenanceStore initialized (db=%s)", provenance_db)

        # Outbound response gate (opt-in; shadow by default). The embedding deployment
        # supplies the mode and any excluded gateways (e.g. its voice path) via env —
        # Colony hardcodes neither.
        from colony_sidecar.gate.response_guard import ResponseGuard, GuardMode
        from colony_sidecar.world_model.extraction.conversation_extractor import (
            ConversationExtractor)
        _guard_mode = (GuardMode.ENFORCE
                       if os.environ.get("COLONY_GUARD_MODE", "").strip().lower() == "enforce"
                       else GuardMode.SHADOW)
        _excluded = [g.strip() for g in
                     os.environ.get("COLONY_GUARD_EXCLUDED_GATEWAYS", "").split(",") if g.strip()]
        from colony_sidecar.gate.guard_audit import GuardAuditStore
        guard_audit_db = state_dir / "colony-guard-audit.db"
        guard_audit_store = GuardAuditStore(db_path=str(guard_audit_db))
        set_response_guard(ResponseGuard(
            cross_context=ProvenanceCrossContextGuard(provenance_store, extractor=ConversationExtractor()),
            default_mode=_guard_mode, excluded_gateways=_excluded, audit_store=guard_audit_store))
        logger.info("ResponseGuard initialized (mode=%s, excluded_gateways=%s, audit=%s)",
                    _guard_mode.value, _excluded or "[]", guard_audit_db)

        from colony_sidecar.tom.engagement import EngagementStore
        engagement_db = state_dir / "colony-engagement.db"
        engagement_store = EngagementStore(db_path=engagement_db)
        set_engagement_store(engagement_store)
        logger.info("EngagementStore initialized (db=%s)", engagement_db)

        from colony_sidecar.contacts.comms import CommsLog
        comms_log = CommsLog(db_path=state_dir / "colony-comms.db")
        set_comms_log(comms_log)
        logger.info("CommsLog initialized")

        # Owner preference learner — captures the owner's *explicit* directives
        # about how to communicate ("be concise", "use bullets", "no emoji") at
        # high confidence; complements the inferred per-contact EngagementStore.
        from colony_sidecar.intelligence.components.preference_learner import PreferenceLearner
        preference_learner = PreferenceLearner(db_path=str(state_dir / "colony-preferences.db"))
        set_preference_learner(preference_learner)
        logger.info("PreferenceLearner initialized (db=%s)", state_dir / "colony-preferences.db")
    except Exception as exc:
        logger.warning("Theory of Mind init failed: %s", exc)

    # --- Directive / boundary memory (safety foundation) ---
    # Durable store of the owner's standing directives (MUST NOT / MUST) with an
    # enforcement guard consulted before autonomous actions. Boundaries must be
    # available before any action-taking, so this is wired unconditionally.
    try:
        from colony_sidecar.directives import DirectiveManager, DirectiveStore
        from colony_sidecar.api.routers.host import set_directive_manager
        _directive_store = DirectiveStore(db_path=str(state_dir / "colony-directives.db"))
        set_directive_manager(DirectiveManager(_directive_store))
        logger.info(
            "DirectiveManager initialized (db=%s, active=%d)",
            state_dir / "colony-directives.db", _directive_store.count_active(),
        )
    except Exception as exc:
        logger.warning("DirectiveManager init failed (boundaries disabled): %s", exc)

    # --- Proposal store (self-directed thinking + research -> proposals) ---
    try:
        from colony_sidecar.proposals import ProposalStore
        from colony_sidecar.api.routers.host import set_proposal_store
        _proposal_store = ProposalStore(db_path=str(state_dir / "colony-proposals.db"))
        set_proposal_store(_proposal_store)
        logger.info("ProposalStore initialized (db=%s)", state_dir / "colony-proposals.db")
    except Exception as exc:
        logger.warning("ProposalStore init failed: %s", exc)

    # --- Type feedback store (outcome-driven priority decay/boost) ---
    try:
        from colony_sidecar.feedback import TypeFeedbackStore
        from colony_sidecar.api.routers.host import set_feedback_store
        set_feedback_store(TypeFeedbackStore(db_path=str(state_dir / "colony-feedback.db")))
        logger.info("TypeFeedbackStore initialized (db=%s)", state_dir / "colony-feedback.db")
    except Exception as exc:
        logger.warning("TypeFeedbackStore init failed: %s", exc)

    # --- Self-model / trust engine + action journal (item 4, Amendment 1) ---
    # Wired before directed action so approval tiering can consult trust.
    _sm_for_directed = None
    try:
        from colony_sidecar.self_model import (
            ActionJournal, CompetenceStore, SelfModel, TrustEngine,
            self_model_enabled,
        )
        from colony_sidecar.api.routers.host import (
            set_self_model, _feedback_store as _fb_for_trust,
        )
        if self_model_enabled():
            from colony_sidecar.autonomy.registry import SubsystemRegistry as _Reg
            _competence = CompetenceStore(
                db_path=str(state_dir / "colony-self-model.db"))
            _journal = ActionJournal(
                db_path=str(state_dir / "colony-action-journal.db"))
            _trust = TrustEngine(
                _competence, db_path=str(state_dir / "colony-self-model.db"),
                feedback_store=_fb_for_trust, journal=_journal)
            _sm_for_directed = SelfModel(_competence, registry=_Reg(),
                                         trust=_trust, journal=_journal)
            set_self_model(_sm_for_directed)
            if _adaptive_params is not None:
                _adaptive_params.set_journal(_journal)
            logger.info(
                "SelfModel/TrustEngine initialized (db=%s, journal=%s, "
                "autograduate=%s)",
                state_dir / "colony-self-model.db",
                state_dir / "colony-action-journal.db",
                os.environ.get("COLONY_TRUST_AUTOGRADUATE", "true"))
        else:
            logger.info("SelfModel disabled (COLONY_SELF_MODEL_ENABLED=false)")
    except Exception as exc:
        logger.warning("SelfModel init failed: %s", exc)

    # --- Selfhood benchmark (Mind M0a): falsifiable weekly metrics ---
    try:
        from colony_sidecar.self_model.benchmark import (
            BenchmarkStore, SelfhoodBenchmark, benchmark_enabled,
        )
        from colony_sidecar.api.routers.host import set_benchmark
        if benchmark_enabled():
            _bench = SelfhoodBenchmark(BenchmarkStore(
                db_path=str(state_dir / "colony-benchmark.db")))
            set_benchmark(_bench)
            logger.info("Selfhood benchmark ready (db=%s)",
                        state_dir / "colony-benchmark.db")
        else:
            logger.info(
                "Selfhood benchmark disabled (COLONY_BENCHMARK_ENABLED=false)")
    except Exception as exc:
        logger.warning("Benchmark init failed: %s", exc)

    # --- Experiment framework (Mind M0b): bounded, guarded self-changes ---
    try:
        from colony_sidecar.self_model.experiments import (
            ExperimentEngine, ExperimentStore, experiments_enabled,
        )
        from colony_sidecar.api.routers.host import set_experiments
        if experiments_enabled():
            set_experiments(ExperimentEngine(ExperimentStore(
                db_path=str(state_dir / "colony-experiments.db"))))
            logger.info("Experiment framework ready (db=%s)",
                        state_dir / "colony-experiments.db")
        else:
            logger.info("Experiment framework disabled "
                        "(COLONY_EXPERIMENTS_ENABLED=false)")
    except Exception as exc:
        logger.warning("Experiment framework init failed: %s", exc)

    # --- Toolsmith (Mind M1): self-built, sandbox-verified tools ---
    try:
        from colony_sidecar.toolsmith import (
            Toolsmith, ToolRegistry, toolsmith_enabled,
        )
        from colony_sidecar.api.routers.host import set_toolsmith
        if toolsmith_enabled():
            _tool_registry = ToolRegistry(
                db_path=str(state_dir / "colony-toolsmith.db"),
                library_root=str(state_dir / "toolsmith_library"))
            _toolsmith = Toolsmith(_tool_registry)
            set_toolsmith(_toolsmith)
            # advertise graduated tools to the reasoning loop
            try:
                from colony_sidecar.api.routers.host import _tool_executor
                if _tool_executor is not None:
                    _tool_executor.set_dynamic_provider(
                        _toolsmith.build_dynamic_provider())
            except Exception as texc:
                logger.warning("toolsmith dynamic provider wiring: %s", texc)
            logger.info("Toolsmith ready (mode=%s, db=%s)",
                        os.environ.get("COLONY_TOOLSMITH", "off"),
                        state_dir / "colony-toolsmith.db")
        else:
            logger.info("Toolsmith disabled (COLONY_TOOLSMITH=off)")
    except Exception as exc:
        logger.warning("Toolsmith init failed: %s", exc)

    # --- Expectation engine (Mind M3a): predictions + surprise + calibration ---
    try:
        from colony_sidecar.self_model.expectations import (
            ExpectationEngine, ExpectationStore, expectations_enabled,
        )
        from colony_sidecar.api.routers.host import set_expectations
        if expectations_enabled():
            _exp_store = ExpectationStore(
                db_path=str(state_dir / "colony-expectations.db"))
            _exp_journal = None
            try:
                from colony_sidecar.api.routers.host import _self_model as _sm_e
                _exp_journal = getattr(_sm_e, "journal", None)
            except Exception:
                _exp_journal = None
            # workspace wired later; set_expectations stores the engine and the
            # autonomy phase links the workspace ref at runtime.
            _expectations = ExpectationEngine(_exp_store, journal=_exp_journal)
            set_expectations(_expectations)
            logger.info("Expectation engine ready (mode=%s, db=%s)",
                        os.environ.get("COLONY_EXPECTATIONS", "off"),
                        state_dir / "colony-expectations.db")
        else:
            logger.info("Expectation engine disabled (COLONY_EXPECTATIONS=off)")
    except Exception as exc:
        logger.warning("Expectation engine init failed: %s", exc)

    # --- Cognitive workspace (Mind M2): continuity of thought ---
    try:
        from colony_sidecar.self_model.workspace import (
            ConcernStore, WorkspaceEngine, workspace_enabled, workspace_mode,
        )
        from colony_sidecar.self_model.thinker import build_thinker
        from colony_sidecar.api.routers.host import set_workspace
        if workspace_enabled():
            _concern_store = ConcernStore(
                db_path=str(state_dir / "colony-workspace.db"))
            _thinker = (build_thinker(llm_router, graph=graph)
                        if llm_router is not None else None)
            _ws_journal = None
            try:
                from colony_sidecar.api.routers.host import _self_model as _sm_ws
                _ws_journal = getattr(_sm_ws, "journal", None)
            except Exception:
                _ws_journal = None
            _workspace = WorkspaceEngine(
                _concern_store, thinker=_thinker, journal=_ws_journal)
            set_workspace(_workspace)
            logger.info("Cognitive workspace ready (mode=%s, db=%s)",
                        workspace_mode(), state_dir / "colony-workspace.db")
        else:
            logger.info("Cognitive workspace disabled (COLONY_WORKSPACE=off)")
    except Exception as exc:
        logger.warning("Workspace init failed: %s", exc)

    # --- Skills memory (procedure memory, item 3) ---
    _skills_mem_store = None
    try:
        from colony_sidecar.skills_memory import SkillStore, skills_distill_mode
        from colony_sidecar.api.routers.host import set_skill_store
        _skills_mem_store = SkillStore(
            db_path=str(state_dir / "colony-skills.db"))
        set_skill_store(_skills_mem_store)
        logger.info("SkillStore initialized (db=%s, %d skill(s), distill=%s)",
                    state_dir / "colony-skills.db",
                    _skills_mem_store.count(), skills_distill_mode())
    except Exception as exc:
        logger.warning("SkillStore init failed: %s", exc)

    # --- Mining: escalation miner + verbatim turn capture (corpus source) ---
    try:
        from colony_sidecar.mining import EscalationMiner, MiningStore, mining_mode
        from colony_sidecar.api.routers.mining import set_mining

        if mining_mode() != "off":
            _mining_store_obj = MiningStore(
                db_path=str(state_dir / "colony-mining.db"))

            def _mining_router_getter():
                try:
                    from colony_sidecar.api.routers.host import _reasoning_loop
                    return getattr(_reasoning_loop, "_model", None)
                except Exception:
                    return None

            _mining_engine_obj = EscalationMiner(
                _mining_store_obj,
                skill_store=_skills_mem_store,
                router_getter=_mining_router_getter,
            )
            set_mining(_mining_store_obj, _mining_engine_obj, state_dir)
            logger.info("EscalationMiner initialized (db=%s, mode=%s)",
                        state_dir / "colony-mining.db", mining_mode())
        else:
            logger.info("Mining disabled (COLONY_ESCALATION_MINING=off)")
    except Exception as exc:
        logger.warning("Mining init failed: %s", exc)

    # --- Read-only repo mirrors + directed action (option A) ---
    try:
        from colony_sidecar.repos import RepoMirrorManager
        from colony_sidecar.directed import (
            DirectedActionService, ScopedTaskStore, directed_mode,
        )
        from colony_sidecar.api.routers.host import (
            set_repo_mirrors, set_directed_service,
            get_directive_manager as _get_dm2,
        )
        _mirrors_mgr = RepoMirrorManager(
            mirror_dir=str(state_dir / "repo-mirrors"),
            directive_manager=_get_dm2(),
        )
        set_repo_mirrors(_mirrors_mgr)
        _n_repos = len(_mirrors_mgr.configured())
        if _n_repos:
            # Clone/pull in the background so boot is not blocked by network.
            async def _sync_mirrors():
                try:
                    loop_ = asyncio.get_event_loop()
                    results = await loop_.run_in_executor(None, _mirrors_mgr.refresh_all)
                    logger.info(
                        "Repo mirrors synced: %s",
                        {k: v.get("action") or v.get("reason") for k, v in results.items()},
                    )
                    from colony_sidecar.api.routers.host import _world_store as _ws
                    n = await _mirrors_mgr.register_entities(_ws)
                    if n:
                        logger.info("Registered %d repo(s) as Project entities", n)
                except Exception:
                    logger.debug("mirror sync failed", exc_info=True)
            asyncio.create_task(_sync_mirrors())
        logger.info("RepoMirrorManager initialized (%d repo(s) configured)", _n_repos)

        async def _directed_deliver(payload: dict) -> bool:
            # Late-bound: route through the autonomy loop's guarded delivery
            # (boundary + sanitize + rate + shadow), same as every reach-out.
            try:
                from colony_sidecar.api.routers.host import _autonomy_loop, _delivery_bridge
                if _autonomy_loop is not None and _delivery_bridge is not None:
                    return await _autonomy_loop._route_reachout_delivery(
                        payload, _delivery_bridge)
            except Exception:
                logger.debug("directed deliver failed", exc_info=True)
            return False

        from colony_sidecar.api.routers.host import _feedback_store as _fb_store
        _directed_svc = DirectedActionService(
            store=ScopedTaskStore(db_path=str(state_dir / "colony-directed.db")),
            directive_manager=_get_dm2(),
            mirrors=_mirrors_mgr,
            feedback_store=_fb_store,
            delivery_router=_directed_deliver,
            self_model=_sm_for_directed,
        )
        set_directed_service(_directed_svc)
        logger.info("DirectedActionService initialized (mode=%s)", directed_mode())

        # The once-per-boundary critical flag rides the same guarded delivery.
        _dm_for_flags = _get_dm2()
        if _dm_for_flags is not None:
            _dm_for_flags.set_delivery_router(_directed_deliver)
    except Exception as exc:
        logger.warning("Directed-action init failed: %s", exc)

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

    # --- 7e. Channel Registration Store ---
    channel_store = None
    try:
        from colony_sidecar.channels.store import ChannelStore
        from colony_sidecar.channels.router import set_channel_store
        from colony_sidecar.channels.phone_gateways import set_channel_store_ref

        channels_db = os.path.join(state_dir, "colony-channels.db")
        channel_store = ChannelStore(db_path=channels_db)
        channel_store.connect()
        set_channel_store(channel_store)
        set_channel_store_ref(channel_store)
        from colony_sidecar.api.routers.host import set_channel_store as _host_set_channel_store
        _host_set_channel_store(channel_store)   # turn traffic auto-registers + touches channels
        logger.info("ChannelStore initialized (db=%s)", channels_db)
    except Exception as exc:
        logger.warning("ChannelStore init failed: %s", exc)

    # --- 8. Contacts ---
    contacts_store = None
    try:
        from colony_sidecar.contacts.config import ContactsConfig
        from colony_sidecar.contacts.store import SQLiteContactStore
        contacts_config = ContactsConfig.from_env()
        contacts_store = SQLiteContactStore(config=contacts_config, graph=graph)
        await contacts_store.connect()
        set_contacts_store(contacts_store)
        logger.info("ContactsStore initialized (path=%s)", contacts_config.sqlite_path)
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

    # --- 8d. Relationship profiler (standing + psyche + approach briefs) ---
    if contacts_store is not None:
        try:
            from colony_sidecar.intelligence.relationships.profiler import (
                RelationshipProfiler,
            )
            import colony_sidecar.api.routers.host as _host_mod
            _rel_profiler = RelationshipProfiler(
                contacts_store=contacts_store,
                comms_log=_host_mod._comms_log,
                affect_store=_host_mod._affect_store,
                facts_store=_host_mod._facts_store,
                engagement_store=_host_mod._engagement_store,
                db_path=str(state_dir / "colony-relationships.db"),
            )
            _host_mod.set_relationship_profiler(_rel_profiler)
            logger.info("RelationshipProfiler initialized (db=%s)",
                        state_dir / "colony-relationships.db")
        except Exception as exc:
            logger.warning("RelationshipProfiler init failed: %s", exc)

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

        # World-model population from conversation (shadow-first). Boundary-checked
        # via the directive manager. Mode from COLONY_WORLD_POPULATE_MODE
        # (off|shadow|live, default shadow).
        try:
            from colony_sidecar.world_model.populator import WorldModelPopulator, populate_mode
            from colony_sidecar.api.routers.host import (
                get_directive_manager as _get_dm, set_world_populator,
            )
            _populator = WorldModelPopulator(world_store, directive_manager=_get_dm())
            set_world_populator(_populator)
            logger.info("WorldModelPopulator initialized (mode=%s)", populate_mode())
        except Exception as pexc:
            logger.warning("WorldModelPopulator init failed: %s", pexc)

        # LLM-assisted world-model extraction (batch, journaled; daily phase).
        try:
            from colony_sidecar.world_model.llm_extract import (
                WorldLLMExtractor, llm_extract_mode,
            )
            from colony_sidecar.api.routers.host import (
                get_directive_manager as _get_dm3, set_world_llm_extractor,
            )
            _wle = WorldLLMExtractor(
                world_store, graph=graph, directive_manager=_get_dm3(),
                journal=getattr(_sm_for_directed, "journal", None))
            set_world_llm_extractor(_wle)
            logger.info("WorldLLMExtractor initialized (mode=%s)",
                        llm_extract_mode())
        except Exception as wexc:
            logger.warning("WorldLLMExtractor init failed: %s", wexc)

        # Belief maintenance (item 7): contradiction detection, resolution,
        # stale decay + the inline property-supersession audit hook.
        try:
            from colony_sidecar.beliefs import BeliefEngine, BeliefStore, beliefs_mode
            from colony_sidecar.api.routers.host import set_belief_engine
            from colony_sidecar.world_model.store import set_property_audit_hook
            _belief_store = BeliefStore(
                db_path=str(state_dir / "colony-beliefs.db"))
            _belief_eng = BeliefEngine(
                _belief_store, world_store=world_store, graph=graph,
                initiative_store=None,  # attached below once wired
                journal=getattr(_sm_for_directed, "journal", None),
                self_model=_sm_for_directed)
            set_belief_engine(_belief_eng)
            set_property_audit_hook(_belief_eng.note_property_update)
            logger.info("BeliefEngine initialized (db=%s, mode=%s)",
                        state_dir / "colony-beliefs.db", beliefs_mode())
        except Exception as bexc:
            logger.warning("BeliefEngine init failed: %s", bexc)

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
                params=_adaptive_params,
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
        from colony_sidecar.delivery.channels import ChannelRegistry
        channel_registry = ChannelRegistry.load(contacts_store=contacts_store, channel_store=channel_store)
        delivery = ProactiveDeliveryBridge(channel_registry=channel_registry)
        set_delivery_bridge(delivery)
        logger.info("Delivery bridge initialized")

        # Adaptive daily cap (Amendment 1.6): the trust engine can EARN the
        # per-recipient cap upward with a proven delivery track record.
        try:
            _trust_for_cap = getattr(_sm_for_directed, "trust", None)
            if (_trust_for_cap is not None
                    and getattr(delivery, "_rate_limiter", None) is not None):
                delivery._rate_limiter._cap_provider = _trust_for_cap.delivery_cap
                logger.info("Delivery rate cap wired to trust engine "
                            "(adaptive, max=%s)",
                            os.environ.get("COLONY_TRUST_DELIVERY_CAP_MAX", "6"))
        except Exception:
            logger.debug("adaptive cap wiring failed", exc_info=True)

        # --- 13b. Briefings: full wiring (delivery + persistence + schedule) ---
        # The bare engine from section 9 can compose briefings but has no gateway, no
        # persistent store, and no scheduler -- proactive output never reaches anyone.
        # Rebuild it here, now that the delivery bridge exists: the bridge-backed
        # gateway auto-registers when a home channel is configured, the store persists
        # under COLONY_STATE_DIR, and the scheduler fires daily/weekly briefings and
        # drains pending deliveries. All deployment specifics come from env.
        try:
            from pathlib import Path as _P

            from colony_sidecar.briefings.config import BriefingConfig
            from colony_sidecar.briefings.engine import BriefingEngine
            from colony_sidecar.briefings.scheduler import BriefingScheduler
            from colony_sidecar.briefings.store import BriefingStore

            b_cfg = BriefingConfig()
            b_cfg.daily.time = os.environ.get("COLONY_BRIEFING_DAILY_TIME", b_cfg.daily.time)
            b_cfg.daily.timezone = os.environ.get("COLONY_BRIEFING_TZ", b_cfg.daily.timezone)
            b_cfg.weekly.timezone = b_cfg.daily.timezone
            b_cfg.delivery_gateway = os.environ.get("COLONY_BRIEFING_GATEWAY", "whatsapp")
            b_cfg.lm_enhancement_enabled = (
                os.environ.get("COLONY_BRIEFING_LM_ENHANCE", "0") not in ("0", "false", "no")
            )
            _b_state_dir = os.environ.get("COLONY_STATE_DIR", ".")
            b_store = BriefingStore(db_path=str(_P(_b_state_dir) / "briefings.db"))
            # Real aggregators where the backing subsystem exists — without
            # them the composer silently falls back to stubs and every data
            # section of every briefing is empty. Calendar/anomaly/mind/
            # synthesis still lack concrete aggregators (see docs/KNOWN-GAPS.md).
            _aggs = {}
            try:
                if graph is not None:
                    from colony_sidecar.briefings.aggregators import RelationshipAggregator
                    _aggs["relationship_aggregator"] = RelationshipAggregator(
                        scorer=None, graph=graph)
            except Exception:
                logger.debug("relationship aggregator wiring failed", exc_info=True)
            try:
                if goals_engine is not None:
                    from colony_sidecar.briefings.aggregators import GoalEngineAggregator
                    _aggs["goal_aggregator"] = GoalEngineAggregator(goals_engine)
            except Exception:
                logger.debug("goal aggregator wiring failed", exc_info=True)
            try:
                # Both resolve their subsystems lazily off host globals at
                # call time (the anomaly detector doesn't even exist until
                # the autonomy registry builds it, well after this point).
                from colony_sidecar.briefings.aggregators import (
                    AnomalyDetectorAggregator, DiscovererSynthesisAggregator)
                _aggs["anomaly_aggregator"] = AnomalyDetectorAggregator()
                _aggs["synthesis_aggregator"] = DiscovererSynthesisAggregator()
            except Exception:
                logger.debug("anomaly/synthesis aggregator wiring failed", exc_info=True)
            try:
                # resolves the enabled calendar connector instance(s) — base
                # or per-account — at call time; harmless when none enabled
                from colony_sidecar.briefings.aggregators import ConnectorCalendarAggregator
                _aggs["calendar_aggregator"] = ConnectorCalendarAggregator()
            except Exception:
                logger.debug("calendar aggregator wiring failed", exc_info=True)
            briefings = BriefingEngine(config=b_cfg, store=b_store,
                                       delivery_bridge=delivery, **_aggs)
            set_briefings_engine(briefings)
            if os.environ.get("COLONY_BRIEFINGS_SCHEDULE", "1") not in ("0", "false", "no"):
                b_sched = BriefingScheduler(config=b_cfg, engine=briefings, store=b_store)
                briefings.attach_scheduler(b_sched)
                b_sched.start()
            logger.info(
                "BriefingEngine rewired: gateway=%s daily=%s %s scheduler=on",
                b_cfg.delivery_gateway, b_cfg.daily.time, b_cfg.daily.timezone,
            )
        except Exception as exc:
            logger.warning("Briefing delivery wiring failed: %s", exc)
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
        skills_registry = SkillRegistry()
        set_skills_registry(skills_registry)
        logger.info("SkillRegistry initialized (%d skills)", len(skills_registry.list_skills()))

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
            from colony_sidecar.chain.node import get_or_create_node_id, ensure_node_keypair, create_node_certificate
            node_id = get_or_create_node_id(state_dir)
            node_km = ensure_node_keypair(state_dir)
            logger.info("Node identity: %s (public_key=%s...)", node_id, node_km.public_key_hex()[:16])

            # Create node certificate if missing
            cert_path = Path(state_dir) / "node-cert.json"
            if not cert_path.exists():
                create_node_certificate(state_dir, colony_key_manager=key_mgr)
                logger.info("Node certificate created and signed by Colony key")
            else:
                logger.info("Node certificate exists")
        except Exception as nexc:
            logger.warning("Node identity init skipped: %s", nexc)

        # The LOCAL identity anchor (colony_id, node keypair, signed node
        # cert) is supported. The REMOTE multi-agent surface (agent connect,
        # cert-chain verification, block/consensus) is EXPERIMENTAL: no
        # consensus loop runs and the remote handshake is not production
        # verified. See docs/MULTI_AGENT.md.
        logger.info("Chain: local identity anchor ready; remote multi-agent "
                    "+ consensus are EXPERIMENTAL (no consensus loop started)")
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

        # Observation store (v0.16.0) — agent-reported domain snapshots
        try:
            from colony_sidecar.observations.store import ObservationStore
            from colony_sidecar.api.routers.observations import set_observation_store
            observation_store = ObservationStore(state_dir=state_dir)
            set_observation_store(observation_store)
            logger.info("ObservationStore initialized (state_dir=%s)", state_dir)
        except Exception as exc:
            logger.warning("ObservationStore init failed (non-fatal): %s", exc)

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

        # Register default periodic tasks. Every registered task does REAL
        # work or reports skipped — a no-op lambda returning {"status":"ok"}
        # makes the loop count a subsystem as running when it never does
        # (signal ingest happens inline at the API; briefings are fired by
        # the BriefingScheduler wired in section 13b — neither needs a task
        # here, so neither gets a fake one).
        def _run_health_check():
            wired = 0
            try:
                import colony_sidecar.api.routers.host as _h
                for _n in ("_commitment_store", "_goals_store", "_affect_store",
                           "_contacts_store", "_delivery_bridge", "_workspace",
                           "_metalearner"):
                    if getattr(_h, _n, None) is not None:
                        wired += 1
            except Exception:
                pass
            return {"status": "ok", "subsystems_wired": wired,
                    "autonomy_running": bool(getattr(autonomy_loop, "_running", False))}

        scheduler.register("health_check", _run_health_check, interval_seconds=300, metadata={"description": "Subsystem health check (reports wired count)"})

        async def _run_memory_consolidate():
            from colony_sidecar.api.routers.host import _consolidator as c
            if c is None:
                return {"status": "skipped", "reason": "consolidator_not_wired"}
            result = await c.run()
            # ConsolidationResult exposes pairs_merged, not merged_count — the
            # old attr name silently reported merged:0 every run.
            return {"status": "ok", "merged": getattr(result, "pairs_merged", 0)}

        scheduler.register("memory_consolidate", _run_memory_consolidate, interval_seconds=3600, metadata={"description": "Deduplicate and merge near-duplicate memories"})

        async def _run_cpi_track():
            from colony_sidecar.api.routers.host import _metalearner as ml
            if ml is None:
                return {"status": "skipped", "reason": "metalearner_not_wired"}
            cpi = await ml.evaluate()
            return {"status": "ok", "overall": round(float(getattr(cpi, "overall", 0.0)), 4)}

        scheduler.register("cpi_track", _run_cpi_track, interval_seconds=86400, metadata={"description": "Calculate Cognitive Performance Index"})

        async def _run_world_model_prune():
            from colony_sidecar.api.routers.host import _world_store as ws
            if ws is None:
                return {"status": "skipped", "reason": "world_model_not_wired"}
            return await ws.prune()

        scheduler.register("world_model_prune", _run_world_model_prune, interval_seconds=86400, metadata={"description": "Remove stale low-confidence world model entities (config TTL)"})

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

    # Wire SubsystemRegistry into ToolExecutor so Colony-native tools
    # (memory_search, goals, relationships, etc.) are available to the
    # initiative executor's reasoning loop.
    if registry is not None and locals().get("tool_executor") is not None:
        te = locals()["tool_executor"]
        from colony_sidecar.tools.handlers import TOOL_HANDLERS as _colony_handlers
        for _tname, _thandler in _colony_handlers.items():
            if _tname not in te._handlers:
                te._handlers[_tname] = lambda args, h=_thandler, r=registry: h(args, r)
        logger.info(
            "Colony tool handlers wired into ToolExecutor (%d colony tools, %d total)",
            len(_colony_handlers),
            len(te._handlers),
        )

    # --- 22. Agent Bridge service (auto-wired initiative/job forwarding) ---
    try:
        from colony_sidecar.services.agent_bridge import create_from_env as _create_bridge
        _bridge_svc = _create_bridge(
            initiative_store=locals().get("initiative_store"),
            autonomy_loop=autonomy_loop,
            task_queue=task_queue,
            observation_store=locals().get("observation_store"),
        )
        if _bridge_svc is not None:
            set_agent_bridge(_bridge_svc)
            asyncio.create_task(_bridge_svc.start())
            logger.info("AgentBridgeService auto-start scheduled")
    except Exception as exc:
        logger.warning("AgentBridgeService init failed (non-fatal): %s", exc)

    # --- 22b. Project engine (goal persistence, cognition item 1) ---
    try:
        from colony_sidecar.projects import ProjectEngine, ProjectStore, projects_mode
        from colony_sidecar.api.routers.host import (
            set_project_engine,
            get_directive_manager as _get_dm_p,
            _directed_service as _dsvc_for_projects,
            _proposal_store as _pstore_for_projects,
            _feedback_store as _fb_for_projects,
        )

        async def _project_deliver(payload: dict) -> bool:
            try:
                from colony_sidecar.api.routers.host import (
                    _autonomy_loop, _delivery_bridge,
                )
                if _autonomy_loop is not None and _delivery_bridge is not None:
                    return await _autonomy_loop._route_reachout_delivery(
                        payload, _delivery_bridge)
            except Exception:
                logger.debug("project deliver failed", exc_info=True)
            return False

        _project_engine_obj = ProjectEngine(
            ProjectStore(db_path=str(state_dir / "colony-projects.db")),
            directive_manager=_get_dm_p(),
            llm_router=llm_router,
            reasoning_loop=locals().get("reasoning_loop"),
            tool_executor=locals().get("tool_executor"),
            directed_service=_dsvc_for_projects,
            proposal_store=_pstore_for_projects,
            feedback_store=_fb_for_projects,
            self_model=_sm_for_directed,
            skill_store=_skills_mem_store,
            delivery_router=_project_deliver,
            initiative_store=locals().get("initiative_store"),
        )
        set_project_engine(_project_engine_obj)
        logger.info("ProjectEngine initialized (db=%s, mode=%s)",
                    state_dir / "colony-projects.db", projects_mode())
        # Late-attach the initiative store to the belief engine (it is wired
        # after the world-model section where the engine was created).
        try:
            from colony_sidecar.api.routers.host import _belief_engine as _be
            if _be is not None:
                _be._initiatives = locals().get("initiative_store")
        except Exception:
            pass
    except Exception as exc:
        logger.warning("ProjectEngine init failed: %s", exc)

    # --- 22c. Worker governor (server-side queue enforcement, item 5) ---
    try:
        from colony_sidecar.task_queue.governor import WorkerGovernor, workers_mode
        from colony_sidecar.api.routers.host import (
            set_worker_governor,
            get_directive_manager as _get_dm_w,
            _feedback_store as _fb_for_workers,
        )

        async def _worker_deliver(payload: dict) -> bool:
            try:
                from colony_sidecar.api.routers.host import (
                    _autonomy_loop, _delivery_bridge,
                )
                if _autonomy_loop is not None and _delivery_bridge is not None:
                    return await _autonomy_loop._route_reachout_delivery(
                        payload, _delivery_bridge)
            except Exception:
                logger.debug("worker deliver failed", exc_info=True)
            return False

        _worker_gov = WorkerGovernor(
            directive_manager=_get_dm_w(),
            feedback_store=_fb_for_workers,
            self_model=_sm_for_directed,
            delivery_router=_worker_deliver,
            skill_store=_skills_mem_store,
            llm_router=llm_router,
        )
        set_worker_governor(_worker_gov)
        logger.info("WorkerGovernor initialized (mode=%s)", workers_mode())
    except Exception as exc:
        logger.warning("WorkerGovernor init failed: %s", exc)

    # --- 22d. Exploration sandbox (gated isolated execution, item 6) ---
    try:
        from colony_sidecar.sandbox import SandboxManager, sandbox_mode
        from colony_sidecar.api.routers.host import (
            set_sandbox, get_directive_manager as _get_dm_sb,
        )
        _sandbox_mgr = SandboxManager(
            directive_manager=_get_dm_sb(),
            self_model=_sm_for_directed,
        )
        set_sandbox(_sandbox_mgr)
        logger.info("SandboxManager initialized (mode=%s, backend=%s)",
                    sandbox_mode(), _sandbox_mgr.backend_name())
    except Exception as exc:
        logger.warning("SandboxManager init failed: %s", exc)

    # --- 22e. Connector framework (read-only pull senses, item 2) ---
    try:
        from colony_sidecar.connectors import (
            ConnectorManager, connectors_mode,
        )
        from colony_sidecar.api.routers.host import (
            set_connector_manager, get_directive_manager as _get_dm_c,
            _world_populator as _pop_for_conn,
        )
        _conn_mgr = ConnectorManager(
            observation_store=locals().get("observation_store"),
            populator=_pop_for_conn,
            directive_manager=_get_dm_c(),
            self_model=_sm_for_directed,
        )
        n_conn = _conn_mgr.register_default_connectors()
        set_connector_manager(_conn_mgr)
        logger.info("ConnectorManager initialized (mode=%s, %d connector(s) enabled)",
                    connectors_mode(), n_conn)
    except Exception as exc:
        logger.warning("ConnectorManager init failed: %s", exc)

    # --- 23. Initiative Executor service (autonomous initiative processing) ---
    try:
        from colony_sidecar.services.initiative_executor import (
            create_from_env as _create_executor,
        )
        from colony_sidecar.api.routers.host import get_directive_manager as _get_dm
        _executor_svc = _create_executor(
            initiative_store=locals().get("initiative_store"),
            reasoning_loop=locals().get("reasoning_loop"),
            tool_executor=locals().get("tool_executor"),
            directive_manager=_get_dm(),
            skill_store=_skills_mem_store,
            self_model=_sm_for_directed,
        )
        if _executor_svc is not None:
            set_initiative_executor(_executor_svc)
            asyncio.create_task(_executor_svc.start())
            logger.info("InitiativeExecutorService auto-start scheduled")
    except Exception as exc:
        logger.warning("InitiativeExecutorService init failed (non-fatal): %s", exc)

    from colony_sidecar.telemetry import TelemetryStore
    telemetry = TelemetryStore()
    telemetry.load()  # restore last_*_at across restart (v0.21.0)
    telemetry.started_at = datetime.now(timezone.utc)
    app.state.telemetry = telemetry
    set_telemetry(telemetry)
    logger.info("TelemetryStore initialized")

    # Session report store (cross-session context bridge)
    from colony_sidecar.sessions.reports import SessionReportStore
    session_report_store = SessionReportStore()
    set_session_report_store(session_report_store)
    logger.info("SessionReportStore initialized")

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
                telemetry=telemetry,
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
            logger.debug("Graph close failed", exc_info=True)
    if world_store is not None:
        try:
            await world_store.close()
        except Exception:
            logger.debug("WorldStore close failed", exc_info=True)
    if skills_registry is not None:
        try:
            skills_registry.close()
        except Exception:
            logger.debug("SkillRegistry close failed", exc_info=True)
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
    set_context_provenance_store(None)
    set_response_guard(None)
    set_pattern_store(None)
    set_surprise_store(None)
    set_tom_extractor(None)
    if channel_store is not None:
        try:
            channel_store.close()
        except Exception:
            logger.debug("ChannelStore close failed", exc_info=True)
    try:
        from colony_sidecar.channels.router import set_channel_store as _set_ch_store
        _set_ch_store(None)
    except Exception:
        pass
    set_chain_manager(None)
    set_secrets_manager(None)
    set_session_store(None)
    set_session_report_store(None)
    set_agent_bridge(None)
    set_initiative_executor(None)
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
        logger.warning("Task queue shutdown failed")
    set_task_queue(None)
    # Stop autonomy loop if running
    try:
        from colony_sidecar.api.routers.host import _autonomy_loop
        if _autonomy_loop is not None and _autonomy_loop.is_running:
            await _autonomy_loop.stop()
    except Exception:
        logger.warning("Autonomy loop shutdown failed")
    set_autonomy_loop(None)
    set_session_store(None)
    set_task_queue(None)
    # Multi-Agent cleanup
    set_agent_store(None)
    set_invite_store(None)
    set_initiative_store(None)
    set_assignment_engine(None)
    set_websocket_manager(None)
    try:
        from colony_sidecar.api.routers.observations import set_observation_store
        set_observation_store(None)
    except Exception:
        pass
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

    # Channel registration router
    from colony_sidecar.channels.router import router as channels_router
    app.include_router(channels_router)

    # Task queue router (v0.13.0)
    from colony_sidecar.api.routers import task_queue as task_queue_router
    app.include_router(task_queue_router.router)

    # Observations router (v0.16.0) — agent-as-sensor ingestion
    from colony_sidecar.api.routers import observations as observations_router
    app.include_router(observations_router.router)
    from colony_sidecar.api.routers import mining as mining_router
    app.include_router(mining_router.router)

    # Context gate (v0.32.0) — budget-aware context preparation
    from colony_sidecar.api.routers import context_gate as context_gate_router
    app.include_router(context_gate_router.router)

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
