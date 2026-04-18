"""FallbackHandler — escalate to a higher tier when a lower tier fails.

When a model call raises a rate-limit, context-length, or capability error,
the handler promotes the request to the next tier and retries once.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from colony_sidecar.router.tiers import ModelTier

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Errors that warrant a tier upgrade (substrings matched in error messages)
_UPGRADE_TRIGGERS = (
    "context_length_exceeded",
    "context window",
    "maximum context",
    "rate limit",
    "rate_limit_exceeded",
    "overloaded",
    "too many tokens",
    "insufficient capability",
)

_TIER_PROMOTION: dict[ModelTier, ModelTier] = {
    ModelTier.SMALL: ModelTier.MEDIUM,
    ModelTier.MEDIUM: ModelTier.LARGE,
    # LARGE has no higher tier — callers must handle this
}


class FallbackHandler:
    """Decide whether to escalate a failed request to the next model tier."""

    def should_escalate(self, error: Exception, current_tier: ModelTier) -> bool:
        """Return True if the error warrants a tier upgrade."""
        if current_tier == ModelTier.LARGE:
            return False  # Already at the top

        msg = str(error).lower()
        triggered = any(trigger in msg for trigger in _UPGRADE_TRIGGERS)
        if triggered:
            logger.warning(
                "FallbackHandler: escalating from %s due to: %s",
                current_tier.value,
                type(error).__name__,
            )
        return triggered

    def next_tier(self, current_tier: ModelTier) -> ModelTier | None:
        """Return the next tier to try, or None if already at LARGE."""
        return _TIER_PROMOTION.get(current_tier)
