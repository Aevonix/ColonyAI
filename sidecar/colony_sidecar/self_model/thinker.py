"""The workspace thinker: one bounded LLM reflection on a concern (Mind M2).

Kept separate from workspace.py so the engine stays model-agnostic. Given a
concern and a little real context (recent memory, the concern's own notes),
the LLM returns a small structured judgement: did it make progress, is it
resolved, a one-line note, and optionally an action to take (emit an
initiative, propose an experiment, or write a memory note). In shadow mode
actions are recorded but not executed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_SYSTEM = """You are the reflective inner loop of an autonomous assistant.
You are given ONE concern currently on the assistant's mind, plus any prior
notes. Think about it for a moment and decide what, if anything, moves it
forward. Output ONE JSON object, no prose:

  progress   true if this thought advanced the concern
  resolve    true if the concern is now settled and should leave the mind
  note       one sentence: the substance of the thought (what you concluded)
  action     optional. One of:
             {"kind":"initiative","title":..,"detail":..}  surface something to the owner
             {"kind":"experiment","hypothesis":..,"ref":..,"metric":..}  propose a self-experiment
             {"kind":"memory","content":..}  record a durable note
             {"kind":"none"}  nothing to do yet

Be honest: most single thoughts make partial progress, not resolution. If
there is genuinely nothing to do, say progress=false, resolve=false,
action={"kind":"none"}. Never invent facts; reason only from what is given."""


def build_thinker(router: Any, *, graph: Any = None
                  ) -> Callable[[Any], Awaitable[Dict[str, Any]]]:
    """Return an async thinker(concern) -> outcome dict bound to the router."""

    async def thinker(concern) -> Dict[str, Any]:
        context = ""
        if graph is not None:
            try:
                hits = await graph.recall(concern.summary, limit=4,
                                          min_confidence=0.1)
                if hits:
                    context = "\n".join(
                        f"- {str(h.get('content',''))[:160]}" for h in hits[:4])
            except Exception:
                context = ""
        user = (
            f"Concern ({concern.kind}, salience {concern.salience:.2f}, "
            f"thought {concern.thoughts_spent} of {concern.max_thoughts}):\n"
            f"{concern.summary}\n"
            + (f"\nPrior note: {concern.last_note}\n" if concern.last_note else "")
            + (f"\nRelevant memory:\n{context}\n" if context else ""))
        try:
            resp = await router.complete(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": user}],
                context={"task": "workspace_thinking"})
            return _parse(getattr(resp, "content", "") or "")
        except Exception as exc:
            logger.warning("thinker LLM call failed: %s", exc)
            return {"progress": False, "resolve": False,
                    "note": "", "action": {"kind": "none"}}

    return thinker


def _parse(content: str) -> Dict[str, Any]:
    m = _JSON_RE.search(content)
    if not m:
        return {"progress": False, "resolve": False, "note": "",
                "action": {"kind": "none"}}
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"progress": False, "resolve": False, "note": "",
                "action": {"kind": "none"}}
    return {
        "progress": bool(d.get("progress")),
        "resolve": bool(d.get("resolve")),
        "note": str(d.get("note", ""))[:400],
        "action": d.get("action") if isinstance(d.get("action"), dict)
                  else {"kind": "none"},
    }
