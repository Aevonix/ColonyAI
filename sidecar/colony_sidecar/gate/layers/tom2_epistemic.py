"""tom2_epistemic — the egress net under leveled cross-contact tom2 (L3.2).

When a level-2 epistemic line is injected into a conversation's context,
the wiring registers a TAINT (gate/taint.py, TTL'd). While any taint is
live, this check hard-blocks a reply that:

  (a) names a tainted SUBJECT inside an epistemic-claim pattern
      ("knows / doesn't know / hasn't heard / unaware / no idea /
      don't tell / keep it from / hasn't been told") — voicing the
      silent prior is the T4 elicitation leak;
  (b) makes a self-referential modeling claim ("I track what people
      know", "epistemic prior", ...) — narrating THAT the system models
      people is itself a disclosure while a prior is hot;
  (c) carries a tainted fact's TEXT (resolved from the fact ref at check
      time, never stored) toward a DIFFERENT conversation than the one
      the taint was registered for — injected content spilling contexts.

INERT BY CONSTRUCTION: with no active taint the check answers [] after a
single in-memory clock comparison (TaintRegistry.any_active watermark) —
zero cost and zero false positives for every turn that never saw a level-2
injection. That inertness is why this check ships on the DEFAULT enforce
allowlist from day one: it cannot block anything until the level-2 flip
(L4.2) actually registers taints.

HONEST LIMITATION (test-documented): (a) and (b) are lexical. A paraphrase
("Bob is still in the dark about it") escapes them. The structural
guarantees live UPSTREAM — the renderer can only inject fact text the
reader already owns — so an escape here leaks phrasing pressure, not new
content. This net narrows the residual, it does not close it.
"""

from __future__ import annotations

import logging
import re
from typing import Any, List

from colony_sidecar.gate.context_provenance import normalize_entity
from colony_sidecar.gate.response_guard import GuardFinding

logger = logging.getLogger(__name__)

CHECK_NAME = "tom2_epistemic"

#: How close (chars, in normalized text) an epistemic pattern must sit to a
#: tainted subject name to count as a claim ABOUT that subject.
_PROXIMITY = 100

#: Minimum normalized fact-text length for the (c) spill check — shorter
#: strings collide with ordinary prose too easily to be evidence.
_MIN_FACT_LEN = 8

_EPISTEMIC_RE = re.compile(
    r"(?:doesn't|does not|don't|didn't|won't|wouldn't) know"
    r"|hasn't (?:heard|been told)"
    r"|has not (?:heard|been told)"
    r"|haven't (?:heard|been told)"
    r"|\bknows?\b"
    r"|\bunaware\b"
    r"|no idea"
    r"|keep (?:it|this|that) from"
    r"|(?:don't|do not) tell"
    r"|hidden from|in the loop"
)

_SELF_MODEL_RE = re.compile(
    r"i (?:keep )?track (?:of )?(?:what|who)"
    r"|i model (?:what|who|people)"
    r"|i (?:keep|maintain|have) a (?:mental )?model of"
    r"|my (?:mental )?model of (?:who|what)"
    r"|i know who knows"
    r"|epistemic prior"
    r"|silent prior"
)


def _norm(text: str) -> str:
    # normalize_entity gives NFKC + lower + strip; fold curly apostrophes so
    # "doesn’t" matches the same pattern as "doesn't".
    return normalize_entity(text).replace("’", "'")


def _near_pattern(text: str, name: str) -> bool:
    """True when an epistemic-claim pattern sits within _PROXIMITY chars of
    any occurrence of ``name`` in ``text`` (both already normalized)."""
    start = 0
    while True:
        i = text.find(name, start)
        if i < 0:
            return False
        window = text[max(0, i - _PROXIMITY): i + len(name) + _PROXIMITY]
        if _EPISTEMIC_RE.search(window):
            return True
        start = i + len(name)


class Tom2EpistemicGuard:
    """Block-severity egress check keyed off the taint registry.

    ``taints`` is a TaintRegistry (or None => permanently inert);
    ``facts_store`` resolves fact refs to text for the (c) spill check at
    CHECK time only (fact text is never persisted by this layer)."""

    def __init__(self, taints: Any, facts_store: Any = None) -> None:
        self._taints = taints
        self._facts = facts_store

    async def check(self, *, response_text: str,
                    conversation_key: Any = None) -> List[GuardFinding]:
        """Never raises; any internal error returns [] (the guard's own
        fail-open contract — the taint layer narrows risk, it must not
        silence the agent on its own faults)."""
        try:
            return self._check(response_text or "",
                               str(conversation_key or ""))
        except Exception:
            logger.warning("tom2_epistemic check failed (skipped)",
                           exc_info=True)
            return []

    # -- internals ------------------------------------------------------------
    def _check(self, response_text: str,
               conversation_key: str) -> List[GuardFinding]:
        if self._taints is None or not self._taints.any_active():
            return []                       # the inert fast path
        text = _norm(response_text)
        if not text:
            return []
        findings: List[GuardFinding] = []

        # (b) self-referential modeling claims — once per reply.
        m = _SELF_MODEL_RE.search(text)
        if m:
            findings.append(GuardFinding(
                check=CHECK_NAME, severity="block",
                reason="self-referential modeling claim while an epistemic "
                       "injection is live",
                excerpt=f"[{m.group(0)[:60]}]"))

        flagged_subjects: set = set()
        flagged_refs: set = set()
        for taint in self._taints.all_active():
            # (a) tainted subject named inside an epistemic-claim pattern —
            # in ANY conversation while the taint is hot.
            subject = str(taint.get("subject_contact_id") or "")
            if subject not in flagged_subjects:
                for name in list(taint.get("subject_names") or []):
                    name = _norm(str(name))
                    if len(name) >= 3 and _near_pattern(text, name):
                        findings.append(GuardFinding(
                            check=CHECK_NAME, severity="block",
                            reason="reply voices an epistemic claim about a "
                                   "subject with a live level-2 injection",
                            excerpt=f"[{name}]"))
                        flagged_subjects.add(subject)
                        break

            # (c) tainted fact text spilling toward a DIFFERENT conversation.
            ref = str(taint.get("fact_ref") or "")
            if (self._facts is None or not ref or ref in flagged_refs
                    or conversation_key == str(
                        taint.get("conversation_key") or "")):
                continue
            try:
                fact = self._facts.get_fact(ref)
            except Exception:
                continue
            fact_text = _norm(str((fact or {}).get("fact") or ""))
            if len(fact_text) >= _MIN_FACT_LEN and fact_text in text:
                findings.append(GuardFinding(
                    check=CHECK_NAME, severity="block",
                    reason="tainted fact content surfacing in a different "
                           "conversation than its injection",
                    excerpt="[tainted fact text]"))
                flagged_refs.add(ref)
        return findings
