"""Cheap token estimation — no tokenizer dependency.

The gate only needs estimates good to ~±20%; the decision headroom
(default 0.8) absorbs the imprecision. English prose runs ~4 chars per
token; code and symbol-dense text runs denser (~3 chars per token), so a
crude density probe adjusts the ratio.
"""

from __future__ import annotations

import math
import os

__all__ = ["estimate_tokens"]

_DEFAULT_CHARS_PER_TOKEN = 4.0
_CODE_CHARS_PER_TOKEN = 3.0


def _chars_per_token() -> float:
    try:
        v = float(os.environ.get("COLONY_CONTEXT_CHARS_PER_TOKEN", ""))
        if v > 0:
            return v
    except ValueError:
        pass
    return _DEFAULT_CHARS_PER_TOKEN


def _looks_dense(text: str, sample_limit: int = 20000) -> bool:
    """True when the text is symbol/whitespace-dense (code, logs, JSON)."""
    sample = text[:sample_limit]
    if not sample:
        return False
    symbolish = sum(
        1 for c in sample if not (c.isalpha() or c in " .,;:'\"!?-")
    )
    return symbolish / len(sample) > 0.25


def estimate_tokens(text: str) -> int:
    """Estimate the token count of *text*.

    Uses a chars-per-token heuristic (configurable via
    ``COLONY_CONTEXT_CHARS_PER_TOKEN``), with a denser ratio for
    code-like input. Deliberately dependency-free; accuracy within
    ~±20% is sufficient for gating decisions.
    """
    if not text:
        return 0
    cpt = _chars_per_token()
    if _looks_dense(text):
        cpt = min(cpt, _CODE_CHARS_PER_TOKEN)
    return math.ceil(len(text) / cpt)
