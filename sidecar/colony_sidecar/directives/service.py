"""DirectiveManager -- capture, store, revoke, and enforce owner boundaries.

The single entry point the rest of Colony uses: the turn pipeline calls
``capture_from_message`` on owner messages; the executor / delivery /
generation chokepoints call ``guard.check(action)``; the context assembler
calls ``guard.context_brief()``.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from colony_sidecar.directives.extractor import extract_directives, is_revocation
from colony_sidecar.directives.guard import DirectiveGuard, Action, Verdict
from colony_sidecar.directives.models import Directive, Polarity, normalize_terms
from colony_sidecar.directives.store import DirectiveStore

logger = logging.getLogger(__name__)


class DirectiveManager:
    def __init__(self, store: DirectiveStore) -> None:
        self.store = store
        self.guard = DirectiveGuard(store)

    # -- capture -------------------------------------------------------
    def capture_from_message(self, message: str, *, source: str = "owner_explicit") -> List[Directive]:
        """Extract directives from an OWNER message and persist them.

        Revocations are matched against active prohibitions and revoke them
        instead of adding a new row. Caller MUST gate this to owner messages.
        """
        found = extract_directives(message, source=source)
        stored: List[Directive] = []
        for d in found:
            if is_revocation(d):
                self._apply_revocation(d)
                continue
            self.store.add(d)
            stored.append(d)
        return stored

    def _apply_revocation(self, revocation: Directive) -> None:
        """Revoke active prohibitions whose subject the owner just lifted."""
        terms = set(revocation.match_terms or normalize_terms(revocation.subject))
        if not terms:
            return
        for d in self.store.active(polarity=Polarity.PROHIBIT):
            dterms = set(d.match_terms)
            # revoke if the lifted subject shares a distinctive term
            if dterms & terms:
                self.store.revoke(d.id)
                logger.info("Directive revoked by owner: %r (id=%s)", d.subject, d.id)

    def add_explicit(self, subject: str, polarity: str = "prohibit",
                     raw_text: str = "", source: str = "owner_explicit",
                     entity_ids: Optional[List[str]] = None) -> Directive:
        d = Directive(
            subject=subject, polarity=Polarity(polarity), raw_text=raw_text or subject,
            source=source, entity_ids=entity_ids or [],
        )
        return self.store.add(d)

    # -- enforce -------------------------------------------------------
    def check(self, action: Action) -> Verdict:
        return self.guard.check(action)

    def context_brief(self) -> str:
        return self.guard.context_brief()

    def active(self) -> List[Directive]:
        return self.store.active()
