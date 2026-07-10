"""Leveled tom2 renderers (L2.2) — level 1 self-reflexive, level 2 silent-prior.

Two renderers, one per level above owner-surfacing, both shipped dark
(nothing live calls them until the wiring tranche):

* ``render_level1`` — the READER'S OWN epistemic rows only. ``knows`` rows
  render the reader's own fact text (every ref must resolve to a fact row
  the reader owns — fail closed); ``unaware_of`` rows collapse to ONE
  content-free caution line: no fact text, no counts, no refs, because any
  of those would leak topology about what is being withheld.
* ``render_level2`` — third-party epistemic topology, consuming ONLY rows
  the L2.1 eligibility pipeline already passed, and rendering each through
  the UNMODIFIED H3.5 gate (``render_inference_for_contact``) — this module
  deliberately owns no visibility logic of its own, so it cannot weaken the
  double gate. Output is framed as a SILENT prior: instructions to never
  volunteer or reference it, only to avoid wrongly assuming shared
  knowledge (M8 is a style, not a safety layer — the safety is upstream).

Neither renderer touches the H3.5 functions; both fail closed to None.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from colony_sidecar.tom.tom2 import (
    _ref_visible_to, render_inference_for_contact)

logger = logging.getLogger(__name__)

#: The one content-free line an unaware_of row about the reader may become.
UNAWARE_CAUTION = ("Some context has not been shared with this contact yet; "
                   "do not assume they know everything you know.")

LEVEL2_HEADER = ("Epistemic prior (SILENT — never volunteer, quote, or "
                 "reference this; use it only to avoid wrongly assuming "
                 "shared knowledge):")


def render_level1(tom2_store: Any, facts_store: Any, reader_contact_id: str,
                  *, limit: int = 5) -> Optional[str]:
    """Self-reflexive prior for the reader: their own knows/unaware rows.

    Never renders anything about a third party; never renders fact text the
    reader does not own; any error returns None (fail closed)."""
    reader = str(reader_contact_id or "").strip()
    if tom2_store is None or facts_store is None or not reader:
        return None
    try:
        rows = tom2_store.list_inferences(contact_id=reader, limit=100)
    except Exception:
        logger.debug("level1 read failed (=> None)", exc_info=True)
        return None

    lines: List[str] = []
    saw_unaware = False
    for row in rows:
        if str(row.get("contact_id") or "") != reader:
            continue                                # never a third party
        kind = row.get("kind")
        if kind == "unaware_of":
            saw_unaware = True                      # content-free, collapsed
            continue
        if kind != "knows":
            continue
        try:
            refs = [row.get("fact_ref")] + list(row.get("evidence_refs")
                                                or [])
            if not refs or any(not _ref_visible_to(facts_store, r, reader)
                               for r in refs):
                continue                            # fail closed per row
            fact = facts_store.get_fact(str(row.get("fact_ref")))
            text = str((fact or {}).get("fact") or "").strip()
        except Exception:
            continue
        if not text:
            continue
        lines.append(f"- This contact already knows: {text[:160]}")
        if len(lines) >= max(1, int(limit)):
            break
    if saw_unaware:
        lines.append(f"- {UNAWARE_CAUTION}")
    return "\n".join(lines) if lines else None


def render_level2(eligible_rows: List[Dict[str, Any]], facts_store: Any,
                  reader_contact_id: str, *, limit: int = 3) -> Optional[str]:
    """Silent-prior rendering of ALREADY-ELIGIBLE third-party inferences.

    Consumes only rows the L2.1 pipeline passed; every line still goes
    through the unmodified H3.5 gate, so even a caller that hands this an
    unvetted row cannot make it render foreign content. None when nothing
    renders (fail closed, no partial hints)."""
    reader = str(reader_contact_id or "").strip()
    if not eligible_rows or facts_store is None or not reader:
        return None
    lines: List[str] = []
    for row in eligible_rows:
        try:
            line = render_inference_for_contact(row, facts_store, reader)
        except Exception:
            logger.debug("level2 render failed for a row (skipped)",
                         exc_info=True)
            continue
        if line:
            lines.append(f"- {line}")
        if len(lines) >= max(1, int(limit)):
            break
    if not lines:
        return None
    return "\n".join([LEVEL2_HEADER] + lines)
