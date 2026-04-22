"""Colony Skills — context budget for progressive skill loading."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ContextBudget:
    """Governs how many tokens loaded skills may collectively consume.

    The base budget comes from config. If TurboQuant is active,
    the effective budget is scaled up by the current compression ratio
    (more cache capacity → more room for skills).
    """

    base_tokens: int = 8192
    turboquant_ratio: float = 1.0  # updated each tick; ≥1.0

    @property
    def effective_tokens(self) -> int:
        return int(self.base_tokens * self.turboquant_ratio)

    def has_capacity(self, needed: int, current_used: int) -> bool:
        return current_used + needed <= self.effective_tokens

    def tokens_available(self, current_used: int) -> int:
        return max(0, self.effective_tokens - current_used)
