"""Context gate — decide per-call whether context fits a model whole,
or needs chunking + retrieval (RAG) / map-reduce summarization first.

Motivation: a model's *useful* context window (the range over which exact
retrieval stays reliable) is often far below its advertised maximum.
Stuffing a 200K document into such a model silently degrades recall.
The gate measures the input against a configurable per-model budget
(``TierConfig.useful_context_tokens`` or an explicit budget) and picks
one of three strategies:

- ``PASS_THROUGH``  — fits; send whole.
- ``RETRIEVE``      — query-focused task; chunk, rank chunks against the
                      query (embeddings when available, lexical TF-IDF
                      otherwise), and pack the best chunks into budget.
- ``MAP_REDUCE``    — holistic task (summarize/review the whole thing);
                      summarize chunks via a caller-provided LLM callable,
                      or fall back to evenly-spaced coverage sampling.

Everything is host-agnostic and dependency-free (pure stdlib); embedding
and summarization are injected as optional callables so any agent can use
it with whatever LLM/embedding stack it already has.

Usage::

    from colony_sidecar.contextgate import prepare_context

    prepared = await prepare_context(
        content=big_document,
        query="when did the outage start?",
        budget_tokens=65536,
    )
    messages = [{"role": "user", "content": prepared.text}]

Configuration (env, all optional):

- ``COLONY_CONTEXT_GATE``            — ``auto`` (default) | ``on`` | ``off``
- ``COLONY_CONTEXT_GATE_HEADROOM``   — fraction of budget that triggers
  gating (default 0.8)
- ``COLONY_CONTEXT_GATE_BUDGET``     — default token budget when neither
  the caller nor the model tier provides one (default 0 = no gating)
- ``COLONY_CONTEXT_CHUNK_TOKENS``    — target chunk size (default 1024)
- ``COLONY_CONTEXT_OVERLAP_TOKENS``  — chunk overlap (default 128)
- ``COLONY_CONTEXT_CHARS_PER_TOKEN`` — token estimator ratio (default 4.0)
"""

from colony_sidecar.contextgate.estimate import estimate_tokens
from colony_sidecar.contextgate.chunker import Chunk, chunk_text
from colony_sidecar.contextgate.retrieve import rank_chunks
from colony_sidecar.contextgate.gate import (
    GateConfig,
    GateDecision,
    PreparedContext,
    classify_task,
    decide,
    prepare_context,
)

__all__ = [
    "Chunk",
    "GateConfig",
    "GateDecision",
    "PreparedContext",
    "chunk_text",
    "classify_task",
    "decide",
    "estimate_tokens",
    "prepare_context",
    "rank_chunks",
]
