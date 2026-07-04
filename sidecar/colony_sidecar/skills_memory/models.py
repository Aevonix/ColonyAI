"""Skill data model + situation-signature matching."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from colony_sidecar.directives.models import normalize_terms


def situation_signature(text: str) -> str:
    """Normalized, order-stable term signature for a situation description."""
    return " ".join(sorted(set(normalize_terms(text))))


def signature_overlap(sig_a: str, sig_b: str) -> float:
    """Jaccard overlap between two signatures (0..1)."""
    a, b = set((sig_a or "").split()), set((sig_b or "").split())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class Skill:
    title: str
    situation: str = ""                 # when does this apply
    steps: List[str] = field(default_factory=list)
    gotchas: List[str] = field(default_factory=list)
    domain: str = ""                    # initiative type | project | directed
    source_ref: str = ""                # initiative/project/job id it came from
    situation_signature: str = ""       # derived from situation when empty
    uses: int = 0
    wins: int = 0
    losses: int = 0
    confidence: float = 0.6
    id: str = field(default_factory=lambda: f"skl-{uuid.uuid4().hex[:12]}")
    created_at: float = field(default_factory=time.time)
    last_used_at: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.situation_signature:
            self.situation_signature = situation_signature(
                f"{self.title} {self.situation}")

    def score(self, now: Optional[float] = None) -> float:
        """Eviction score: confidence + track record - staleness."""
        now = now or time.time()
        age_days = max(0.0, (now - (self.last_used_at or self.created_at)) / 86400.0)
        return self.confidence + 0.2 * (self.wins - self.losses) - age_days / 180.0

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "situation": self.situation,
            "situation_signature": self.situation_signature,
            "steps": json.dumps(self.steps), "gotchas": json.dumps(self.gotchas),
            "domain": self.domain, "source_ref": self.source_ref,
            "uses": self.uses, "wins": self.wins, "losses": self.losses,
            "confidence": self.confidence, "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "Skill":
        def _j(key):
            try:
                v = json.loads(r.get(key) or "[]")
                return [str(x) for x in v] if isinstance(v, list) else []
            except Exception:
                return []
        return cls(
            id=r["id"], title=r.get("title", "") or "",
            situation=r.get("situation", "") or "",
            situation_signature=r.get("situation_signature", "") or "",
            steps=_j("steps"), gotchas=_j("gotchas"),
            domain=r.get("domain", "") or "", source_ref=r.get("source_ref", "") or "",
            uses=int(r.get("uses") or 0), wins=int(r.get("wins") or 0),
            losses=int(r.get("losses") or 0),
            confidence=float(r.get("confidence", 0.6) or 0.6),
            created_at=float(r.get("created_at") or time.time()),
            last_used_at=(float(r["last_used_at"]) if r.get("last_used_at") else None),
        )
