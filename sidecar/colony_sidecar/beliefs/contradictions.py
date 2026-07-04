"""Contradiction detection: same subject + predicate, conflicting value.

Two claim sources:
- World-model entity properties (fully structured; exact).
- Graph memories (free text): a CONSERVATIVE copular extractor pulls
  (subject, predicate, value) triples only from short, unambiguous
  constructions, so detected conflicts are real rather than parser noise.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

from colony_sidecar.beliefs.models import Claim, norm_value

# Conservative copular / relational patterns. Subject <= 4 tokens, sentence
# bounded, value bounded. Anything fancier waits for an LLM pass.
_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # "X's <predicate> is <value>"
    (re.compile(r"^(?P<subj>[A-Z][\w .&-]{1,40}?)'s\s+(?P<pred>[a-z ]{2,25})\s+"
                r"(?:is|are)\s+(?P<val>[^,.;]{1,60})[.,;]?$"), "possessive"),
    # "X works at Y" / "X works for Y"
    (re.compile(r"^(?P<subj>[A-Z][\w .&-]{1,40}?)\s+works?\s+(?:at|for)\s+"
                r"(?P<val>[^,.;]{1,60})[.,;]?$"), "works_at"),
    # "X lives in Y" / "X is based in Y" / "X is located in Y"
    (re.compile(r"^(?P<subj>[A-Z][\w .&-]{1,40}?)\s+(?:lives|resides)\s+in\s+"
                r"(?P<val>[^,.;]{1,60})[.,;]?$"), "location"),
    (re.compile(r"^(?P<subj>[A-Z][\w .&-]{1,40}?)\s+is\s+(?:based|located)\s+in\s+"
                r"(?P<val>[^,.;]{1,60})[.,;]?$"), "location"),
    # "X is the <role> of/at Y" -> predicate role
    (re.compile(r"^(?P<subj>[A-Z][\w .&-]{1,40}?)\s+is\s+the\s+"
                r"(?P<pred>[a-z ]{2,25})\s+(?:of|at)\s+"
                r"(?P<val>[^,.;]{1,60})[.,;]?$"), "role"),
]

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# Values that carry no belief content.
_EMPTY_VALUES = frozenset({"", "unknown", "none", "n a", "na", "null", "tbd"})


def claims_from_text(text: str, *, confidence: float = 0.5, ts: float = 0.0,
                     source: str = "inference", ref: str = "") -> List[Claim]:
    """Conservative triple extraction from one memory's content."""
    out: List[Claim] = []
    for sent in _SENT_SPLIT.split(text or ""):
        sent = sent.strip()
        if not sent or len(sent) > 140:
            continue
        for pat, kind in _PATTERNS:
            m = pat.match(sent)
            if not m:
                continue
            subj = m.group("subj").strip()
            if len(subj.split()) > 4:
                continue
            pred = (m.groupdict().get("pred") or kind).strip().lower()
            val = m.group("val").strip()
            if norm_value(val) in _EMPTY_VALUES:
                continue
            out.append(Claim(subject=subj, predicate=pred, value=val,
                             confidence=confidence, ts=ts, source=source,
                             ref=ref, scope="graph"))
            break
    return out


def property_claims(entity: Any) -> List[Claim]:
    """Structured claims from a world-model entity's properties."""
    out: List[Claim] = []
    props = getattr(entity, "properties", None) or {}
    name = getattr(entity, "name", "") or ""
    eid = getattr(entity, "id", "") or ""
    updated = getattr(entity, "updated_at", None)
    try:
        ts = updated.timestamp() if hasattr(updated, "timestamp") else 0.0
    except Exception:
        ts = 0.0
    base_conf = float(getattr(entity, "confidence", 0.5) or 0.5)
    for key, value in props.items():
        if key.startswith("_conf_") or value in (None, ""):
            continue
        conf = float(props.get(f"_conf_{key}", base_conf) or base_conf)
        out.append(Claim(subject=name or eid, predicate=key, value=str(value),
                         confidence=conf, ts=ts, source="world_model",
                         ref=eid, scope="world_model"))
    return out


def detect_conflicts(claims: List[Claim]) -> List[Tuple[Claim, Claim]]:
    """Pairs of claims sharing subject+predicate with conflicting values.

    Within a key, claims whose values agree (normalized/containment) are
    treated as corroboration, not conflict.
    """
    by_key = {}
    for c in claims or []:
        by_key.setdefault(c.key(), []).append(c)
    out: List[Tuple[Claim, Claim]] = []
    for _key, group in by_key.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if not a.same_value(b):
                    out.append((a, b))
    return out
