"""Skill distillation: one bounded LLM pass on qualifying completions.

Trigger discipline keeps the LLM cost modest and the library honest:
only success AFTER at least one retry, or a completion whose result carries a
resolution not already covered by an existing skill, qualifies. The LLM
proposes {title, situation, steps, gotchas} as STRICT JSON; code validates,
dedups by situation-signature overlap (>0.8 bumps the existing skill instead
of storing a near-duplicate), and enforces the library cap.

Modes (COLONY_SKILLS_DISTILL): off | shadow (log the would-be skill, store
nothing) | live. Default shadow.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, List, Optional

from colony_sidecar.skills_memory.models import (
    Skill, signature_overlap, situation_signature,
)
from colony_sidecar.skills_memory.store import SkillStore, skills_distill_mode, skills_max

logger = logging.getLogger(__name__)

# Result text that indicates a diagnosis/resolution worth remembering.
_RESOLUTION_RE = re.compile(
    r"\b(?:root cause|fixed by|resolved by|the fix|turned out|workaround|"
    r"solution was|caused by|diagnos)\w*\b", re.IGNORECASE)

_MAX_STEPS = 10
_MAX_GOTCHAS = 5

_SYSTEM_PROMPT = """\
You distill reusable procedures from completed work. Given a task and how it
was completed, extract ONE compact, generic procedure another agent could
follow when a similar situation recurs.

Respond with ONLY a JSON object (no prose, no markdown fences):
{"title": str (<80 chars, imperative), "situation": str (when this applies,
<200 chars), "steps": [str, ...] (2-8 concrete steps), "gotchas": [str, ...]
(0-4 pitfalls, may be empty)}.
If the work contains no reusable procedure, respond with exactly: null"""


def should_distill(attempt_count: int, result_text: str,
                   store: Optional[SkillStore]) -> bool:
    """Trigger conditions: retry-success, or a novel diagnosis in the result."""
    if attempt_count and int(attempt_count) >= 1:
        return True
    text = (result_text or "").strip()
    if not text or not _RESOLUTION_RE.search(text):
        return False
    # Novel: the resolution's signature is not already covered by a skill.
    if store is not None:
        sig = situation_signature(text[:400])
        for s in store.list(limit=1000):
            if signature_overlap(sig, s.situation_signature) > 0.5:
                return False
    return True


def _parse_skill_json(content: str) -> Optional[dict]:
    text = (content or "").strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
    if text.lower() in ("null", "none", ""):
        return None
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        text = m.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.debug("skill distill output was not valid JSON; dropping")
        return None
    return data if isinstance(data, dict) else None


def _validate(data: dict, domain: str, source_ref: str) -> Optional[Skill]:
    title = str(data.get("title", "")).strip()[:120]
    situation = str(data.get("situation", "")).strip()[:400]
    steps = [str(s).strip()[:300] for s in (data.get("steps") or [])
             if str(s).strip()][:_MAX_STEPS]
    gotchas = [str(g).strip()[:300] for g in (data.get("gotchas") or [])
               if str(g).strip()][:_MAX_GOTCHAS]
    if not title or not situation or not steps:
        return None
    return Skill(title=title, situation=situation, steps=steps,
                 gotchas=gotchas, domain=(domain or "").lower(),
                 source_ref=source_ref)


async def distill_from_completion(
    router: Any,
    store: Optional[SkillStore],
    *,
    domain: str,
    task_text: str,
    result_text: str,
    source_ref: str = "",
    mode: Optional[str] = None,
) -> Optional[Skill]:
    """One reasoning pass -> validated, deduped Skill (or None).

    Returns the stored (or shadow-logged) Skill. Dedup: an existing skill with
    >0.8 signature overlap gets its use count bumped instead.
    """
    mode = mode or skills_distill_mode()
    if mode == "off" or router is None:
        return None
    prompt = (f"TASK ({domain}):\n{(task_text or '')[:1500]}\n\n"
              f"HOW IT WAS COMPLETED:\n{(result_text or '')[:2500]}")
    try:
        response = await router.complete(
            [{"role": "system", "content": _SYSTEM_PROMPT},
             {"role": "user", "content": prompt}],
            context={"task": "skill_distillation"})
        content = getattr(response, "content", "") or ""
    except Exception as exc:
        logger.debug("skill distillation LLM call failed: %s", exc)
        return None

    data = _parse_skill_json(content)
    if not data:
        return None
    skill = _validate(data, domain, source_ref)
    if skill is None:
        return None

    if store is not None:
        existing = store.find_similar(skill.situation_signature, threshold=0.8)
        if existing is not None:
            store.bump_use(existing.id)
            logger.info("skills_memory: near-duplicate of %s -- bumped use "
                        "instead of storing %r", existing.id, skill.title)
            return existing

    if mode == "shadow":
        logger.info("SHADOW-SKILL distilled (not stored): %s",
                    json.dumps(skill.to_row(), default=str)[:600])
        return skill

    if store is not None:
        store.add(skill)
        store.evict_to_cap(skills_max())
        logger.info("skills_memory: stored skill %s %r (domain=%s)",
                    skill.id, skill.title, skill.domain)
    return skill
