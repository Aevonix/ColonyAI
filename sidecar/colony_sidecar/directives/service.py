"""DirectiveManager -- capture, store, revoke, and enforce owner boundaries.

The single entry point the rest of Colony uses: the turn pipeline calls
``capture_from_message`` on owner messages; the executor / delivery /
generation chokepoints call ``guard.check(action)``; the context assembler
calls ``guard.context_brief()``.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from colony_sidecar.directives.extractor import extract_directives, is_revocation
from colony_sidecar.directives.guard import DirectiveGuard, Action, Verdict
from colony_sidecar.directives.models import (
    Directive, Level, Polarity, normalize_terms,
)
from colony_sidecar.directives.store import DirectiveStore

logger = logging.getLogger(__name__)

# Affirmations that confirm a pending boundary lift. Deliberately short/explicit.
_AFFIRM = re.compile(
    r"^\s*(?:yes|yep|yeah|yup|confirm(?:ed)?|do it|go ahead|correct|affirmative|"
    r"sure|ok(?:ay)?|please do|resume it|lift it)\b", re.IGNORECASE,
)
# How long a pending confirmation stays valid (owner must confirm promptly).
_PENDING_TTL_SECS = 900.0


@dataclass
class CaptureResult:
    captured: List[Directive] = field(default_factory=list)
    revoked: List[Directive] = field(default_factory=list)
    ack: Optional[str] = None                 # confirmation echo for the owner
    needs_confirmation: Optional[str] = None  # a lift awaiting explicit confirmation

    def any(self) -> bool:
        return bool(self.captured or self.revoked or self.ack or self.needs_confirmation)


class DirectiveManager:
    def __init__(self, store: DirectiveStore) -> None:
        self.store = store
        self.guard = DirectiveGuard(store)
        # Pending boundary lift awaiting explicit owner confirmation (1c).
        self._pending_lift: Optional[dict] = None
        # One-shot acknowledgment to echo back to the owner (1a).
        self._last_ack: Optional[str] = None

    # -- capture -------------------------------------------------------
    def capture_from_message(self, message: str, *, source: str = "owner_explicit") -> CaptureResult:
        """Extract directives from an OWNER message and persist them.

        Asymmetric friction (1c): SETTING a boundary is one-turn easy; LIFTING
        one is staged as a pending confirmation and only applied when the owner
        explicitly confirms on a subsequent message. Caller MUST gate to owner.
        """
        result = CaptureResult()
        text = (message or "").strip()

        # 1) If a lift is pending, ONLY an explicit affirmation on the immediate
        #    next message confirms it; anything else clears it (fail-safe: a
        #    prohibition is never lifted by an attribution error or stray text).
        if self._pending_lift is not None:
            expired = time.time() - self._pending_lift["ts"] > _PENDING_TTL_SECS
            if not expired and _AFFIRM.match(text):
                revoked = self._apply_revocation_ids(self._pending_lift["ids"])
                subj = self._pending_lift["subject"]
                self._pending_lift = None
                result.revoked = revoked
                result.ack = f"Confirmed. I will resume {subj}."
                self._last_ack = result.ack
                return result
            # not confirmed -> the boundary stays; drop the pending lift
            self._pending_lift = None

        found = extract_directives(text, source=source)

        # 2) A revocation stages a pending confirmation (never lifts immediately).
        revocations = [d for d in found if is_revocation(d)]
        if revocations:
            rev = revocations[0]
            terms = set(rev.match_terms or normalize_terms(rev.subject))
            matches = [d for d in self.store.active(polarity=Polarity.PROHIBIT)
                       if set(d.match_terms) & terms]
            if matches:
                self._pending_lift = {
                    "subject": rev.subject, "ids": [d.id for d in matches],
                    "ts": time.time(),
                }
                subs = "; ".join(d.raw_text or d.subject for d in matches)
                result.needs_confirmation = (
                    f"You asked me to resume: {rev.subject}. That lifts a boundary "
                    f"you set ({subs}). Confirm and I will resume it; otherwise it stays in place."
                )
            return result

        # 3) New prohibitions / requirements are stored immediately.
        for d in found:
            self.store.add(d)
            result.captured.append(d)
        if result.captured:
            parts = []
            for d in result.captured:
                # The echo STATES the interpretation (tiered semantics) so the
                # owner can correct it in one turn.
                if d.polarity != Polarity.PROHIBIT:
                    parts.append(f"I will make sure to {d.subject}")
                elif d.level == Level.OBSERVE:
                    parts.append(
                        f"full blackout on {d.subject}: I will not act on it "
                        "or look at it")
                else:
                    parts.append(
                        f"I will stop acting on {d.subject}. I will still keep "
                        "awareness of it; say 'don't even look at it' for a "
                        "full blackout")
            result.ack = "Noted: " + "; ".join(parts) + "."
            self._last_ack = result.ack
        return result

    async def capture_llm(self, message: str) -> List[Directive]:
        """LLM-assisted capture (1b), run only when the deterministic pass found
        nothing. Inferred directives are lower-confidence and surfaced for the
        owner to correct."""
        from colony_sidecar.directives.extractor import llm_extract_directives
        found = await llm_extract_directives(message)
        stored: List[Directive] = []
        for d in found:
            self.store.add(d)
            stored.append(d)
            verb = "will not" if d.polarity == Polarity.PROHIBIT else "will make sure to"
            self._last_ack = (f"I inferred a standing instruction: I {verb} {d.subject}. "
                              "Tell me if that is wrong.")
        return stored

    def consume_ack(self) -> Optional[str]:
        """Return and clear the one-shot acknowledgment (echoed once)."""
        ack, self._last_ack = self._last_ack, None
        return ack

    def pending_confirmation(self) -> Optional[str]:
        """The current pending boundary-lift prompt, if any (for context echo)."""
        if self._pending_lift is None:
            return None
        if time.time() - self._pending_lift["ts"] > _PENDING_TTL_SECS:
            self._pending_lift = None
            return None
        subj = self._pending_lift["subject"]
        return f"AWAITING CONFIRMATION: the owner asked to resume {subj}; do not resume until they confirm."

    def _apply_revocation_ids(self, ids: List[str]) -> List[Directive]:
        revoked = []
        for did in ids:
            d = self.store.get(did)
            if d and self.store.revoke(did):
                revoked.append(d)
                logger.info("Directive revoked by owner (confirmed): %r (id=%s)", d.subject, did)
        return revoked

    def add_explicit(self, subject: str, polarity: str = "prohibit",
                     raw_text: str = "", source: str = "owner_explicit",
                     entity_ids: Optional[List[str]] = None) -> Directive:
        d = Directive(
            subject=subject, polarity=Polarity(polarity), raw_text=raw_text or subject,
            source=source, entity_ids=entity_ids or [],
        )
        return self.store.add(d)

    # -- boundary-respecting critical flag (high-severity path) ---------
    def set_delivery_router(self, router: Any) -> None:
        """Async callable(payload)->bool routing through the guarded reach-out
        path. Used ONLY for the once-per-boundary critical flag."""
        self._delivery_router = router

    async def flag_critical(self, subject: str, finding: str,
                            severity: float = 0.9) -> dict:
        """Under an ACT boundary, reflection may surface something critical
        (security vulnerability, data loss, financial risk). Correct behavior:
        an internal note plus AT MOST ONE flag surfaced to the owner through
        the guarded reach-out path, clearly marked as boundary-respecting.
        Never silent action; never silent omission of a critical fact.
        """
        import os
        try:
            threshold = float(os.environ.get("COLONY_BOUNDARY_FLAG_MIN_SEVERITY", "0.85"))
        except (TypeError, ValueError):
            threshold = 0.85
        logger.info("boundary-concern noted (severity=%.2f): %s -- %s",
                    severity, subject, finding[:160])
        if severity < threshold:
            return {"flagged": False, "reason": "below_threshold",
                    "noted_internally": True}

        # Which active prohibition covers this subject?
        terms = set(normalize_terms(subject))
        match = None
        for d in self.store.active(polarity=Polarity.PROHIBIT):
            if set(d.match_terms) & terms:
                match = d
                break
        if match is None:
            return {"flagged": False, "reason": "no_matching_boundary",
                    "noted_internally": True}
        if self.store.has_flag(match.id):
            return {"flagged": False, "reason": "already_flagged_once",
                    "noted_internally": True}
        if not self.store.record_flag(match.id, finding, severity):
            return {"flagged": False, "reason": "already_flagged_once",
                    "noted_internally": True}

        delivered = False
        router = getattr(self, "_delivery_router", None)
        if router is not None:
            try:
                from colony_sidecar.proposals import Proposal, proposal_to_payload
                prop = Proposal(
                    title=f"Boundary-respecting flag: {subject[:60]}",
                    finding=(f"You asked me to leave {match.subject} alone, and I have. "
                             f"But you should know: {finding}"),
                    why_it_helps="a critical fact should never be silently omitted",
                    suggested_action="Tell me whether to keep hands off or investigate.",
                    source=f"boundary-flag:{match.id}",
                    initiative_type="proposal",
                    confidence=min(1.0, max(0.0, severity)),
                )
                delivered = bool(await router(proposal_to_payload(prop)))
            except Exception:
                logger.debug("boundary flag delivery failed", exc_info=True)
        logger.warning(
            "BOUNDARY-RESPECTING CRITICAL FLAG surfaced once for [%s] %r: %s",
            match.id, match.subject, finding[:200],
        )
        return {"flagged": True, "directive_id": match.id, "delivered": delivered}

    # -- entity-scoped matching (2) ------------------------------------
    def set_entity_index(self, entities: dict) -> None:
        """Provide {entity_id: [names/aliases]} so boundaries resolve by entity."""
        self.guard.set_entity_index(entities)

    async def refresh_entity_index(self, world_store: Any, limit: int = 500) -> int:
        """Load entities + aliases from the world model into the guard index."""
        if world_store is None:
            return 0
        try:
            ents = await world_store.find_entities(query="", limit=limit)
        except Exception:
            return 0
        mapping = {}
        for e in ents or []:
            eid = getattr(e, "id", None) or (e.get("id") if isinstance(e, dict) else None)
            name = getattr(e, "name", None) or (e.get("name") if isinstance(e, dict) else None)
            aliases = getattr(e, "aliases", None) or (e.get("aliases") if isinstance(e, dict) else []) or []
            if eid and name:
                mapping[eid] = [name] + list(aliases)
        self.guard.set_entity_index(mapping)
        return len(mapping)

    # -- enforce -------------------------------------------------------
    def check(self, action: Action) -> Verdict:
        return self.guard.check(action)

    def context_brief(self) -> str:
        return self.guard.context_brief()

    def active(self) -> List[Directive]:
        return self.store.active()
