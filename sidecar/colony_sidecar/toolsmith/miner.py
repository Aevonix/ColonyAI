"""Journal miner: turns repeated procedures into tool candidates (Mind M1).

Greenfield per the audit: nothing else aggregates the action journal for
recurrence. A candidate is a cluster of journal entries whose descriptions
normalize to the same shape and recur at least `min_occurrences` times in a
domain, and for which no live tool already exists.
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_NUM_RE = re.compile(r"\b\d+\b")
_HEX_RE = re.compile(r"\b[0-9a-f]{6,}\b")
_WS_RE = re.compile(r"\s+")
_STOP = {"the", "a", "an", "to", "for", "of", "and", "on", "in", "with",
         "is", "was", "at", "by", "this", "that", "it", "from"}


def _normalize(text: str) -> str:
    """Collapse a description to its recurring shape: lowercase, digits and
    hashes masked, stopwords dropped, whitespace squeezed."""
    t = (text or "").lower()
    t = _HEX_RE.sub("#", t)
    t = _NUM_RE.sub("#", t)
    t = _WS_RE.sub(" ", t).strip()
    toks = [w for w in re.findall(r"[a-z#_]+", t) if w not in _STOP]
    return " ".join(toks[:12])


@dataclass
class ToolCandidate:
    signature: str
    domain: str
    description: str            # a representative journal description
    occurrences: int
    evidence: List[str] = field(default_factory=list)   # journal refs/ids
    sample_descriptions: List[str] = field(default_factory=list)


class ToolsmithMiner:
    def __init__(self, journal: Any = None, registry: Any = None) -> None:
        self._journal = journal
        self._registry = registry

    def _j(self) -> Any:
        if self._journal is not None:
            return self._journal
        try:
            from colony_sidecar.api.routers import host
            sm = getattr(host, "_self_model", None)
            return getattr(sm, "journal", None) if sm is not None else None
        except Exception:
            return None

    @staticmethod
    def _min_occurrences() -> int:
        try:
            return int(os.environ.get("COLONY_TOOLSMITH_MIN_OCCURRENCES", "4"))
        except ValueError:
            return 4

    @staticmethod
    def _excluded_domains() -> set:
        raw = os.environ.get(
            "COLONY_TOOLSMITH_EXCLUDE_DOMAINS",
            "meta_learning,sandbox,toolsmith")
        return {d.strip() for d in raw.split(",") if d.strip()}

    def mine(self, *, limit: int = 1000) -> List[ToolCandidate]:
        """Return tool candidates ranked by occurrence count."""
        journal = self._j()
        if journal is None:
            return []
        try:
            entries = journal.recent(limit=limit)
        except Exception as exc:
            logger.warning("toolsmith miner: journal read failed: %s", exc)
            return []
        excluded = self._excluded_domains()
        clusters: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "domain": "", "descs": [], "refs": []})
        for e in entries:
            domain = (e.get("domain") or "").strip().lower()
            if domain in excluded:
                continue
            # only recurring *actions*, not held/blocked deliberations
            if e.get("decision") not in ("acted", "noted", None):
                continue
            desc = e.get("description") or ""
            sig = _normalize(desc)
            if len(sig) < 8:
                continue
            key = f"{domain}::{sig}"
            c = clusters[key]
            c["count"] += 1
            c["domain"] = domain
            if len(c["descs"]) < 5:
                c["descs"].append(desc)
            ref = e.get("ref") or str(e.get("id") or "")
            if ref and len(c["refs"]) < 20:
                c["refs"].append(ref)

        existing = self._existing_names()
        out: List[ToolCandidate] = []
        floor = self._min_occurrences()
        for key, c in clusters.items():
            if c["count"] < floor:
                continue
            sig = key.split("::", 1)[1]
            if self._signature_covered(sig, existing):
                continue
            out.append(ToolCandidate(
                signature=sig, domain=c["domain"],
                description=c["descs"][0] if c["descs"] else sig,
                occurrences=c["count"], evidence=c["refs"],
                sample_descriptions=c["descs"]))
        out.sort(key=lambda x: x.occurrences, reverse=True)
        return out

    def _existing_names(self) -> List[str]:
        if self._registry is None:
            return []
        try:
            return [t.name for t in self._registry.list()]
        except Exception:
            return []

    @staticmethod
    def _signature_covered(sig: str, tool_names: List[str]) -> bool:
        """Cheap guard against re-mining a procedure a tool already covers:
        a tool whose name tokens are a subset of the signature tokens."""
        sig_tokens = set(sig.replace("#", " ").split())
        for name in tool_names:
            name_tokens = set(name.split("_"))
            if name_tokens and name_tokens <= sig_tokens:
                return True
        return False
