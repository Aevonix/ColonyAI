"""DirectiveGuard -- enforce owner boundaries before any autonomous action.

Given a proposed action (deliver a message, execute a tool, run a directed
task, generate an initiative), the guard checks it against every active
PROHIBIT directive and returns a Verdict. Callers at each action chokepoint
(executor, delivery, generation, directed-action) consult it and REFUSE when
a boundary is violated.

Matching is deterministic and biased toward catching real violations on
SPECIFIC subjects (repo names, business names, proper nouns) while avoiding
false blocks on generic words. This hard, code-level gate is paired with
soft LLM awareness (active directives injected into reasoning context), so a
model can neither forget nor talk its way past a standing boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from colony_sidecar.directives.models import (
    Directive, Polarity, normalize_terms,
)

logger = logging.getLogger(__name__)


@dataclass
class Action:
    """A proposed autonomous action to be checked against boundaries."""

    kind: str                       # deliver | execute_tool | directed_action | generate | research
    text: str = ""                  # description / message / topic
    target: str = ""                # recipient / repo / business / subject
    entity_id: str = ""             # optional graph entity id
    tool_name: str = ""
    args: Optional[Dict[str, Any]] = None
    high_risk: bool = False         # outbound / mutating / directed -> fail-closed on ambiguity

    def searchable_terms(self) -> List[str]:
        parts = [self.text, self.target, self.tool_name]
        if self.args:
            for v in self.args.values():
                if isinstance(v, (str, int, float)):
                    parts.append(str(v))
        toks = set()
        for p in parts:
            toks.update(normalize_terms(p))
        return list(toks)


@dataclass
class Verdict:
    allowed: bool
    violations: List[Directive] = field(default_factory=list)
    reason: str = "ok"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "violations": [
                {"id": d.id, "subject": d.subject, "raw_text": d.raw_text}
                for d in self.violations
            ],
        }


def _is_distinctive(term: str) -> bool:
    """A term specific enough that a single match is a real signal."""
    return (
        len(term) >= 5
        or any(c in term for c in "-_/.")
        or any(c.isdigit() for c in term)
    )


def _terms_match(directive_terms: List[str], action_terms: List[str]) -> bool:
    """True if the directive's subject matches the action's terms."""
    if not directive_terms:
        return False
    action = set(action_terms)

    def _hit(t: str) -> bool:
        if t in action:
            return True
        # loose stem/prefix containment for morphological variants
        # (research/researching, cert/certs, deploy/deployment)
        if len(t) >= 4:
            for a in action:
                if a == t:
                    return True
                if len(a) >= 4 and (a.startswith(t) or t.startswith(a)):
                    return True
        return False

    hits = [t for t in directive_terms if _hit(t)]
    if not hits:
        return False
    # A distinctive subject term (repo/business/proper-noun-like) matching alone
    # is a real signal. Generic short terms require the whole subject to match.
    if any(_is_distinctive(t) for t in hits):
        return True
    return len(hits) == len(directive_terms)


class DirectiveGuard:
    """Consults the directive store and enforces PROHIBIT boundaries."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def _active_prohibitions(self) -> List[Directive]:
        if self._store is None:
            return []
        try:
            return self._store.active(polarity=Polarity.PROHIBIT)
        except Exception as exc:  # a store failure must fail CLOSED for high risk
            logger.warning("DirectiveGuard: store read failed: %s", exc)
            return []

    def check(self, action: Action) -> Verdict:
        """Return a Verdict. allowed=False means the action violates a boundary."""
        prohibitions = self._active_prohibitions()
        if not prohibitions:
            return Verdict(allowed=True, reason="no_active_boundaries")

        action_terms = action.searchable_terms()
        action_entities = {action.entity_id} if action.entity_id else set()
        violations: List[Directive] = []
        for d in prohibitions:
            # scope by action kind if the directive restricts kinds
            if d.action_kinds and action.kind not in d.action_kinds:
                continue
            if action_entities and set(d.entity_ids) & action_entities:
                violations.append(d)
                continue
            if _terms_match(d.match_terms, action_terms):
                violations.append(d)

        if violations:
            subjects = "; ".join(v.subject for v in violations)
            logger.warning(
                "DirectiveGuard BLOCKED %s action (%r): violates boundary(ies): %s",
                action.kind, (action.text or action.target)[:80], subjects,
            )
            return Verdict(allowed=False, violations=violations,
                           reason=f"boundary_violation: {subjects}")
        return Verdict(allowed=True, reason="ok")

    def obligations(self) -> List[Directive]:
        """Active REQUIRE directives (standing obligations)."""
        if self._store is None:
            return []
        try:
            return self._store.active(polarity=Polarity.REQUIRE)
        except Exception:
            return []

    def context_brief(self, limit: int = 20) -> str:
        """Render active boundaries/obligations for injection into reasoning."""
        if self._store is None:
            return ""
        try:
            active = self._store.active()
        except Exception:
            return ""
        if not active:
            return ""
        lines: List[str] = []
        prohibits = [d for d in active if d.polarity == Polarity.PROHIBIT][:limit]
        requires = [d for d in active if d.polarity == Polarity.REQUIRE][:limit]
        if prohibits:
            lines.append("MUST NOT (standing boundaries from the owner):")
            for d in prohibits:
                lines.append(f"  - {d.raw_text or d.subject}")
        if requires:
            lines.append("MUST (standing obligations from the owner):")
            for d in requires:
                lines.append(f"  - {d.raw_text or d.subject}")
        return "\n".join(lines)
