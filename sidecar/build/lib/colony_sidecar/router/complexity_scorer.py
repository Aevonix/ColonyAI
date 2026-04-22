"""Complexity scorer — heuristic prompt analysis for tier selection.

Scores range 0.0–1.0:
  0.00–0.30  → SMALL  (simple Q&A, factual lookups, short summaries)
  0.30–0.65  → MEDIUM (multi-step reasoning, code generation, analysis)
  0.65–1.00  → LARGE  (complex architecture, deep research, critical tasks)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from colony_sidecar.router.tiers import ModelTier


@dataclass
class ComplexitySignals:
    token_count: int
    has_code: bool
    has_math: bool
    has_multi_step: bool        # "first... then... finally..."
    has_reasoning_required: bool  # "why", "explain", "analyze"
    has_tool_use: bool
    conversation_depth: int     # number of prior turns
    user_tier: str              # "standard" | "power" | "developer"


# Keyword sets for signal extraction
_MULTI_STEP_PATTERNS = re.compile(
    r"\b(first|step \d|then|after that|finally|next|followed by|subsequently)\b",
    re.IGNORECASE,
)
_REASONING_PATTERNS = re.compile(
    r"\b(why|explain|analyze|analyse|compare|contrast|evaluate|assess|critique|"
    r"debate|argue|justify|prove|derive|infer|reason|think through|walk me through)\b",
    re.IGNORECASE,
)
_CODE_PATTERNS = re.compile(
    r"(```|\bdef \b|\bclass \b|\bimport \b|\bfunction\b|\bvoid \b|\bpublic \b|"
    r"\bprivate \b|<code>|<script)",
    re.IGNORECASE,
)
_MATH_PATTERNS = re.compile(
    r"(\$\$?|\\\(|\bintegral\b|\bderivative\b|\bmatrix\b|\bequation\b|"
    r"\balgebra\b|\bcalculus\b|\bstatistics\b|\bprobability\b)",
    re.IGNORECASE,
)

# Signal weights for the final score.
# Weights are intentionally skewed toward reasoning/multi-step signals
# because those are the strongest predictors of tasks that genuinely need
# a larger model (architecture analysis, complex debugging, etc.).
_WEIGHTS = {
    "token_count": 0.12,
    "has_code": 0.12,
    "has_math": 0.08,
    "has_multi_step": 0.18,
    "has_reasoning_required": 0.22,
    "has_tool_use": 0.08,
    "conversation_depth": 0.06,
    "user_tier": 0.05,
    # Bonus applied when BOTH multi-step AND reasoning are detected.
    # A prompt that requires step-by-step thinking AND analytical reasoning
    # is reliably in the LARGE tier regardless of token count.
    "combined_reasoning_bonus": 0.20,
}

_TOKEN_SCALE = 500  # tokens above which token_count score saturates at 1.0
_DEPTH_SCALE = 10   # turns above which depth score saturates at 1.0


class ComplexityScorer:
    """Score prompt complexity to select an appropriate model tier."""

    def score(self, prompt: str, context: dict | None = None) -> float:
        """Return a complexity score in [0.0, 1.0]."""
        signals = self._extract_signals(prompt, context or {})
        return self._compute_score(signals)

    def select_tier(self, prompt: str, context: dict | None = None) -> ModelTier:
        """Return the cheapest model tier appropriate for this prompt."""
        score = self.score(prompt, context)
        if score < 0.3:
            return ModelTier.SMALL
        elif score < 0.65:
            return ModelTier.MEDIUM
        else:
            return ModelTier.LARGE

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_signals(self, prompt: str, context: dict) -> ComplexitySignals:
        # Rough token estimate: ~4 chars per token
        token_count = max(1, len(prompt) // 4)

        messages = context.get("messages", [])
        depth = len(messages) if isinstance(messages, list) else 0

        tools = context.get("tools", [])
        has_tool_use = bool(tools) or "tool" in prompt.lower() or "function" in prompt.lower()

        user_tier = context.get("user_tier", "standard")

        return ComplexitySignals(
            token_count=token_count,
            has_code=bool(_CODE_PATTERNS.search(prompt)),
            has_math=bool(_MATH_PATTERNS.search(prompt)),
            has_multi_step=bool(_MULTI_STEP_PATTERNS.search(prompt)),
            has_reasoning_required=bool(_REASONING_PATTERNS.search(prompt)),
            has_tool_use=has_tool_use,
            conversation_depth=depth,
            user_tier=user_tier,
        )

    def _compute_score(self, signals: ComplexitySignals) -> float:
        w = _WEIGHTS

        # Normalise continuous signals to [0, 1]
        token_score = min(1.0, signals.token_count / _TOKEN_SCALE)
        depth_score = min(1.0, signals.conversation_depth / _DEPTH_SCALE)
        tier_score = {"standard": 0.0, "power": 0.5, "developer": 1.0}.get(
            signals.user_tier, 0.0
        )

        # Bonus: a prompt that requires BOTH step-by-step structure AND analytical
        # reasoning is reliably complex enough to warrant the LARGE tier.
        # 0.25 ensures reasoning + multi_step clears the 0.65 LARGE threshold.
        combined_bonus = (
            0.25
            if signals.has_multi_step and signals.has_reasoning_required
            else 0.0
        )

        raw = (
            token_score * w["token_count"]
            + float(signals.has_code) * w["has_code"]
            + float(signals.has_math) * w["has_math"]
            + float(signals.has_multi_step) * w["has_multi_step"]
            + float(signals.has_reasoning_required) * w["has_reasoning_required"]
            + float(signals.has_tool_use) * w["has_tool_use"]
            + depth_score * w["conversation_depth"]
            + tier_score * w["user_tier"]
            + combined_bonus
        )
        return min(1.0, max(0.0, raw))
