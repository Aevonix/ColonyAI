"""Colony Identity Bootstrap — self-referential query detection.

Detects user messages that ask about Colony's own capabilities,
architecture, or identity, and builds grounding context from the
self-knowledge corpus so the agent can answer accurately.
"""

from __future__ import annotations

import re
from typing import Optional

# Pre-compiled patterns for self-referential query detection.
# Each pattern targets a class of question about Colony's own capabilities.
_SELF_PATTERNS = [
    # Capability / feature queries
    re.compile(r"\bwhat\b.*\b(?:can you do|are your capabilities|features)\b", re.I),
    re.compile(r"\bwhat\b.*\b(?:tools|commands|skills)\b.*\b(?:do you|you)\b", re.I),
    re.compile(r"\bhow\b.*\bdo you work\b", re.I),
    re.compile(r"\bwhat\b.*\b(?:endpoints?|api|routes?)\b.*\b(?:do you have|available|support)\b", re.I),
    re.compile(r"\bwhat\b.*\bcommands?\b.*\b(?:do you|you)\b.*\bsupport\b", re.I),
    # Architecture queries
    re.compile(r"\bhow many\b.*\blayers?\b", re.I),
    re.compile(r"\bwhat\b.*\b(?:is|are)\b.*\byour\b.*\barchitecture\b", re.I),
    re.compile(r"\bwhat\b.*\bmodels?\b.*\b(?:do you use|you use)\b", re.I),
    re.compile(r"\bwhat\b.*\b(?:inference|llm)\b.*\btiers?\b", re.I),
    # Safety / gate queries
    re.compile(r"\bwhat\b.*\b(?:safety|gate|security)\b.*\blayers?\b", re.I),
    re.compile(r"\bresponse\s*gate\b", re.I),
    # Identity queries
    re.compile(r"\bwho\b.*\bare you\b", re.I),
    re.compile(r"\bwhat\b.*\bare you\b", re.I),
    re.compile(r"\btell me about yourself\b", re.I),
    re.compile(r"\bdescribe yourself\b", re.I),
    re.compile(r"\byour\b.*\b(?:version|colony.?id|identity)\b", re.I),
    # Cognition pipeline queries
    re.compile(r"\bcognition\b.*\b(?:pipeline|phases?)\b", re.I),
    re.compile(r"\bself.?(?:improvement|learning|reflection)\b", re.I),
    # Subsystem queries
    re.compile(r"\bwhat\b.*\bsubsystems?\b", re.I),
    re.compile(r"\blist\b.*\byour\b.*\b(?:systems?|components?)\b", re.I),
]


def query_is_self_referential(message: str) -> bool:
    """Return True if *message* is asking about Colony's own capabilities."""
    if not message or len(message) < 8:
        return False
    for pattern in _SELF_PATTERNS:
        if pattern.search(message):
            return True
    return False


def build_self_context_from_corpus() -> Optional[str]:
    """Build a grounding context block from the live SelfKnowledgeCorpus.

    Returns a formatted string suitable for injection into the agent's
    prompt, or None if the corpus cannot be loaded.
    """
    try:
        from colony_sidecar.identity_bootstrap.builder import IdentityBootstrapBuilder

        builder = IdentityBootstrapBuilder()
        corpus = builder.build()
    except Exception:
        return None

    lines = [
        "# Colony Self-Knowledge (auto-injected)",
        "",
        f"**Identity:** {corpus.colony_name} (ID: {corpus.colony_id}, "
        f"v{corpus.colony_version}, network: {corpus.network_id})",
        "",
        f"**Architecture:** {len(corpus.layers)} layers — "
        + ", ".join(l.name for l in corpus.layers),
        "",
        f"**API Endpoints:** {len(corpus.api_endpoints)} across "
        f"{len(set(e.router for e in corpus.api_endpoints))} routers: "
        + ", ".join(sorted(set(e.router for e in corpus.api_endpoints))),
        "",
        f"**Cognition Pipeline:** {len(corpus.cognition_phases)} phases — "
        + ", ".join(p.name for p in corpus.cognition_phases),
        "",
        f"**ResponseGate:** {len(corpus.gate_layers)} safety layers — "
        + ", ".join(g.name for g in corpus.gate_layers),
        "",
        f"**Inference Tiers:** {len(corpus.inference_tiers)} — "
        + ", ".join(f"{t.name} ({t.complexity_range})" for t in corpus.inference_tiers),
        "",
    ]
    return "\n".join(lines)
