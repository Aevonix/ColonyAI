"""Skill retrieval: situation -> the k most relevant procedures, as a compact
prompt block. Pure read; keyword-signature scoring (embedding optional later)."""

from __future__ import annotations

from typing import List, Optional

from colony_sidecar.skills_memory.models import (
    Skill, signature_overlap, situation_signature,
)
from colony_sidecar.skills_memory.store import SkillStore

_MIN_OVERLAP = 0.08


def relevant_skills(store: Optional[SkillStore], situation: str,
                    k: int = 3, domain: Optional[str] = None) -> List[Skill]:
    """Top-k skills by signature overlap with the situation (domain-boosted)."""
    if store is None or not (situation or "").strip():
        return []
    sig = situation_signature(situation)
    scored = []
    for s in store.list(limit=100000):
        ov = signature_overlap(sig, s.situation_signature)
        if domain and s.domain == (domain or "").lower():
            ov += 0.05
        # Track-record weighting: skills that historically informed winning
        # runs rank up, losers rank down. Laplace prior keeps a fresh skill
        # neutral (factor 1.0); the executor's outcome attribution feeds
        # wins/losses, so use changes future retrieval.
        wins = int(getattr(s, "wins", 0) or 0)
        losses = int(getattr(s, "losses", 0) or 0)
        win_rate = (wins + 1.0) / (wins + losses + 2.0)
        ov *= 0.75 + 0.5 * win_rate
        if ov >= _MIN_OVERLAP:
            scored.append((ov, s))
    scored.sort(key=lambda t: t[0], reverse=True)
    top = [s for _, s in scored[:k]]
    for s in top:
        try:
            store.bump_use(s.id)
        except Exception:
            pass
    return top


def format_block(skills: List[Skill], strategy_note: str = "") -> str:
    """Render skills (+ optional per-domain strategy note) for a system prompt.
    Empty string when there is nothing to say."""
    lines: List[str] = []
    if skills:
        lines.append("## Relevant past procedures (learned from your own prior work)")
        for s in skills:
            lines.append(f"- {s.title} -- applies when: {s.situation}")
            for i, step in enumerate(s.steps, 1):
                lines.append(f"    {i}. {step}")
            for g in s.gotchas:
                lines.append(f"    ! gotcha: {g}")
    if (strategy_note or "").strip():
        lines.append("## Lessons from past failures in this domain")
        lines.append(strategy_note.strip())
    return "\n".join(lines)
