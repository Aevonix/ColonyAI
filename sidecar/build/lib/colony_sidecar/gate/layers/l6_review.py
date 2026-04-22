"""Layer 6 — Secondary LLM review (soft flag only). Separate model instance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class ReviewResult:
    flagged: bool
    category: Optional[str] = None


class SecondaryReviewerProtocol(Protocol):
    async def review(self, payload, injection_suspicious: bool = False) -> ReviewResult:
        ...


class SecondaryReviewer:
    """Layer 6 — Secondary LLM review.

    Uses a separate model instance. Has no access to workspace or other sessions.
    Returns binary: appropriate | flag_for_review.

    In production, this calls the Anthropic API with a restricted prompt.
    For testing/default use, this is a no-op that always passes.
    """

    def __init__(self, config=None, llm_client=None) -> None:
        self._config = config
        self._llm_client = llm_client

    async def review(self, payload, injection_suspicious: bool = False) -> ReviewResult:
        if self._llm_client is None:
            # No LLM configured — pass through
            return ReviewResult(flagged=False)

        trust_tier = payload.trust_tier.value if hasattr(payload.trust_tier, "value") else str(payload.trust_tier)
        prompt = (
            f"You are a message appropriateness reviewer. You will be given:\n"
            f"1. A message draft.\n"
            f"2. The trust tier of the recipient.\n"
            f"3. Optionally: an injection suspicion flag.\n\n"
            f"Your ONLY job is to classify the message as \"appropriate\" or \"flag_for_review\".\n\n"
            f"Trust tier: {trust_tier}\n"
            f"Injection suspicion: {injection_suspicious}\n\n"
            f"Message:\n---\n{payload.response_text}\n---\n\n"
            f'Respond with a single JSON object: {{"verdict": "appropriate"}} or '
            f'{{"verdict": "flag_for_review", "category": "<brief category>"}}'
        )

        try:
            response_text = await self._llm_client.complete(prompt)
            import json
            data = json.loads(response_text.strip())
            if data.get("verdict") == "flag_for_review":
                return ReviewResult(flagged=True, category=data.get("category"))
            return ReviewResult(flagged=False)
        except Exception as exc:
            # Fail-closed: if the review LLM is unavailable or returns malformed
            # output, block the message rather than silently passing it (SEC-14-H-01)
            import logging
            logging.getLogger(__name__).warning(
                "Gate L6 review LLM unavailable — failing closed: %s", exc
            )
            return ReviewResult(flagged=True, category="review_error")
