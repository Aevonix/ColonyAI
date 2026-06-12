"""Remote reranker provider: Qwen3 instruction templating.

Qwen3-Reranker scores from yes/no token logits that are only calibrated when
the request is wrapped in the model's instruction template; vLLM's /v1/rerank
does not apply it server-side. Raw strings rank noise above relevant passages
(observed live: "Bananas are yellow." 0.98 vs the correct answer 0.75), so the
provider must wrap requests itself when prompt_style="qwen3".
"""
import json

import pytest

from colony_sidecar.vector.reranker import (
    OpenAIAPIRerankerProvider,
    QWEN3_RERANK_PREFIX,
    QWEN3_RERANK_SUFFIX,
    format_qwen3_rerank,
)


def test_format_qwen3_wraps_query_and_documents():
    q, docs = format_qwen3_rerank("capital of France?", ["Paris.", "Berlin."])
    assert q.startswith(QWEN3_RERANK_PREFIX)
    assert "<Instruct>: " in q
    assert "<Query>: capital of France?" in q
    assert docs == [
        f"<Document>: Paris.{QWEN3_RERANK_SUFFIX}",
        f"<Document>: Berlin.{QWEN3_RERANK_SUFFIX}",
    ]


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    """Captures the outgoing payload and returns a canned rerank response."""

    sent = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.sent = json
        return _FakeResponse(
            {"results": [{"index": 1, "relevance_score": 0.97},
                         {"index": 0, "relevance_score": 0.01}]}
        )


@pytest.mark.asyncio
async def test_qwen3_style_sends_wrapped_payload_but_returns_original_text(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    p = OpenAIAPIRerankerProvider("Qwen/Qwen3-Reranker-8B")
    p.configure("http://127.0.0.1:8093", "key", prompt_style="qwen3")
    docs = ["Bananas are yellow.", "Paris is the capital of France."]
    res = await p.rerank("What is the capital of France?", docs, top_k=2)

    sent = _FakeClient.sent
    assert sent["query"].startswith(QWEN3_RERANK_PREFIX)
    assert all(d.startswith("<Document>: ") for d in sent["documents"])
    # Results must map back to the ORIGINAL document text, not the wrapped form
    assert res[0].text == "Paris is the capital of France."
    assert res[0].score == 0.97


@pytest.mark.asyncio
async def test_default_style_sends_raw_strings(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    p = OpenAIAPIRerankerProvider("some/other-reranker")
    p.configure("http://127.0.0.1:8093", "key")
    await p.rerank("q", ["d1", "d2"], top_k=2)
    assert _FakeClient.sent["query"] == "q"
    assert _FakeClient.sent["documents"] == ["d1", "d2"]
