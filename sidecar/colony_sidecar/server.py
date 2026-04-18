"""Colony sidecar FastAPI server.

Standalone intelligence server that hosts (OpenClaw, future shims) mount
as a plugin via the ``/v1/host`` API surface.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from colony_sidecar.api.routers.host import (
    router as host_router,
    set_reasoning_loop,
    set_graph,
    set_response_gate,
    set_signal_collector,
    set_embedder,
    supported_capabilities,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize subsystems on startup, tear down on shutdown."""
    # --- 1. LLM Router ---
    llm_router = None
    try:
        from colony_sidecar.router.router import LLMRouter
        llm_router = LLMRouter()
        logger.info("LLMRouter initialized")
    except Exception as exc:
        logger.warning("LLMRouter init failed — reasoning will not be available: %s", exc)

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
        collector = SignalCollector()
        set_signal_collector(collector)
        logger.info("SignalCollector initialized")
    except Exception as exc:
        logger.warning("SignalCollector init failed: %s", exc)

    # --- 6. Embedding pipeline ---
    try:
        from colony_sidecar.vector.embedder import EmbeddingPipeline
        from colony_sidecar.vector.config import EmbeddingConfig
        embed_config = EmbeddingConfig()
        pipeline = EmbeddingPipeline(embed_config)
        set_embedder(pipeline)
        logger.info("EmbeddingPipeline initialized (model=%s)", embed_config.model_name)
    except Exception as exc:
        logger.warning("EmbeddingPipeline init failed — memory/embed returns 501: %s", exc)

    logger.info("Sidecar capabilities: %s", supported_capabilities())
    yield

    # Shutdown — close connections
    if graph is not None:
        try:
            await graph.close()
        except Exception:
            pass
    set_reasoning_loop(None)
    set_graph(None)
    set_response_gate(None)
    set_signal_collector(None)
    set_embedder(None)
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
