"""Directive / boundary data model.

A Directive is a durable standing instruction from the owner, captured from
conversation or set explicitly. It has a polarity:

* PROHIBIT -- "don't / avoid / ignore / stop / leave alone X". A binding
  boundary: autonomous actions that match it are REFUSED.
* REQUIRE  -- "always / make sure to / from now on do X". A standing
  obligation surfaced to the reasoner and the initiative layer.
* PREFER   -- a soft preference (context only, never blocks).

Directives are deployment-agnostic: the subject text and match terms come
from the owner's own words / config, never hardcoded here.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Polarity(str, Enum):
    PROHIBIT = "prohibit"
    REQUIRE = "require"
    PREFER = "prefer"


class DirectiveStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


# Generic tokens that carry no discriminating meaning for subject matching.
_STOPWORDS = frozenset({
    "the", "a", "an", "to", "of", "on", "in", "for", "with", "about", "at",
    "my", "your", "me", "you", "it", "that", "this", "any", "some", "and",
    "or", "please", "just", "really", "anymore", "again", "do", "not", "dont",
    "don", "ever", "stop", "avoid", "ignore", "never", "leave", "alone",
    "touch", "is", "are", "be", "when", "if", "should", "would", "can",
    # generic filler verbs / quantifiers that carry no subject meaning
    "track", "tracking", "anything", "everything", "something", "stuff",
    "thing", "things", "worry", "worrying", "bother", "bothering", "mention",
    "mentioning", "care", "dealing", "deal", "regarding", "worried",
})

_WORD = re.compile(r"[a-z0-9][a-z0-9_+.\-/]*")


def normalize_terms(text: Optional[str]) -> List[str]:
    """Lowercase significant tokens from a subject/target string."""
    if not text:
        return []
    toks = _WORD.findall(str(text).lower())
    return [t for t in toks if t not in _STOPWORDS and len(t) > 1]


@dataclass
class Directive:
    """A standing owner directive / boundary."""

    subject: str                     # human-readable subject ("repo colony-web")
    polarity: Polarity = Polarity.PROHIBIT
    raw_text: str = ""               # the owner's original words
    match_terms: List[str] = field(default_factory=list)   # normalized subject tokens
    entity_ids: List[str] = field(default_factory=list)    # optional graph entity ids
    action_kinds: List[str] = field(default_factory=list)  # limit to kinds; empty = all
    source: str = "owner_explicit"   # owner_explicit | inferred | config
    confidence: float = 0.9
    status: DirectiveStatus = DirectiveStatus.ACTIVE
    id: str = field(default_factory=lambda: f"dir-{uuid.uuid4().hex[:12]}")
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None

    def __post_init__(self) -> None:
        if isinstance(self.polarity, str):
            self.polarity = Polarity(self.polarity)
        if isinstance(self.status, str):
            self.status = DirectiveStatus(self.status)
        if not self.match_terms:
            self.match_terms = normalize_terms(self.subject)

    def is_active(self, now: Optional[float] = None) -> bool:
        if self.status != DirectiveStatus.ACTIVE:
            return False
        if self.expires_at is not None and (now or time.time()) > self.expires_at:
            return False
        return True

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "polarity": self.polarity.value,
            "raw_text": self.raw_text,
            "match_terms": " ".join(self.match_terms),
            "entity_ids": " ".join(self.entity_ids),
            "action_kinds": " ".join(self.action_kinds),
            "source": self.source,
            "confidence": self.confidence,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Directive":
        def _split(v: Any) -> List[str]:
            return [t for t in str(v or "").split(" ") if t]
        return cls(
            id=row["id"],
            subject=row["subject"],
            polarity=Polarity(row["polarity"]),
            raw_text=row.get("raw_text", "") or "",
            match_terms=_split(row.get("match_terms")),
            entity_ids=_split(row.get("entity_ids")),
            action_kinds=_split(row.get("action_kinds")),
            source=row.get("source", "owner_explicit") or "owner_explicit",
            confidence=float(row.get("confidence", 0.9) or 0.9),
            status=DirectiveStatus(row.get("status", "active")),
            created_at=float(row.get("created_at") or time.time()),
            updated_at=float(row.get("updated_at") or time.time()),
            expires_at=(float(row["expires_at"]) if row.get("expires_at") not in (None, "") else None),
        )
