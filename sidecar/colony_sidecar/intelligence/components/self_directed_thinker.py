"""Self-directed thinking — Colony's internal cognition phase (v0.17.0).

The InitiativeEngine's generators are data-reactive: they scan existing
graph state for known shapes of work (stale goals, neglected contacts,
failing CI). The thinker is the complement: on a slow cadence it hands
an LLM a snapshot of Colony's situation — goals, commitments, recent
work, capability gaps, open questions — and asks what work SHOULD exist
that nothing has surfaced yet. Output becomes ordinary initiatives that
flow through the same store, delivery, and approval machinery as
everything else.

Safety posture:

- Disabled unless ``COLONY_ENABLE_INTERNAL_THINKING=true``.
- Runs at most once per ``COLONY_THINKING_INTERVAL_SECS`` (default 1h),
  not every loop tick.
- At most ``COLONY_THINKING_MAX_INITIATIVES`` (default 3) per cycle.
- Proposals are capped at priority 0.85 and may never carry an
  ``action_hint`` — thought-up work always lands as review/decide
  initiatives, never as directly executable agent actions. Anything
  mutating or outbound therefore still crosses the action registry and
  the owner-approval gate before an agent can touch it.
- Dedup keys remember recent proposals so repeated thinking cycles
  don't re-pitch the same idea.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from colony_sidecar.intelligence.components.initiative_engine import (
    Initiative,
    InitiativeType,
)

logger = logging.getLogger(__name__)

# Types the LLM may propose. Deliberately excludes AGENT_ACTION (and
# anything else that auto-executes) — see module docstring.
_ALLOWED_TYPES = {
    "research": InitiativeType.RESEARCH,
    "task": InitiativeType.TASK,
    "project": InitiativeType.PROJECT,
    "system": InitiativeType.SYSTEM,
    "knowledge_acquisition": InitiativeType.KNOWLEDGE_ACQUISITION,
    "capability_gap": InitiativeType.CAPABILITY_GAP,
}

_MAX_PRIORITY = 0.85
_RECENT_KEY_CAP = 200

_SYSTEM_PROMPT = """\
You are Colony, the always-on intelligence sidecar of an autonomous \
agent. You are in your private thinking phase: nobody asked you a \
question. Review the situation report and decide whether any genuinely \
valuable work should exist that is not already underway.

Rules:
- Propose at most {max_items} initiatives. Quality over quantity — an \
empty list is a good answer when nothing is genuinely worth doing.
- Never re-propose anything resembling the CURRENT INITIATIVES list.
- Never propose sending messages, spending money, signing up for \
services, or modifying external systems directly. If such a thing seems \
valuable, propose it as work to evaluate and let the owner decide.
- Prefer work that compounds: filling knowledge gaps, repairing failing \
capabilities, consolidating memory, advancing stalled goals.

Respond with ONLY a JSON array (no prose, no markdown fences). Each \
element: {{"title": str (imperative, <100 chars), "type": one of \
{allowed}, "priority": float 0.0-1.0, "rationale": str (why this, why \
now, grounded in the situation report)}}."""


class SelfDirectedThinker:
    """Generates novel initiatives from periodic LLM reflection."""

    def __init__(self, router: Any,
                 interval_secs: Optional[int] = None,
                 max_per_cycle: Optional[int] = None) -> None:
        self._router = router
        self._interval = interval_secs if interval_secs is not None else int(
            os.environ.get("COLONY_THINKING_INTERVAL_SECS", "3600"))
        self._max = max_per_cycle if max_per_cycle is not None else int(
            os.environ.get("COLONY_THINKING_MAX_INITIATIVES", "3"))
        self._last_run: Optional[float] = None
        self._recent_keys: List[str] = []

    # -- scheduling ---------------------------------------------------------

    def due(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.monotonic()
        if self._last_run is None:
            return True
        return (now - self._last_run) >= self._interval

    def mark_ran(self, now: Optional[float] = None) -> None:
        self._last_run = now if now is not None else time.monotonic()

    # -- thinking -----------------------------------------------------------

    async def think(self, situation: Dict[str, Any]) -> List[Initiative]:
        """One reflection cycle: situation report in, initiatives out."""
        if self._router is None:
            return []
        prompt = self._build_messages(situation)
        try:
            response = await self._router.complete(
                prompt, context={"task": "internal_thinking"})
            content = getattr(response, "content", "") or ""
        except Exception as exc:
            logger.warning("Self-directed thinking LLM call failed: %s", exc)
            return []

        initiatives: List[Initiative] = []
        for item in self._parse(content)[: self._max]:
            initiative = self._to_initiative(item)
            if initiative is None:
                continue
            if initiative.dedup_key in self._recent_keys:
                logger.debug("Thinking dedup: %s", initiative.dedup_key)
                continue
            self._remember(initiative.dedup_key)
            initiatives.append(initiative)
        if initiatives:
            logger.info("Self-directed thinking produced %d initiative(s): %s",
                        len(initiatives),
                        "; ".join(i.description[:60] for i in initiatives))
        return initiatives

    # -- internals ----------------------------------------------------------

    def _build_messages(self, situation: Dict[str, Any]) -> List[dict]:
        sections = []
        for name, value in situation.items():
            if value in (None, "", [], {}):
                continue
            try:
                rendered = json.dumps(value, default=str, indent=None)[:4000]
            except (TypeError, ValueError):
                rendered = str(value)[:4000]
            sections.append(f"## {name}\n{rendered}")
        report = "\n\n".join(sections) or "(no situation data available)"
        system = _SYSTEM_PROMPT.format(
            max_items=self._max, allowed=sorted(_ALLOWED_TYPES))
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"SITUATION REPORT\n\n{report}"},
        ]

    @staticmethod
    def _parse(content: str) -> List[dict]:
        text = content.strip()
        # Tolerate markdown fences and leading prose despite instructions.
        if "```" in text:
            match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()
        if not text.startswith("["):
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if not match:
                return []
            text = match.group(0)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Thinking output was not valid JSON; dropping")
            return []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def _to_initiative(self, item: dict) -> Optional[Initiative]:
        title = str(item.get("title", "")).strip()
        type_key = str(item.get("type", "")).strip().lower()
        rationale = str(item.get("rationale", "")).strip()
        if not title or type_key not in _ALLOWED_TYPES:
            return None
        try:
            priority = float(item.get("priority", 0.5))
        except (TypeError, ValueError):
            priority = 0.5
        priority = max(0.0, min(_MAX_PRIORITY, priority))
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
        return Initiative(
            id=f"init-think-{uuid.uuid4().hex[:12]}",
            type=_ALLOWED_TYPES[type_key],
            description=title,
            priority=priority,
            rationale=f"[self-directed thinking] {rationale}" if rationale
            else "[self-directed thinking]",
            action_hint=None,  # never directly executable — see docstring
            dedup_key=f"thinking:{slug}",
        )

    def _remember(self, key: Optional[str]) -> None:
        if not key:
            return
        self._recent_keys.append(key)
        if len(self._recent_keys) > _RECENT_KEY_CAP:
            self._recent_keys = self._recent_keys[-_RECENT_KEY_CAP:]
