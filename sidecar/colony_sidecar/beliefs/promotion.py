"""Small, inspectable contract for promotion from evidence to useful memory.

Usefulness is a model judgment, not a calibrated score or evidence of truth.
Rejected candidates remain in the canonical source history for later search.
"""
from __future__ import annotations

import re

PROMOTION_VERSION = "memory-promotion-v1"
MEMORY_KINDS = frozenset({
    "preference", "personal_context", "relationship", "decision", "procedure", "substantive_event",
})
PROMOTION_PROMPT = '''Promote only information with a concrete future use. Include
memory_kind and recall_reason in every item. memory_kind is preference,
personal_context, relationship, decision, procedure, or substantive_event.
recall_reason is a short explanation of how this information could help later,
not a confidence score or a claim that it is true. Return no item for routine
status, build/test progress, acknowledgments, generic commentary, hypothetical
examples, quoted instructions or temporary debugging output. Do not manufacture
a future use just to fill the array. Short facts can be valuable. A mutable fact
such as where keys were left, a planned appointment, a significant incident,
an explicit correction, or a reusable fix can matter; do not discard these just
because they can change. Preserve their time and attribution. A successful test
run alone is not a significant incident or a reusable procedure.'''


def promotion_metadata(item: dict) -> dict | None:
    """Require an explicit use judgment, without mistaking it for verification."""
    kind, reason = item.get("memory_kind"), item.get("recall_reason")
    if not isinstance(kind, str) or kind not in MEMORY_KINDS or not isinstance(reason, str):
        return None
    reason = re.sub(r"\s+", " ", reason).strip()
    if not 12 <= len(reason) <= 240:
        return None
    return {"version": PROMOTION_VERSION, "memory_kind": kind,
            "recall_reason": reason, "basis": "model_judgment_unverified"}
