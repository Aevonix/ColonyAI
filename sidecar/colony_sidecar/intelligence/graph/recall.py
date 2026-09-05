"""Small retrieval helpers shared by the existing graph/vector recall path."""
from __future__ import annotations

import re
import hashlib
import json
from typing import Any

FULLTEXT_INDEX = "memory_content_fulltext"
_WORDS = re.compile(r"[^\W_]+(?:[-.][^\W_]+)*", re.UNICODE)
_STOP = frozenset("a an and are as at be by do does for from how i in is it me of on or that the this to was what when where which who with you".split())


def calibration_fingerprint(metadata: dict[str, Any]) -> str:
    """A configuration stamp, not proof of the remotely served weights."""
    return hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def render_memory_context(memories: list[dict[str, Any]]) -> str:
    """Preserve source handles and uncertainty in the injected evidence packet."""
    lines = []
    for memory in memories:
        source = {"id": str(memory.get("id") or ""),
                  "source": str(memory.get("source_uri") or ""),
                  "state": str(memory.get("epistemic_state") or "inferred")}
        if memory.get("effective_confidence") is not None:
            source["confidence"] = memory["effective_confidence"]
        if memory.get("created_at") is not None:
            source["recorded_at"] = str(memory["created_at"])
        if memory.get("contradiction_count"):
            source["contradictions"] = memory["contradiction_count"]
        if memory.get("rerank_calibration"):
            source["rerank_calibration"] = memory["rerank_calibration"]
        if memory.get("rerank_status") == "unavailable":
            source["rerank_status"] = "unavailable"
        lines.append(f"- {json.dumps(source, ensure_ascii=False)} {memory.get('content', '')}")
    return "\n".join(lines)


def lexical_query(text: str, max_terms: int = 16) -> str:
    """Literal words/identifiers, not caller-supplied Lucene operators."""
    words = dict.fromkeys(word.casefold() for word in _WORDS.findall(text[:4000])
                          if word.casefold() not in _STOP)
    return " OR ".join(f'"{word}"' for word in list(words)[:max_terms])


def fuse_candidates(
    dense: list[dict[str, Any]],
    lexical: list[dict[str, Any]],
    *,
    limit: int,
    strength_ranking: bool = False,
) -> list[dict[str, Any]]:
    """Fuse independent ranks, never incomparable Lucene/cosine raw scores.

    Inputs have already passed authoritative scope and epistemic checks. A
    lexical row wins duplicate hydration because it was read after vector
    hydration and may contain a newer correction.
    """
    rows: dict[str, dict[str, Any]] = {}
    ranks: dict[str, float] = {}
    dense = sorted(dense, key=lambda row: row.get("relevance", 0), reverse=True)
    for candidates in (dense, lexical):
        seen = set()
        for rank, row in enumerate(candidates, 1):
            mid = str(row.get("id") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            rows[mid] = dict(row)
            ranks[mid] = ranks.get(mid, 0) + 1 / (60 + rank)
    for mid, row in rows.items():
        confidence = float(row.get("effective_confidence", row.get("strength", 1)))
        relevance = ranks[mid] * confidence
        if strength_ranking:
            relevance *= .5 + .5 * float(row.get("strength", 1))
        row["relevance"] = relevance
        row["retrieval_method"] = "hybrid"
    return sorted(rows.values(), key=lambda row: (-row["relevance"], str(row["id"])))[:limit]
