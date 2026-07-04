"""Turn self-directed thoughts and research findings into Proposals."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict

from colony_sidecar.proposals.models import Proposal

# Why a given kind of proposed work helps the owner (generic framing).
_WHY_BY_TYPE = {
    "research": "surfaces information relevant to your goals before you have to ask",
    "knowledge_acquisition": "fills a gap in what I know about your world so I can help better",
    "capability_gap": "repairs something I currently cannot do reliably for you",
    "task": "moves a piece of your work forward",
    "project": "advances one of your projects",
    "system": "keeps your systems healthy",
}

_THINK_PREFIX = re.compile(r"^\[self-directed thinking\]\s*", re.IGNORECASE)


def build_from_thinker(initiative: Any) -> Proposal:
    """Package one self-directed-thinking initiative as a Proposal."""
    title = (getattr(initiative, "description", "") or "").strip()
    itype = getattr(initiative, "type", None)
    itype = getattr(itype, "value", None) or str(itype or "research")
    rationale = _THINK_PREFIX.sub("", getattr(initiative, "rationale", "") or "").strip()
    try:
        conf = float(getattr(initiative, "priority", 0.6) or 0.6)
    except (TypeError, ValueError):
        conf = 0.6
    return Proposal(
        title=title[:100] or "(proposal)",
        finding=rationale or "I think this work is worth doing now.",
        why_it_helps=_WHY_BY_TYPE.get(itype, "advances your priorities"),
        suggested_action=title,
        citations=[],
        source="thinker",
        initiative_type=itype,
        confidence=conf,
    )


def build_from_research(goal: str, artifact_text: str, sources: list,
                        confidence: float = 0.7) -> Proposal:
    """Package a research finding (with citations) as a Proposal."""
    cites = []
    for s in (sources or [])[:6]:
        if isinstance(s, dict):
            cites.append({"title": s.get("title", ""), "url": s.get("url", "")})
        else:
            cites.append({"title": str(getattr(s, "title", "") or ""),
                          "url": str(getattr(s, "url", "") or "")})
    return Proposal(
        title=(goal or "Research finding")[:100],
        finding=(artifact_text or "").strip()[:1200],
        why_it_helps="answers a question relevant to your goals",
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
