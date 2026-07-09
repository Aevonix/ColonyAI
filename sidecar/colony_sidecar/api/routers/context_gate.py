"""Context-gate API: budget-aware context preparation for any agent.

POST /v1/context/prepare — decide whether content fits a model's useful
context window whole, and if not, chunk + retrieve (query-focused) or
coverage-sample (holistic) down to budget. See
:mod:`colony_sidecar.contextgate` for the underlying machinery.

Auth rides the global ApiKeyMiddleware like every other router.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from colony_sidecar.contextgate import GateConfig, prepare_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/context", tags=["context"])


class DocumentIn(BaseModel):
    name: str = ""
    content: str


class PrepareRequest(BaseModel):
    content: Optional[str] = Field(
        default=None, description="Raw text to prepare (or use `documents`)."
    )
    documents: Optional[List[DocumentIn]] = Field(
        default=None, description="Named documents; concatenated with headers."
    )
    query: str = Field(
        default="", description="The task/question the context will serve."
    )
    budget_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Token budget. 0 = derive from `model_tier`'s usefulContextTokens, "
            "else the COLONY_CONTEXT_GATE_BUDGET default."
        ),
    )
    model_tier: str = Field(
        default="",
        description="small|medium|large — derive budget from this tier's config.",
    )
    task_kind: Optional[str] = Field(
        default=None, description="retrieval | holistic (overrides the heuristic)."
    )


class PrepareResponse(BaseModel):
    text: str
    decision: str
    task_kind: str
    est_tokens_in: int
    est_tokens_out: int
    budget_tokens: int
    chunks_total: int
    chunks_used: int
    coverage: float


def _tier_budget(tier_name: str) -> int:
    """Budget from the live LLMRouter's tier config (0 when unknown)."""
    try:
        from colony_sidecar.api.routers.host import get_llm_router
        from colony_sidecar.router.tiers import ModelTier

        llm_router = get_llm_router()
        if llm_router is None:
            return 0
        cfg = llm_router.tier_config(ModelTier(tier_name))
        return cfg.useful_context_tokens if cfg else 0
    except (ValueError, ImportError):
        return 0


@router.post("/prepare", response_model=PrepareResponse)
async def prepare(body: PrepareRequest) -> PrepareResponse:
    """Prepare content to fit a model's useful context window."""
    if body.content is None and not body.documents:
        raise HTTPException(status_code=422, detail="Provide `content` or `documents`.")

    if body.documents:
        parts = []
        for i, doc in enumerate(body.documents):
            name = doc.name or f"document {i + 1}"
            parts.append(f"# {name}\n\n{doc.content}")
        content = "\n\n".join(parts)
        if body.content:
            content = body.content + "\n\n" + content
    else:
        content = body.content or ""

    budget = body.budget_tokens
    if budget <= 0 and body.model_tier:
        budget = _tier_budget(body.model_tier.strip().lower())

    prepared = await prepare_context(
        content=content,
        query=body.query,
        budget_tokens=budget,
        task_kind=body.task_kind,
        config=GateConfig.from_env(),
    )
    return PrepareResponse(
        text=prepared.text,
        decision=prepared.decision.value,
        task_kind=prepared.task_kind,
        est_tokens_in=prepared.est_tokens_in,
        est_tokens_out=prepared.est_tokens_out,
        budget_tokens=prepared.budget_tokens,
        chunks_total=prepared.chunks_total,
        chunks_used=prepared.chunks_used,
        coverage=prepared.coverage,
    )
