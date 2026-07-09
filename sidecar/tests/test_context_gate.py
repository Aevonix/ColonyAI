"""Tests for the context gate — trigger logic, chunking, retrieval, API."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from colony_sidecar.contextgate import (
    GateConfig,
    GateDecision,
    chunk_text,
    classify_task,
    decide,
    estimate_tokens,
    prepare_context,
    rank_chunks,
)
from colony_sidecar.contextgate.retrieve import lexical_scores


# ---------------------------------------------------------------------------
# Estimator
# ---------------------------------------------------------------------------

def test_estimate_empty():
    assert estimate_tokens("") == 0


def test_estimate_prose_ratio():
    text = "The quick brown fox jumps over the lazy dog. " * 100
    est = estimate_tokens(text)
    # ~4 chars/token for prose
    assert abs(est - len(text) / 4) < len(text) / 20


def test_estimate_code_denser():
    code = '{"key": [1, 2, 3], "nested": {"a": 1, "b": [4, 5]}}\n' * 100
    prose = "a plain english sentence about nothing in particular here " * 90
    # Symbol-dense text should estimate more tokens per char than prose
    assert estimate_tokens(code) / len(code) > estimate_tokens(prose) / len(prose)


def test_estimate_env_override(monkeypatch):
    monkeypatch.setenv("COLONY_CONTEXT_CHARS_PER_TOKEN", "2.0")
    text = "hello world, this is plain prose without any symbols at all " * 10
    assert estimate_tokens(text) == pytest.approx(len(text) / 2, rel=0.01)


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------

def test_chunk_empty():
    assert chunk_text("   \n  ") == []


def test_chunk_small_single():
    chunks = chunk_text("just a short paragraph", target_tokens=100)
    assert len(chunks) == 1
    assert chunks[0].index == 0


def test_chunk_respects_target_size():
    paras = "\n\n".join(f"Paragraph {i}. " + ("word " * 60) for i in range(40))
    chunks = chunk_text(paras, target_tokens=200, overlap_tokens=0)
    assert len(chunks) > 3
    # No chunk wildly exceeds target (single blocks may slightly overshoot)
    assert all(c.tokens <= 400 for c in chunks)


def test_chunk_offsets_cover_source_in_order():
    paras = "\n\n".join(f"Paragraph {i}. " + ("word " * 60) for i in range(20))
    chunks = chunk_text(paras, target_tokens=150, overlap_tokens=0)
    for a, b in zip(chunks, chunks[1:]):
        assert a.end <= b.start + 2  # ordered, non-overlapping cores
    assert chunks[0].start == 0
    assert chunks[-1].end >= len(paras) - 2


def test_chunk_overlap_present():
    paras = "\n\n".join(f"Paragraph {i}. " + ("word " * 80) for i in range(10))
    with_overlap = chunk_text(paras, target_tokens=150, overlap_tokens=50)
    # Overlapped chunks carry a prefix from the previous chunk's tail
    assert len(with_overlap) >= 2
    assert with_overlap[1].text != paras[with_overlap[1].start:with_overlap[1].end]


def test_chunk_code_fence_atomic():
    doc = (
        "Intro paragraph.\n\n"
        "```python\n" + ("x = 1\n" * 30) + "```\n"
        "\nOutro paragraph."
    )
    chunks = chunk_text(doc, target_tokens=500, overlap_tokens=0)
    joined = "".join(c.text for c in chunks)
    # The fence contents survive intact in one chunk
    assert any("```python" in c.text and c.text.count("x = 1") == 30 for c in chunks)
    assert "Outro paragraph." in joined


# ---------------------------------------------------------------------------
# Task classification
# ---------------------------------------------------------------------------

def test_classify_empty_is_holistic():
    assert classify_task("") == "holistic"


def test_classify_question_is_retrieval():
    assert classify_task("When did the outage start?") == "retrieval"
    assert classify_task("find the section about billing") == "retrieval"


def test_classify_summarize_is_holistic():
    assert classify_task("Summarize this document") == "holistic"
    assert classify_task("please review the whole report") == "holistic"


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

def _cfg(**kw) -> GateConfig:
    base = dict(mode="auto", headroom=0.8, default_budget_tokens=0)
    base.update(kw)
    return GateConfig(**base)


def test_decide_under_budget_passes():
    assert decide(100, 1000, "what is x?", config=_cfg()) == GateDecision.PASS_THROUGH


def test_decide_no_budget_passes():
    assert decide(10**9, 0, "what is x?", config=_cfg()) == GateDecision.PASS_THROUGH


def test_decide_mode_off_passes():
    assert decide(10**9, 100, "what is x?", config=_cfg(mode="off")) == GateDecision.PASS_THROUGH


def test_decide_over_budget_retrieval():
    assert decide(1000, 1000, "when did it fail?", config=_cfg()) == GateDecision.RETRIEVE


def test_decide_over_budget_holistic():
    assert decide(1000, 1000, "summarize everything", config=_cfg()) == GateDecision.MAP_REDUCE


def test_decide_headroom_boundary():
    cfg = _cfg(headroom=0.8)
    assert decide(800, 1000, "q?", config=cfg) == GateDecision.PASS_THROUGH
    assert decide(801, 1000, "q?", config=cfg) == GateDecision.RETRIEVE


def test_decide_explicit_task_kind_wins():
    assert (
        decide(1000, 1000, "summarize it", task_kind="retrieval", config=_cfg())
        == GateDecision.RETRIEVE
    )


def test_decide_default_budget_from_config():
    cfg = _cfg(default_budget_tokens=500)
    assert decide(1000, 0, "q?", config=cfg) == GateDecision.RETRIEVE


def test_gateconfig_from_env(monkeypatch):
    monkeypatch.setenv("COLONY_CONTEXT_GATE", "off")
    monkeypatch.setenv("COLONY_CONTEXT_GATE_HEADROOM", "0.5")
    monkeypatch.setenv("COLONY_CONTEXT_GATE_BUDGET", "1234")
    cfg = GateConfig.from_env()
    assert cfg.mode == "off"
    assert cfg.headroom == 0.5
    assert cfg.default_budget_tokens == 1234


# ---------------------------------------------------------------------------
# Retrieval ranking
# ---------------------------------------------------------------------------

def _make_doc(needle_para: str, n: int = 30) -> str:
    paras = [f"Filler paragraph {i}. " + ("lorem ipsum dolor sit amet " * 12) for i in range(n)]
    paras[n // 2] = needle_para
    return "\n\n".join(paras)


def test_lexical_scores_find_needle():
    doc = _make_doc("The database outage started at 03:14 UTC because the disk filled up.")
    chunks = chunk_text(doc, target_tokens=120, overlap_tokens=0)
    scores = lexical_scores(chunks, "when did the database outage start?")
    best = max(range(len(chunks)), key=lambda i: scores[i])
    assert "03:14" in chunks[best].text


def test_rank_chunks_lexical_fallback_on_bad_embedder():
    doc = _make_doc("The secret launch code is PINEAPPLE-7.")
    chunks = chunk_text(doc, target_tokens=120, overlap_tokens=0)

    async def broken_embed(texts):
        raise RuntimeError("embedder down")

    ranked = asyncio.run(rank_chunks(chunks, "what is the secret launch code?", broken_embed))
    assert "PINEAPPLE-7" in ranked[0][0].text


def test_rank_chunks_uses_embedder():
    doc = _make_doc("needle sentence")
    chunks = chunk_text(doc, target_tokens=120, overlap_tokens=0)
    target = next(i for i, c in enumerate(chunks) if "needle" in c.text)

    async def embed(texts):
        # Query aligned with the target chunk only
        vecs = []
        for i, _t in enumerate(texts):
            if i == 0 or i - 1 == target:
                vecs.append([1.0, 0.0])
            else:
                vecs.append([0.0, 1.0])
        return vecs

    ranked = asyncio.run(rank_chunks(chunks, "anything", embed))
    assert ranked[0][0].index == target


# ---------------------------------------------------------------------------
# prepare_context end-to-end
# ---------------------------------------------------------------------------

def test_prepare_pass_through():
    out = asyncio.run(prepare_context("short text", "q?", budget_tokens=1000, config=_cfg()))
    assert out.decision == GateDecision.PASS_THROUGH
    assert out.text == "short text"
    assert out.coverage == 1.0


def test_prepare_retrieve_selects_relevant_and_fits_budget():
    doc = _make_doc("The database outage started at 03:14 UTC on March 5.", n=60)
    budget = 500
    out = asyncio.run(
        prepare_context(
            doc,
            "when did the database outage start?",
            budget_tokens=budget,
            config=_cfg(chunk_tokens=120, overlap_tokens=0),
        )
    )
    assert out.decision == GateDecision.RETRIEVE
    assert "03:14" in out.text
    assert out.est_tokens_out <= budget
    assert 0 < out.chunks_used < out.chunks_total
    assert out.coverage < 1.0


def test_prepare_map_reduce_sample_without_llm():
    doc = "\n\n".join(f"Section {i}. " + ("content words here " * 30) for i in range(50))
    out = asyncio.run(
        prepare_context(
            doc,
            "summarize the whole document",
            budget_tokens=400,
            config=_cfg(chunk_tokens=100, overlap_tokens=0),
        )
    )
    assert out.decision == GateDecision.MAP_REDUCE
    assert out.est_tokens_out <= 400
    # Coverage sampling spans the document: first and last thirds represented
    used = [int(s.split("/")[0]) for s in
            (line.split("chunk ")[1] for line in out.text.splitlines() if line.startswith("--- chunk"))]
    assert min(used) <= out.chunks_total // 3
    assert max(used) >= 2 * out.chunks_total // 3


def test_prepare_map_reduce_with_summarizer():
    doc = "\n\n".join(f"Section {i}. " + ("content words here " * 30) for i in range(20))

    calls = []

    async def summarize(text, query):
        calls.append(text)
        return "SUMMARY."

    out = asyncio.run(
        prepare_context(
            doc,
            "summarize the whole document",
            budget_tokens=400,
            config=_cfg(chunk_tokens=100, overlap_tokens=0),
            summarize_fn=summarize,
        )
    )
    assert out.decision == GateDecision.MAP_REDUCE
    assert len(calls) == out.chunks_total
    assert "SUMMARY." in out.text


def test_prepare_summarizer_failure_degrades():
    doc = "\n\n".join(f"Section {i}. " + ("content words here " * 30) for i in range(10))

    async def broken(text, query):
        raise RuntimeError("llm down")

    out = asyncio.run(
        prepare_context(
            doc,
            "summarize it",
            budget_tokens=400,
            config=_cfg(chunk_tokens=100, overlap_tokens=0),
            summarize_fn=broken,
        )
    )
    assert out.decision == GateDecision.MAP_REDUCE
    assert out.text  # degraded but produced output


# ---------------------------------------------------------------------------
# API route
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    from colony_sidecar.api.routers.context_gate import router as cg_router

    app = FastAPI()
    app.include_router(cg_router)
    return TestClient(app)


def test_api_prepare_pass_through(client):
    r = client.post(
        "/v1/context/prepare",
        json={"content": "tiny", "query": "q?", "budget_tokens": 1000},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "pass_through"
    assert body["text"] == "tiny"


def test_api_prepare_retrieve(client):
    doc = _make_doc("The invoice number is INV-42.", n=60)
    r = client.post(
        "/v1/context/prepare",
        json={"content": doc, "query": "what is the invoice number?", "budget_tokens": 400},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "retrieve"
    assert "INV-42" in body["text"]


def test_api_prepare_documents_form(client):
    r = client.post(
        "/v1/context/prepare",
        json={
            "documents": [{"name": "a.md", "content": "alpha"}, {"content": "beta"}],
            "query": "q?",
            "budget_tokens": 1000,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "# a.md" in body["text"] and "beta" in body["text"]


def test_api_requires_content(client):
    r = client.post("/v1/context/prepare", json={"query": "q?"})
    assert r.status_code == 422


def test_api_model_tier_budget(client, monkeypatch):
    from colony_sidecar.api.routers import host as host_mod
    from colony_sidecar.router.router import LLMRouter
    from colony_sidecar.router.tiers import build_tiers_from_host

    tiers = build_tiers_from_host(
        {
            "provider": "vllm",
            "baseUrl": "http://x/v1",
            "models": {
                "small": "s",
                "medium": "m",
                "large": {"model": "l", "usefulContextTokens": 300},
            },
        }
    )
    monkeypatch.setattr(host_mod, "_llm_router", LLMRouter(tiers=tiers))

    doc = _make_doc("The invoice number is INV-42.", n=60)
    r = client.post(
        "/v1/context/prepare",
        json={"content": doc, "query": "what is the invoice number?", "model_tier": "large"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["budget_tokens"] == 300
    assert body["decision"] == "retrieve"
