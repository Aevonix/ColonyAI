"""A previous copy of the request must not crowd out independent evidence."""
from copy import deepcopy
from datetime import datetime
import json

import pytest

from apsimo.intelligence.graph.recall import source_candidates
from apsimo.intelligence.graph.selection import RecallSelector
from apsimo.beliefs.source_projection import SourceClaimProjection
from apsimo.beliefs.source_time import interpret_time_query
from apsimo.turns.idempotency import TurnIdempotencyLedger, source_message_hash
from test_source_claim_projection import Model, claim


def quotation(identifier, text, role="user", **extra):
    return dict(source_candidates([{
        "turn_id": identifier, "role": role, "content": text,
        "source_message_hash": identifier + "-version",
        "scope": "person", "contact_id": "person-a", "session_id": "earlier",
    }])[0], **extra)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow", "on", "failed"])
async def test_exact_request_is_supplementary_across_rerank_paths(monkeypatch, mode):
    query = "Where is the spare connector?"
    echo = quotation("question", query)
    correction = quotation("correction", "The spare connector is now in drawer six.")
    observation = quotation("observation", "The connector inspection found bent pins.", "assistant")
    rows = [echo, observation, correction]
    original = deepcopy(rows)
    monkeypatch.setenv("COLONY_RECALL_RERANK", "on" if mode == "failed" else mode)
    monkeypatch.delenv("COLONY_RECALL_RERANK_MIN_SCORE", raising=False)

    async def rank(query, documents, top_k):
        assert documents == [observation["content"], correction["content"], echo["content"]]
        if mode == "failed":
            raise RuntimeError("controlled unavailable reranker")
        # Exact lexical/semantic overlap is not independent answer evidence.
        return [{"index": i, "score": 100 if text == query else 2 - i}
                for i, text in enumerate(documents)]

    selected, context = await RecallSelector(rank).select_context(query, [], rows, limit=2)
    assert [row["source_turn_id"] for row in selected] == ["observation", "correction"]
    assert rows == original
    assert '"role": "assistant"' in context and '"role": "user"' in context
    assert len(context) <= 6000
    # The source is still available when the complete packet fits.
    selected, _ = await RecallSelector().select_context(query, [], rows, limit=3)
    assert selected[-1]["source_turn_id"] == "question"


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"atomic_evidence": True}, {"validity_status": "temporal_history"},
    {"procedure_context": "complete"}, {"source_context": "complete"},
    {"_annotation_ids": ["annotation"]}, {"scope": "session"},
    {"contact_id": None}, {"epistemic_state": "derived_unverified"},
])
async def test_qualified_or_uncertain_source_is_not_deprioritized(monkeypatch, extra):
    monkeypatch.setenv("COLONY_RECALL_RERANK", "off")
    query = "Where was the connector yesterday?"
    qualified = quotation("qualified", query, **extra)
    rows = [qualified, quotation("other", "An unrelated observation.", "assistant")]
    selected, _ = await RecallSelector().select_context(query, [], rows, limit=1)
    assert selected[0]["source_turn_id"] == "qualified"


@pytest.mark.asyncio
async def test_history_and_assistant_observations_keep_exact_bytes(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_RERANK", "off")
    query = "What did we ask and observe about the connector yesterday?"
    rows = [quotation("question", "Where is the spare connector?"),
            quotation("observation", "I measured its resistance as 4 ohms.", "assistant"),
            quotation("tool", "Continuity test: PASS", "tool")]
    selected, _ = await RecallSelector().select_context(query, [], rows, limit=3)
    assert [(r["source_turn_id"], r["content"], r["role"]) for r in selected] == [
        (r["source_turn_id"], r["content"], r["role"]) for r in rows]


@pytest.mark.asyncio
async def test_no_semantic_or_case_normalization(monkeypatch):
    monkeypatch.setenv("COLONY_RECALL_RERANK", "off")
    rows = [quotation("case-differs", "Where is the Connector?"),
            quotation("observation", "The connector is in drawer six.")]
    selected, _ = await RecallSelector().select_context("Where is the connector?", [], rows, limit=1)
    assert selected[0]["source_turn_id"] == "case-differs"


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted", [False, True])
async def test_pending_and_admitted_correction_survive_repeated_recall(tmp_path, monkeypatch, admitted):
    """Reconstructed ingestion/FTS/projection/packing case, not a model benchmark."""
    monkeypatch.setenv("COLONY_RECALL_RERANK", "off")
    query = "Which drawer currently holds the spare connector? Use retained evidence and answer briefly."
    old = "Remember this workshop fact: the spare connector is in drawer four."
    correction = "Correction: the spare connector is now in drawer six. The previous drawer is outdated. Remember the correction."
    ledger = TurnIdempotencyLedger(tmp_path / "source.db")
    projection = SourceClaimProjection(ledger)
    model = Model({
        old: claim("the spare connector is in drawer four.", "drawer four", subject="spare connector", predicate="location"),
        correction: claim(correction, "drawer six", subject="spare connector", predicate="location", operation="correct", match_prior=True),
    })

    def record(identifier, user, assistant, *, derive=False):
        ledger.record_source(identifier, contact_id="person-a", session_id="earlier",
            messages=[{"role": "user", "content": user}, {"role": "assistant", "content": assistant}],
            occurred_at="2026-03-01T12:00:00+00:00", derive_claims=derive)

    record("formation", old, "", derive=True)
    assert await projection.process_one(model)
    record("question-old", query.replace("currently ", ""),
        "Drawer four. Per retained memory, the spare connector is in drawer four.")
    record("correction", correction, "", derive=True)
    record("question-new", query, "Drawer six. Per corrected memory, the spare connector is in drawer six.")
    record("question-repeat", query, "")
    if admitted:
        assert await projection.process_one(model)
    scope = {"contact_id": "person-a", "session_id": "fresh"}
    hits = ledger.search_sources(query, **scope, limit=10)
    beliefs, quotes = projection.prepare_context([], hits, **scope,
        time_query=interpret_time_query(query, now=datetime.fromisoformat("2026-03-02T12:00:00+00:00")))
    selected, context = await RecallSelector().select_context(query, beliefs, quotes, limit=5, max_chars=6000)
    # Test evidence membership and exact attribution, not just the new value
    # appearing in a prior assistant's answer or a source-ID metadata list.
    if admitted:
        assertions = [a for r in selected if r.get("content_format") == "source_assertions_v1"
                      for a in json.loads(r["content"])["assertions"]]
        assert len(assertions) == 1
        assert assertions[0]["source"] == "turn:correction"
        assert assertions[0]["role"] == "user" and assertions[0]["quote"] == correction
    else:
        direct = [r for r in selected if r.get("source_turn_id") == "correction" and r.get("role") == "user"]
        assert len(direct) == 1
        assert direct[0]["content"] == correction
        assert direct[0]["source_message_hash"] == source_message_hash("earlier", {"role": "user", "content": correction})
        assert any(r.get("source_turn_id") == "formation" for r in selected)
    assert any(r.get("role") == "assistant" for r in selected)
    assert len(selected) <= 5 and len(context) <= 6000
