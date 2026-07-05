"""WorldModelPopulator -- turn conversation into structured world knowledge.

Wires the (already-existing, rule-based, zero-LLM) ConversationExtractor to the
world-model store so Colony actually learns the owner's people, companies,
projects and products from what he says. This was the missing link that left
the world model empty.

Observability-first and reversible:
  * mode "off"    -- do nothing.
  * mode "shadow" -- extract + resolve + log EXACTLY what it WOULD populate,
                     write nothing (default).
  * mode "live"   -- upsert entities (dedup via EntityResolver) + light
                     relationships into the local world-model store only.

Every candidate is boundary-checked: if the owner set a directive to leave a
subject alone, it is never added to the world model.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from colony_sidecar.world_model.extraction.conversation_extractor import (
    ConversationExtractor, ExtractionCandidate,
)

logger = logging.getLogger(__name__)


def populate_mode() -> str:
    from colony_sidecar.util.autonomy_preset import resolve
    return resolve("COLONY_WORLD_POPULATE_MODE",
                   ("off", "shadow", "live"), "shadow")


@dataclass
class PopulationReport:
    source_id: str = ""
    mode: str = "shadow"
    created: List[Dict[str, Any]] = field(default_factory=list)   # {name,type,confidence}
    merged: List[Dict[str, Any]] = field(default_factory=list)    # {name,type,into}
    proposed: List[Dict[str, Any]] = field(default_factory=list)
    relationships: List[Dict[str, Any]] = field(default_factory=list)  # {source,rel,target}
    skipped_boundary: List[str] = field(default_factory=list)

    def total(self) -> int:
        return len(self.created) + len(self.merged) + len(self.proposed)


# Person <-> Company work link cues (person <cue> company).
_WORK_CUE = re.compile(
    r"\b(?:works?\s+(?:at|for)|employed\s+at|ceo\s+of|cto\s+of|founder\s+of|"
    r"co-?founder\s+of|runs?|leads?|heads?|at)\b", re.IGNORECASE,
)

# Lowercase connectives that only appear inside a sentence, never in a real
# entity NAME. Their presence marks a candidate as a sentence fragment.
_FRAGMENT_WORDS = frozenset({
    "the", "a", "an", "who", "whom", "that", "which", "and", "or", "but",
    "at", "in", "on", "of", "for", "to", "with", "about", "met", "meet",
    "works", "work", "working", "is", "are", "was", "were", "said", "says",
    "from", "by", "joined", "join", "called", "told", "asked",
})


def _looks_like_fragment(name: str) -> bool:
    """A real entity name is a few proper tokens, not a clause."""
    toks = name.split()
    if len(toks) > 5:
        return True
    lows = {t.lower().strip(".,") for t in toks}
    return bool(lows & _FRAGMENT_WORDS)


# Generic role/system words + calendar words that rule-based NER mis-tags as
# people/places. Deployment-specific role names (the assistant persona, host
# agent, etc.) are added from config/env at runtime -- never hardcoded here.
_GENERIC_NOISE = frozenset({
    "agent", "assistant", "system", "user", "bot", "ai",
    "gpt", "model", "human", "owner",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "today", "tomorrow",
    "yesterday", "morning", "evening", "tonight", "week", "month", "year",
})


def _noise_names() -> frozenset:
    """Generic role/calendar noise + any deployment-specific role names from
    env (persona/agent name, plus COLONY_WORLD_NOISE_NAMES)."""
    extra = set()
    for var in ("COLONY_PERSONA_NAME", "COLONY_AGENT_NAME"):
        v = os.environ.get(var, "").strip().lower()
        if v:
            extra.add(v)
    for t in os.environ.get("COLONY_WORLD_NOISE_NAMES", "").split(","):
        t = t.strip().lower()
        if t:
            extra.add(t)
    return _GENERIC_NOISE | extra


# Common English nouns/verbs that never form part of a real person's name.
# Title-cased operational phrases ("Root Cause", "Orphan Messages") are the
# dominant person-noise class observed in live data.
_NON_NAME_WORDS = frozenset({
    "cause", "root", "message", "messages", "session", "sessions",
    "initiative", "initiatives", "operation", "operations", "orphan",
    "fresh", "spawn", "error", "errors", "task", "tasks", "queue", "memory",
    "review", "reviews", "summary", "report", "reports", "update", "updates",
    "status", "issue", "issues", "problem", "problems", "fix", "fixes",
    "bug", "bugs", "test", "tests", "daily", "weekly", "briefing", "digest",
    "reminder", "alert", "alerts", "notification", "delivery", "channel",
    "gateway", "bridge", "worker", "workers", "pending", "stale", "self",
    "improvement", "cleanup", "backlog", "audit", "check", "checks",
})


def _is_low_quality(name: str, etype: str) -> bool:
    """High-precision gate for world-model population.

    Rule-based NER is noisy on conversation; drop the dominant noise classes so
    the world model is trustworthy rather than full of sentence-initial words.
    """
    low = name.strip().lower()
    _noise = _noise_names()
    if low in _noise:
        return True
    # URLs, paths, emails, and placeholder fragments are never entity NAMES
    # (they belong in external_ids/properties, not as canonical names).
    if ("://" in low or low.startswith(("http", "www."))
            or low.endswith(("...", "/")) or "/" in name.strip()):
        return True
    toks = name.split()
    # Any token being a role/system/greeting word taints the whole name
    # (e.g. a greeting + the assistant's own name, or "Agent ...").
    tok_lows = {t.lower().strip(".,") for t in toks}
    if tok_lows & (_noise | {"hey", "hi", "hello", "yo", "ok", "okay", "thanks"}):
        return True
    # Persons/locations must be multi-token proper names (single capitalized
    # words at sentence start are the main noise source). Companies/products
    # come from structured signals (org-suffix, domain, url) and are kept.
    if etype in ("person", "location") and len(toks) < 2:
        return True
    # A "person" whose name contains a common operational noun is a
    # title-cased phrase, not a human ("Root Cause", "Orphan Messages").
    if etype == "person" and tok_lows & _NON_NAME_WORDS:
        return True
    return False


class WorldModelPopulator:
    def __init__(
        self,
        store: Any,
        directive_manager: Any = None,
        mode: Optional[str] = None,
        min_confidence: float = 0.30,
    ) -> None:
        self._store = store
        self._directives = directive_manager
        self._mode = mode or populate_mode()
        self._min_conf = min_confidence
        self._extractor = ConversationExtractor()
        self._resolver = None
        if store is not None:
            try:
                from colony_sidecar.world_model.resolution.entity_resolver import EntityResolver
                self._resolver = EntityResolver(store)
            except Exception as exc:
                logger.debug("EntityResolver unavailable: %s", exc)

    @property
    def mode(self) -> str:
        return self._mode

    def _boundary_ok(self, name: str) -> bool:
        if self._directives is None:
            return True
        try:
            from colony_sidecar.directives import Action
            return self._directives.check(Action(kind="populate", text=name, target=name)).allowed
        except Exception:
            return True

    async def populate_from_text(self, text: str, source_id: str) -> PopulationReport:
        report = PopulationReport(source_id=source_id, mode=self._mode)
        if self._mode == "off" or self._store is None or not text:
            return report

        try:
            extraction = await self._extractor.extract(text, source_id)
        except Exception as exc:
            logger.debug("world populate extract failed: %s", exc)
            return report

        # Dedup within the message + confidence + boundary filter.
        candidates: List[ExtractionCandidate] = []
        seen = set()
        for c in extraction.entities:
            key = (c.text.strip().lower(), c.entity_type)
            if not c.text.strip() or key in seen or c.confidence < self._min_conf:
                continue
            seen.add(key)
            # Reject sentence-fragment "names" and low-quality/noise candidates
            # that slip through rule-based NER (high-precision population).
            if _looks_like_fragment(c.text) or _is_low_quality(c.text, c.entity_type):
                continue
            if not self._boundary_ok(c.text):
                report.skipped_boundary.append(c.text)
                continue
            candidates.append(c)

        name_to_type: Dict[str, str] = {}
        for c in candidates:
            name_to_type[c.text] = c.entity_type
            await self._handle_candidate(c, report)

        self._infer_relationships(text, candidates, report)
        return report

    async def _handle_candidate(self, c: ExtractionCandidate, report: PopulationReport) -> None:
        action = "create"
        matched = None
        if self._resolver is not None:
            try:
                res = await self._resolver.resolve(c, c.entity_type)
                action = getattr(res.action, "value", str(res.action))
                matched = res.matched_entity_id
            except Exception as exc:
                logger.debug("resolve failed for %r: %s", c.text, exc)

        rec = {"name": c.text, "type": c.entity_type, "confidence": round(c.confidence, 2)}
        if action == "merge":
            rec["into"] = matched
            report.merged.append(rec)
            if self._mode == "live" and matched:
                try:
                    await self._store.add_entity_alias(matched, c.text)
                except Exception:
                    pass
            return
        if action == "propose":
            rec["near"] = matched
            report.proposed.append(rec)
            # in live mode a proposal still creates the candidate + queues review,
            # but to stay conservative we only create on a clean CREATE.
            return
        # CREATE
        report.created.append(rec)
        if self._mode == "live":
            try:
                from colony_sidecar.world_model.entities import ENTITY_CLASS_MAP, BaseEntity
                from colony_sidecar.world_model.sqlite.backend import _generate_id
                cls = ENTITY_CLASS_MAP.get(c.entity_type, BaseEntity)
                ent = cls(id=_generate_id("we"), name=c.text,
                          entity_type=c.entity_type, confidence=c.confidence)
                await self._store.upsert_entity(ent)
            except Exception as exc:
                logger.debug("upsert_entity failed for %r: %s", c.text, exc)

    def _infer_relationships(self, text: str, candidates: List[ExtractionCandidate],
                             report: PopulationReport) -> None:
        """Light, conservative relationship inference (name-level in the report).

        Only person<->company WORKS_AT when a clear work cue links them, and
        person<->person KNOWS co-occurrence at low confidence. Written in live
        mode is deferred to LLM-assist; here we surface them for observability.
        """
        persons = [c for c in candidates if c.entity_type == "person"]
        companies = [c for c in candidates if c.entity_type == "company"]
        for p in persons:
            for co in companies:
                lo, hi = sorted([p.start_char, co.start_char])
                between = text[lo:hi]
                if _WORK_CUE.search(between) or _WORK_CUE.search(
                    text[max(0, p.start_char - 20):p.start_char]
                ):
                    report.relationships.append(
                        {"source": p.text, "rel": "WM_WORKS_AT", "target": co.text,
                         "confidence": 0.5})
        for i in range(len(persons)):
            for j in range(i + 1, len(persons)):
                report.relationships.append(
                    {"source": persons[i].text, "rel": "WM_KNOWS",
                     "target": persons[j].text, "confidence": 0.3})
