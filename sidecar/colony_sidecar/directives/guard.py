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
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from colony_sidecar.directives.models import (
    GLOBAL_PAUSE_TERM, Directive, Level, Polarity, normalize_terms,
)

logger = logging.getLogger(__name__)

# Capability classes (tiered boundary semantics): boundaries bind ACTION, not
# judgment or perception, unless explicitly about perception. READ kinds stay
# open under an ACT-level boundary and are blocked only by an OBSERVE
# blackout. Active autonomous work (research jobs, delegation, delivery,
# execution) is an ACT capability -- "stop researching X" must stop research
# about X, while awareness/reads of X survive "don't touch X".
_READ_KINDS = frozenset({"repo_read", "read", "recall", "populate", "observe"})


def action_capability(kind: str) -> str:
    return "read" if (kind or "").lower() in _READ_KINDS else "act"


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


def _common_terms() -> frozenset:
    """Terms that appear in virtually every internal action text and must
    never count as a match signal on their own: the product name, the agent's
    own name, plus any deployment-supplied extras (COLONY_MATCH_COMMON_TERMS,
    csv). Live incident 2026-07-05: a fragment boundary whose only surviving
    term was "colony" matched every job that said "report to Colony"."""
    import os
    terms = {"colony"}
    agent = os.environ.get("COLONY_AGENT_NAME", "")
    for tok in re.findall(r"[a-z0-9]+", agent.lower()):
        if len(tok) > 1:
            terms.add(tok)
    extra = os.environ.get("COLONY_MATCH_COMMON_TERMS", "")
    for tok in extra.lower().split(","):
        tok = tok.strip()
        if tok:
            terms.add(tok)
    return frozenset(terms)


