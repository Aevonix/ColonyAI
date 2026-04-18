"""Layer 4 — Trust tier boundary enforcement. Deterministic pattern matching."""

from __future__ import annotations

import re

from colony_sidecar.gate.layers.base import LayerResult
from colony_sidecar.intelligence.relationships.trust_tiers import TrustTier

# Relationship assessment disclosure patterns
_ASSESSMENT_PATTERNS = [
    re.compile(r"i[''`]?ve\s+noticed\s+that\s+\w+", re.IGNORECASE),
    re.compile(r"based\s+on\s+my\s+(interactions?|observations?)\s+with\s+\w+", re.IGNORECASE),
    re.compile(r"in\s+my\s+assessment\s+of\s+\w+", re.IGNORECASE),
]

# Colony internal state patterns
_INTERNAL_STATE_PATTERNS = [
    re.compile(r"my\s+(memory|graph|knowledge\s+graph|neo4j|context\s+window)", re.IGNORECASE),
    re.compile(r"i\s+(store|remember|track|maintain)\s+", re.IGNORECASE),
    re.compile(r"(scratch\s+(buffer|pad)|working\s+memory|deliberat)", re.IGNORECASE),
]

# Private detail patterns
_PRIVATE_DETAIL_PATTERNS = [
    re.compile(r"\b(born|age[sd]?|birthday|health|medical|diagnosis|illness)\b", re.IGNORECASE),
    re.compile(r"\b(address|lives\s+at|located\s+at|home\s+in)\b", re.IGNORECASE),
    re.compile(r"\b(salary|income|earns?|makes?\s+\$)\b", re.IGNORECASE),
]


class TrustTierChecker:
    """Layer 4 — Trust tier boundary enforcement. Deterministic pattern matching."""

    def __init__(self, config=None) -> None:
        self._config = config

    async def check(self, payload) -> LayerResult:
        tier = payload.trust_tier

        if tier == TrustTier.SILENCED:
            return LayerResult(
                blocked=True,
                code="block_trust_tier",
                reason="contact is silenced; no response permitted",
            )

        text = payload.response_text

        if tier in (TrustTier.REGULAR, TrustTier.PERIPHERAL):
            for pattern in _ASSESSMENT_PATTERNS:
                if pattern.search(text):
                    return LayerResult(
                        blocked=True,
                        code="block_trust_tier",
                        reason=f"relationship assessment disclosure not permitted at tier={tier.value}",
                        flagged_excerpt="[assessment pattern]",
                    )
            for pattern in _INTERNAL_STATE_PATTERNS:
                if pattern.search(text):
                    return LayerResult(
                        blocked=True,
                        code="block_trust_tier",
                        reason=f"internal state disclosure not permitted at tier={tier.value}",
                        flagged_excerpt="[internal state pattern]",
                    )

        if tier == TrustTier.PERIPHERAL:
            for pattern in _PRIVATE_DETAIL_PATTERNS:
                if pattern.search(text):
                    return LayerResult(
                        blocked=True,
                        code="block_trust_tier",
                        reason="private detail patterns not permitted at peripheral tier",
                        flagged_excerpt="[private detail]",
                    )

        return LayerResult(blocked=False, code="pass")
