"""Gate decision + context preparation service.

``decide()`` picks a strategy from (estimated size, budget, task kind);
``prepare_context()`` executes it and returns the final context text plus
metadata. See the package docstring for the full model.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

from colony_sidecar.contextgate.chunker import Chunk, chunk_text
from colony_sidecar.contextgate.estimate import estimate_tokens
from colony_sidecar.contextgate.retrieve import EmbedFn, rank_chunks

logger = logging.getLogger(__name__)

__all__ = [
    "GateConfig",
    "GateDecision",
    "PreparedContext",
    "classify_task",
    "decide",
    "prepare_context",
]

SummarizeFn = Callable[[str, str], Awaitable[str]]  # (chunk_text, query) -> summary


class GateDecision(str, Enum):
    PASS_THROUGH = "pass_through"
    RETRIEVE = "retrieve"
    MAP_REDUCE = "map_reduce"


@dataclass
class GateConfig:
    """Tunables for the context gate. All values env-overridable."""

    mode: str = "auto"                 # auto | on | off
    headroom: float = 0.8              # gate when est > budget * headroom
    default_budget_tokens: int = 0     # used when caller/tier give none (0 = don't gate)
    chunk_tokens: int = 1024
    overlap_tokens: int = 128
    min_score: float = 0.05            # drop retrieval chunks scoring below this

    @classmethod
    def from_env(cls) -> "GateConfig":
        cfg = cls()
        mode = os.environ.get("COLONY_CONTEXT_GATE", "").strip().lower()
        if mode in ("auto", "on", "off"):
            cfg.mode = mode
        for attr, env, cast in (
            ("headroom", "COLONY_CONTEXT_GATE_HEADROOM", float),
            ("default_budget_tokens", "COLONY_CONTEXT_GATE_BUDGET", int),
            ("chunk_tokens", "COLONY_CONTEXT_CHUNK_TOKENS", int),
            ("overlap_tokens", "COLONY_CONTEXT_OVERLAP_TOKENS", int),
        ):
            raw = os.environ.get(env, "")
            if raw:
                try:
                    setattr(cfg, attr, cast(raw))
                except ValueError:
                    logger.warning("Invalid %s=%r — using default", env, raw)
        return cfg


@dataclass
class PreparedContext:
    """Result of ``prepare_context``."""

    text: str
    decision: GateDecision
    est_tokens_in: int
    est_tokens_out: int
    budget_tokens: int
    chunks_total: int = 0
    chunks_used: int = 0
    coverage: float = 1.0   # fraction of source chars represented in output
    task_kind: str = ""


# ---------------------------------------------------------------------------
# Task classification — the smarter-than-size signal
# ---------------------------------------------------------------------------

_HOLISTIC_RE = re.compile(
    r"\b(summar[iy][sz]e|overview|review|rewrite|rephrase|translate|proofread|"
    r"critique|tl;?dr|digest|condense|outline|abstract)\b",
    re.IGNORECASE,
)
_RETRIEVAL_RE = re.compile(
    r"(\?|\b(what|when|where|who|whom|whose|which|why|how|find|look ?up|search|"
    r"locate|extract|quote|list all|did|does|do|is|are|was|were)\b)",
    re.IGNORECASE,
)


def classify_task(query: str) -> str:
    """Classify the caller's intent as ``retrieval`` or ``holistic``.

    Retrieval tasks (needle questions, lookups) benefit from ranked-chunk
    RAG; holistic tasks (summarize/review the whole document) need
    coverage of everything and get map-reduce instead. Callers that know
    their intent should pass ``task_kind`` explicitly — this heuristic is
    only the fallback.
    """
    q = (query or "").strip()
    if not q:
        return "holistic"
    if _HOLISTIC_RE.search(q):
        return "holistic"
    if _RETRIEVAL_RE.search(q):
        return "retrieval"
    # A short, specific query usually names the thing to find.
    return "retrieval" if len(q) < 200 else "holistic"


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

def decide(
    est_tokens: int,
    budget_tokens: int,
    query: str = "",
    task_kind: Optional[str] = None,
    config: Optional[GateConfig] = None,
) -> GateDecision:
    """Pick a strategy for content of *est_tokens* against *budget_tokens*.

    ``mode=off`` or an unknown budget (0) always passes through; otherwise
    content within ``budget * headroom`` passes through, and oversized
    content is routed to RETRIEVE or MAP_REDUCE by task kind.
    """
    cfg = config or GateConfig.from_env()
    if cfg.mode == "off":
        return GateDecision.PASS_THROUGH
    budget = budget_tokens or cfg.default_budget_tokens
    if budget <= 0:
        return GateDecision.PASS_THROUGH
    if est_tokens <= budget * cfg.headroom:
        return GateDecision.PASS_THROUGH
    kind = task_kind if task_kind in ("retrieval", "holistic") else classify_task(query)
    return GateDecision.RETRIEVE if kind == "retrieval" else GateDecision.MAP_REDUCE


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------

def _assemble(
    selected: list[Chunk],
    total: int,
    source_chars: int,
    label: str,
) -> tuple[str, float]:
    """Join *selected* chunks (document order) with provenance markers."""
    selected = sorted(selected, key=lambda c: c.index)
    covered = sum(c.end - c.start for c in selected)
    coverage = min(1.0, covered / source_chars) if source_chars else 1.0
    parts = [
        f"[context gate: {label} — {len(selected)} of {total} chunks, "
        f"~{coverage:.0%} of the source shown]"
    ]
    for c in selected:
        parts.append(f"--- chunk {c.index + 1}/{total} (chars {c.start}-{c.end}) ---")
        parts.append(c.text)
    return "\n".join(parts), coverage


def _pack_to_budget(
    ranked: list[tuple[Chunk, float]],
    budget_tokens: int,
    min_score: float,
) -> list[Chunk]:
    selected: list[Chunk] = []
    used = 0
    for chunk, score in ranked:
        if score < min_score and selected:
            break
        if used + chunk.tokens > budget_tokens:
            if not selected:
                selected.append(chunk)  # always include at least the best chunk
            break
        selected.append(chunk)
        used += chunk.tokens
    return selected


def _sample_evenly(chunks: list[Chunk], budget_tokens: int) -> list[Chunk]:
    """Pick evenly-spaced chunks so the selection spans the whole source."""
    if not chunks:
        return []
    avg = max(1, sum(c.tokens for c in chunks) // len(chunks))
    k = max(1, min(len(chunks), budget_tokens // avg))
    if k >= len(chunks):
        return list(chunks)
    step = len(chunks) / k
    picked = []
    used = 0
    for i in range(k):
        c = chunks[int(i * step)]
        if used + c.tokens > budget_tokens and picked:
            break
        picked.append(c)
        used += c.tokens
    return picked


async def prepare_context(
    content: str,
    query: str = "",
    budget_tokens: int = 0,
    task_kind: Optional[str] = None,
    config: Optional[GateConfig] = None,
    embed_fn: Optional[EmbedFn] = None,
    summarize_fn: Optional[SummarizeFn] = None,
) -> PreparedContext:
    """Prepare *content* for a model call within *budget_tokens*.

    Returns the content unchanged when it fits (or gating is off).
    Otherwise chunks it and either retrieves the chunks most relevant to
    *query* (retrieval tasks) or map-reduces via *summarize_fn* /
    coverage-samples (holistic tasks). Never raises on ranking or
    summarization failure — degrades toward coverage sampling.
    """
    cfg = config or GateConfig.from_env()
    est = estimate_tokens(content)
    budget = budget_tokens or cfg.default_budget_tokens
    decision = decide(est, budget, query, task_kind, cfg)
    kind = task_kind if task_kind in ("retrieval", "holistic") else classify_task(query)

    if decision == GateDecision.PASS_THROUGH:
        return PreparedContext(
            text=content,
            decision=decision,
            est_tokens_in=est,
            est_tokens_out=est,
            budget_tokens=budget,
            task_kind=kind,
        )

    chunks = chunk_text(content, cfg.chunk_tokens, cfg.overlap_tokens)
    total = len(chunks)
    effective_budget = max(1, int(budget * cfg.headroom))

    if decision == GateDecision.RETRIEVE:
        ranked = await rank_chunks(chunks, query, embed_fn)
        selected = _pack_to_budget(ranked, effective_budget, cfg.min_score)
        text, coverage = _assemble(
            selected, total, len(content), "chunks selected for relevance to the query"
        )
    else:  # MAP_REDUCE
        if summarize_fn is not None:
            summaries: list[Chunk] = []
            for c in chunks:
                try:
                    s = await summarize_fn(c.text, query)
                except Exception:
                    logger.warning(
                        "summarize_fn failed on chunk %d — using head of chunk",
                        c.index, exc_info=True,
                    )
                    s = c.text[: cfg.chunk_tokens]
                summaries.append(
                    Chunk(
                        index=c.index,
                        text=s,
                        start=c.start,
                        end=c.end,
                        tokens=estimate_tokens(s),
                    )
                )
            # Keep summaries within budget (they normally fit; sample if not)
            if sum(s.tokens for s in summaries) > effective_budget:
                summaries = _sample_evenly(summaries, effective_budget)
            text, coverage = _assemble(
                summaries, total, len(content), "per-chunk summaries (map-reduce)"
            )
        else:
            selected = _sample_evenly(chunks, effective_budget)
            text, coverage = _assemble(
                selected, total, len(content), "evenly-spaced coverage sample"
            )

    prepared = PreparedContext(
        text=text,
        decision=decision,
        est_tokens_in=est,
        est_tokens_out=estimate_tokens(text),
        budget_tokens=budget,
        chunks_total=total,
        chunks_used=text.count("--- chunk "),
        coverage=coverage,
        task_kind=kind,
    )
    logger.info(
        "context gate: %s (%s) %d -> %d est tokens, %d/%d chunks, %.0f%% coverage",
        prepared.decision.value,
        prepared.task_kind,
        prepared.est_tokens_in,
        prepared.est_tokens_out,
        prepared.chunks_used,
        prepared.chunks_total,
        prepared.coverage * 100,
    )
    return prepared
