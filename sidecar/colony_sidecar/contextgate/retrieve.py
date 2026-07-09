"""Chunk ranking for the context gate.

Embeddings when the caller provides an ``embed_fn`` (any async
``list[str] -> list[list[float]]``); otherwise a dependency-free lexical
TF-IDF cosine ranker, so the gate works even where no embedding stack is
configured.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from typing import Awaitable, Callable, Optional, Sequence

from colony_sidecar.contextgate.chunker import Chunk

logger = logging.getLogger(__name__)

__all__ = ["rank_chunks", "lexical_scores"]

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]

_WORD_RE = re.compile(r"[a-z0-9]+")

# Minimal English stopword set — enough to stop function words from
# dominating TF-IDF; deliberately tiny to stay language-tolerant.
_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have if in into is it its of on "
    "or that the their there these they this to was were what when where which "
    "who will with you your".split()
)


def _terms(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS]


def lexical_scores(chunks: Sequence[Chunk], query: str) -> list[float]:
    """TF-IDF cosine similarity of each chunk against *query* (0..1)."""
    q_terms = _terms(query)
    if not q_terms or not chunks:
        return [0.0] * len(chunks)

    chunk_terms = [_terms(c.text) for c in chunks]
    n = len(chunks)
    df: Counter[str] = Counter()
    for terms in chunk_terms:
        df.update(set(terms))

    def _idf(term: str) -> float:
        return math.log(1.0 + n / (1.0 + df.get(term, 0)))

    q_tf = Counter(q_terms)
    q_vec = {t: (1.0 + math.log(c)) * _idf(t) for t, c in q_tf.items()}
    q_norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0

    scores: list[float] = []
    for terms in chunk_terms:
        tf = Counter(terms)
        dot = 0.0
        norm_sq = 0.0
        for t, c in tf.items():
            w = (1.0 + math.log(c)) * _idf(t)
            norm_sq += w * w
            qv = q_vec.get(t)
            if qv:
                dot += w * qv
        norm = math.sqrt(norm_sq) or 1.0
        scores.append(dot / (norm * q_norm))
    return scores


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


async def rank_chunks(
    chunks: Sequence[Chunk],
    query: str,
    embed_fn: Optional[EmbedFn] = None,
) -> list[tuple[Chunk, float]]:
    """Rank *chunks* by relevance to *query*, best first.

    Uses *embed_fn* (batched: query + chunks in one call) when provided,
    falling back to lexical TF-IDF on any embedding failure so ranking
    never hard-fails.
    """
    if not chunks:
        return []

    if embed_fn is not None and query:
        try:
            vectors = await embed_fn([query] + [c.text for c in chunks])
            if len(vectors) == len(chunks) + 1:
                q_vec = vectors[0]
                scored = [
                    (c, _cosine(q_vec, v)) for c, v in zip(chunks, vectors[1:])
                ]
                scored.sort(key=lambda cs: cs[1], reverse=True)
                return scored
            logger.warning(
                "embed_fn returned %d vectors for %d texts — falling back to lexical",
                len(vectors), len(chunks) + 1,
            )
        except Exception:
            logger.warning("embed_fn failed — falling back to lexical ranking", exc_info=True)

    scored = list(zip(chunks, lexical_scores(chunks, query)))
    scored.sort(key=lambda cs: cs[1], reverse=True)
    return scored
