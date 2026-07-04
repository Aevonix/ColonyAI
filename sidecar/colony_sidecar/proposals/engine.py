"""Turn self-directed thoughts and research findings into Proposals."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from colony_sidecar.proposals.models import Proposal

_THINK_PREFIX = re.compile(r"^\[self-directed thinking\]\s*", re.IGNORECASE)

# Ungrounded rationale text: empty, or a generic "worth doing" placeholder
# that carries no evidence of WHY the work helps. Proposals whose only
# justification is one of these do not ship (honest why_it_helps: item 4).
_UNGROUNDED = re.compile(
    r"^(i think this work is worth doing( now)?\.?"
    r"|worth doing( now)?\.?"
    r"|advances your priorities\.?"
    r"|moves a piece of your work forward\.?)$",
    re.IGNORECASE,
)


def _first_sentence(text: str, limit: int = 200) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    m = re.search(r"[.!?](\s|$)", text)
    sentence = text[: m.end()].strip() if m else text
    return sentence[:limit].strip()


def build_from_thinker(initiative: Any) -> Optional[Proposal]:
    """Package one self-directed-thinking initiative as a Proposal.

    Returns None (does NOT ship) when the initiative carries no grounded
    reason: ``why_it_helps`` must come from the initiative's own evidence
    (its rationale), never a per-type template. A thought whose rationale is
    empty or a generic "worth doing" placeholder is not deliverable.
    """
    title = (getattr(initiative, "description", "") or "").strip()
    itype = getattr(initiative, "type", None)
    itype = getattr(itype, "value", None) or str(itype or "research")
    rationale = _THINK_PREFIX.sub("", getattr(initiative, "rationale", "") or "").strip()

    # Grounding gate: a real, evidence-bearing rationale is required.
    if not rationale or _UNGROUNDED.match(rationale):
        return None

    try:
        conf = float(getattr(initiative, "priority", 0.6) or 0.6)
    except (TypeError, ValueError):
        conf = 0.6
    return Proposal(
        title=title[:100] or "(proposal)",
        finding=rationale,
        # Grounded in the initiative's own evidence, not a template.
        why_it_helps=_first_sentence(rationale),
        suggested_action=title,
        citations=[],
        source="thinker",
        initiative_type=itype,
        confidence=conf,
    )


def build_from_research(goal: str, artifact_text: str, sources: list,
                        confidence: float = 0.7) -> Optional[Proposal]:
    """Package a research finding (with citations) as a Proposal.

    Returns None (does NOT ship) without grounded evidence: a research
    proposal needs an actual finding AND the question that motivated it (its
    grounded ``why_it_helps``). An empty finding or a missing goal means there
    is nothing honest to say about why it helps.
    """
    finding = (artifact_text or "").strip()
    goal = (goal or "").strip()
    if not finding or not goal:
        return None
    cites = []
    for s in (sources or [])[:6]:
        if isinstance(s, dict):
            cites.append({"title": s.get("title", ""), "url": s.get("url", "")})
        else:
            cites.append({"title": str(getattr(s, "title", "") or ""),
                          "url": str(getattr(s, "url", "") or "")})
    return Proposal(
        title=goal[:100],
        finding=finding[:1200],
        # Grounded in the actual question that prompted the research.
        why_it_helps=f"answers a question you're working on: {goal}"[:200],
        suggested_action="Review the finding and tell me if you want me to go deeper.",
        citations=cites, source="research", initiative_type="research",
        confidence=confidence,
    )


def proposal_to_payload(p: Proposal) -> Dict[str, Any]:
    """Delivery payload for a Proposal (dedicated 'proposal' type)."""
    return {
        "id": p.id,
        "type": "proposal",
        "priority": p.confidence,
        "title": p.title[:80],
        "description": p.render(),
        "rationale": p.finding,
        "suggested_action": p.suggested_action,
        "entity_id": None,          # proposals target the owner
        "entity_type": "proposal",
        "channel_hint": "dm",
        "context": {},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
