"""Project planner: objective -> validated, dependency-ordered Steps.

One LLM planning pass returns a STRICT JSON step list; deterministic code
re-validates everything (same discipline as directed intake): unknown
action_kinds are dropped, dependency cycles are broken, unknown/self deps are
stripped, and the step count is capped. The LLM proposes; code decides.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from colony_sidecar.projects.models import ACTION_KINDS, Step, projects_max_steps

logger = logging.getLogger(__name__)

# The system prompt composes through the shared cognition charter
# (cognition/charter.py, role "planner"): shared doctrine + planner rules +
# injected self-model/boundaries/skills. This block only supplies the
# action-kind vocabulary the role rules reference.
_ACTION_KINDS_CONTEXT = """\
Allowed action kinds (choose the weakest kind that does the job):
- "analyze": reason over information the system already has (memory, world
  model, configured read-only repo mirrors).
- "research": gather new information (web search, background research).
- "internal": internal bookkeeping (record findings, update memory).
- "directed": delegate scoped repo/code work to the directed-action pipeline
  (it has its own approval gates). Use ONLY for work on the owner's
  designated repositories.
- "deliver": report a finding/milestone to the owner."""


def _parse_array(content: str) -> List[dict]:
    text = (content or "").strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
    if not text.startswith("["):
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            return []
        text = m.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("project planner output was not valid JSON; dropping")
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def validate_steps(raw: List[dict], project_id: str = "",
                   max_steps: Optional[int] = None) -> List[Step]:
    """Deterministic re-validation of an LLM-proposed step list.

    Drops steps with unknown action_kind, strips unknown/self dependencies,
    breaks dependency cycles (a dep that is not strictly ordinal-decreasing
    after topological sorting is dropped), caps the count, and renumbers
    ordinals contiguously.
    """
    cap = max_steps or projects_max_steps()

    # 1. Shape + kind filter.
    cleaned: List[Dict[str, Any]] = []
    for d in raw or []:
        desc = str(d.get("description", "")).strip()
        kind = str(d.get("action_kind", "")).strip().lower()
        if not desc or kind not in ACTION_KINDS:
            continue
        try:
            ordinal = int(d.get("ordinal", len(cleaned) + 1))
        except (TypeError, ValueError):
            ordinal = len(cleaned) + 1
        deps = []
        for dep in (d.get("depends_on") or []):
            try:
                deps.append(int(dep))
            except (TypeError, ValueError):
                continue
        try:
            confidence = max(0.0, min(1.0, float(d.get("confidence", 0.6))))
        except (TypeError, ValueError):
            confidence = 0.6
        cleaned.append({"ordinal": ordinal, "description": desc[:500],
                        "action_kind": kind, "depends_on": deps,
                        "confidence": confidence})

    if not cleaned:
        return []

    # 2. Deduplicate ordinals (keep first) and drop unknown/self deps.
    by_ord: Dict[int, Dict[str, Any]] = {}
    for d in cleaned:
        if d["ordinal"] not in by_ord:
            by_ord[d["ordinal"]] = d
    known = set(by_ord)
    for d in by_ord.values():
        d["depends_on"] = [x for x in d["depends_on"]
                           if x in known and x != d["ordinal"]]

    # 3. Topological sort (Kahn); cycle-breaking: when no node is free, cut
    #    the remaining node with the fewest deps loose (drop its deps).
    remaining = dict(by_ord)
    ordered: List[Dict[str, Any]] = []
    placed: set = set()
    while remaining:
        free = [o for o, d in remaining.items()
                if all(dep in placed for dep in d["depends_on"])]
        if not free:
            victim = min(remaining.values(),
                         key=lambda d: (len(d["depends_on"]), d["ordinal"]))
            logger.info("project planner: breaking dependency cycle at step %s",
                        victim["ordinal"])
            victim["depends_on"] = [dep for dep in victim["depends_on"]
                                    if dep in placed]
            free = [victim["ordinal"]]
        for o in sorted(free):
            ordered.append(remaining.pop(o))
            placed.add(o)

    # 4. Cap, then drop deps pointing at capped-away steps.
    ordered = ordered[:cap]
    kept = {d["ordinal"] for d in ordered}
    for d in ordered:
        d["depends_on"] = [x for x in d["depends_on"] if x in kept]

    # 5. Renumber contiguously (deps remapped).
    remap = {d["ordinal"]: i + 1 for i, d in enumerate(ordered)}
    steps: List[Step] = []
    for d in ordered:
        steps.append(Step(
            project_id=project_id,
            ordinal=remap[d["ordinal"]],
            description=d["description"],
            action_kind=d["action_kind"],
            depends_on=sorted(remap[x] for x in d["depends_on"]),
            confidence=d.get("confidence", 0.6),
        ))
    return steps


async def plan_project(
    router: Any,
    objective: str,
    *,
    project_id: str = "",
    context: str = "",
    skills_block: str = "",
    self_brief: str = "",
    boundaries: str = "",
    max_steps: Optional[int] = None,
) -> List[Step]:
    """One LLM planning pass -> validated Steps. Empty list on any failure."""
    if router is None or not (objective or "").strip():
        return []
    cap = max_steps or projects_max_steps()
    user_parts = [f"OBJECTIVE:\n{objective.strip()[:1500]}"]
    if context:
        user_parts.append(f"CONTEXT:\n{str(context)[:2000]}")
    try:
        from colony_sidecar.cognition.charter import build_system_prompt
        system = build_system_prompt(
            "planner",
            self_brief=(self_brief or None),
            boundaries=(boundaries or None),
            skills=(skills_block or None),
            extra=_ACTION_KINDS_CONTEXT,
            max_steps=cap)
        response = await router.complete(
            [{"role": "system", "content": system},
             {"role": "user", "content": "\n\n".join(user_parts)}],
            context={"task": "project_planning"})
        content = getattr(response, "content", "") or ""
    except Exception as exc:
        logger.warning("project planning LLM call failed: %s", exc)
        return []
    return validate_steps(_parse_array(content), project_id=project_id,
                          max_steps=cap)
