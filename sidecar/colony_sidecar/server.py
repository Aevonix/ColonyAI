"""Colony sidecar FastAPI server.

Standalone intelligence server that hosts (OpenClaw, future shims) mount
as a plugin via the ``/v1/host`` API surface.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from colony_sidecar.api.routers.host import router as host_router, set_reasoning_loop, supported_capabilities

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize subsystems on startup, tear down on shutdown."""
    # --- Reasoning loop ---
    reasoning_loop = None
    llm_router = None
    try:
        from colony_sidecar.router.router import LLMRouter
        llm_router = LLMRouter()
        logger.info("LLMRouter initialized")
    except Exception as exc:
        logger.warning("LLMRouter init failed — reasoning will not be available: %s", exc)

    if llm_router is not None:
        try:
            from colony_sidecar.reasoning import ReasoningLoop, ToolExecutor
            reasoning_tools = ToolExecutor()
            reasoning_loop = ReasoningLoop(model=llm_router, tools=reasoning_tools)
            set_reasoning_loop(reasoning_loop)
            logger.info("ReasoningLoop initialized (max_iterations=%d)", reasoning_loop._config.max_iterations)
        except Exception as exc:
            logger.warning("ReasoningLoop init failed — /v1/host/reasoning/turn returns 501: %s", exc)

    logger.info("Sidecar capabilities: %s", supported_capabilities())
    yield

    # Shutdown
    set_reasoning_loop(None)
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