def _is_distinctive(term: str) -> bool:
    """A term specific enough that a single match is a real signal.

    The bar is deliberately high (>=8 chars, or a separator/digit shape like
    repo/path/version tokens): mid-length generic words ("attempt",
    "respond", "message") matching alone caused mass false blocks. Shorter
    real subjects still match through the all-terms path below.
    """
    if term in _common_terms():
        return False
    return (
        len(term) >= 8
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
        # Stem/prefix containment for morphological variants only
        # (research/researching, cert/certs, deploy/deployment). The shorter
        # side must be a real stem: >=5 chars, or within 2 chars of the
        # longer word. Without that floor, "what" counted as a variant of
        # "whatsapp" and a WhatsApp boundary matched any sentence containing
        # the word "what" (live false-block, 2026-07-05).
        if len(t) >= 4:
            for a in action:
                if a == t:
                    return True
                if len(a) >= 4 and (a.startswith(t) or t.startswith(a)):
                    shorter, longer = (a, t) if len(a) <= len(t) else (t, a)
                    if len(shorter) >= 5 or len(longer) - len(shorter) <= 2:
                        return True
        return False

    hits = [t for t in directive_terms if _hit(t)]
    if not hits:
        return False
    # A distinctive subject term (repo/business/proper-noun-like) matching alone
    # is a real signal. Generic short terms require the whole subject to match,
    # and an all-terms match must include at least one substantive (len>=4,
    # non-common) term so pronoun-grade fragments can never bind.
    if any(_is_distinctive(t) for t in hits):
        return True
    common = _common_terms()
    if not any(len(t) >= 4 and t not in common for t in hits):
        return False
    return len(hits) == len(directive_terms)


class DirectiveGuard:
    """Consults the directive store and enforces PROHIBIT boundaries."""

    def __init__(self, store: Any) -> None:
        self._store = store
        # Recent refusals, for observability / "why didn't you do X" answers.
        self._recent_blocks: deque = deque(maxlen=100)
        # Entity-scoped matching (2): {frozenset(alias_tokens): entity_id}. Built
        # from the world model as entities are learned; empty -> keyword-only.
        self._entity_index: Dict[frozenset, str] = {}

    def set_entity_index(self, entities: Dict[str, List[str]]) -> None:
        """Refresh the alias->entity_id index. entities = {entity_id: [names/aliases]}."""
        index: Dict[frozenset, str] = {}
        for eid, aliases in (entities or {}).items():
            for alias in aliases:
                toks = frozenset(normalize_terms(alias))
                if toks:
                    index[toks] = eid
        self._entity_index = index

    def _resolve_entities(self, terms: set) -> set:
        """Entity IDs whose alias tokens are all present in `terms`."""
        if not self._entity_index or not terms:
            return set()
        return {eid for alias_toks, eid in self._entity_index.items()
                if alias_toks <= terms}

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
        action_term_set = set(action_terms)
        capability = action_capability(action.kind)

        # Global pause (Amendment 1.5): an active kill-switch boundary refuses
        # every act-capability action immediately, no matching required.
        # Reads/perception stay open (ACT semantics).
        if capability != "read":
            paused = [d for d in prohibitions
                      if GLOBAL_PAUSE_TERM in (d.match_terms or [])]
            if paused:
                d = paused[0]
                action_summary = (action.text or action.target or action.tool_name)[:120]
                logger.warning(
                    "DirectiveGuard GLOBAL PAUSE active [%s]: %s action (%r) "
                    "refused", d.id, action.kind, action_summary,
                )
                self._recent_blocks.append({
                    "ts": time.time(),
                    "action_kind": action.kind,
                    "capability": capability,
                    "action_summary": action_summary,
                    "directive_ids": [d.id],
                    "subjects": [d.subject],
                    "directives": [d.raw_text or d.subject],
                })
                return Verdict(allowed=False, violations=[d],
                               reason="global_pause_active")
        # Entity-scoped: resolve the action's target/terms to entity IDs so a
        # directive about an entity blocks actions naming it by ANY alias, not
        # just a keyword hit. Keyword matching (below) stays as the fallback.
        action_entities = {action.entity_id} if action.entity_id else set()
        action_entities |= self._resolve_entities(action_term_set)
        violations: List[Directive] = []
        for d in prohibitions:
            # Tiered semantics: an ACT-level boundary binds actions only;
            # reads/perception stay open. Only an OBSERVE blackout binds reads.
            if capability == "read" and (d.level or Level.ACT) != Level.OBSERVE:
                continue
            # scope by action kind if the directive restricts kinds
            if d.action_kinds and action.kind not in d.action_kinds:
                continue
            directive_entities = set(d.entity_ids) | self._resolve_entities(set(d.match_terms))
            if action_entities and directive_entities & action_entities:
                violations.append(d)
                continue
            if _terms_match(d.match_terms, action_terms):
                violations.append(d)

        if violations:
            subjects = "; ".join(v.subject for v in violations)
            ids = ",".join(v.id for v in violations)
            action_summary = (action.text or action.target or action.tool_name)[:120]
            if capability == "read":
                # OBSERVE blackout on a read: logged so introspection about the
                # blindspot's existence still works ("not looking, per directive").
                logger.info(
                    "DirectiveGuard: not looking, per directive [%s] -- %s read "
                    "of %r withheld (%s)",
                    ids, action.kind, action_summary, subjects,
                )
            else:
                logger.warning(
                    "DirectiveGuard BLOCKED %s action (%r): violates boundary(ies) "
                    "[%s]: %s",
                    action.kind, action_summary, ids, subjects,
                )
            self._recent_blocks.append({
                "ts": time.time(),
                "action_kind": action.kind,
                "capability": capability,
                "action_summary": action_summary,
                "directive_ids": [v.id for v in violations],
                "subjects": [v.subject for v in violations],
                "directives": [v.raw_text or v.subject for v in violations],
            })
            return Verdict(allowed=False, violations=violations,
                           reason=f"boundary_violation: {subjects}")
        return Verdict(allowed=True, reason="ok")

    def recent_blocks(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Most-recent refusals (for the 'why didn't you do X' introspection)."""
        items = list(self._recent_blocks)[-limit:]
        items.reverse()
        return items

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
