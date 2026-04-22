"""Colony Briefing System — LLM Enhancement Pass.

Generates natural-language narratives for briefing sections.
Falls back gracefully when no LLM client is provided or when
the LLM call fails.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from .models import Briefing, BriefingSection

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are Colony's briefing narrator. Transform structured data into concise, "
    "natural-language summaries. Be direct and specific. Do not invent information "
    "not present in the data. Tone: calm, professional, actionable."
)


@runtime_checkable
class LLMClientProtocol(Protocol):
    """Minimal interface expected from an LLM client."""

    def complete(self, system: str, prompt: str, max_tokens: int) -> str: ...


class BriefingLMEnhancer:
    """Generate natural-language narratives for briefing sections."""

    def __init__(
        self,
        llm_client: Optional[LLMClientProtocol] = None,
        enabled: bool = True,
    ) -> None:
        self._client = llm_client
        self._enabled = enabled
        self._tokens_used = 0

    @property
    def tokens_used(self) -> int:
        return self._tokens_used

    def enhance_section(
        self,
        section_name: str,
        structured_content: Dict[str, Any],
        max_tokens: int = 150,
    ) -> str:
        """Generate a natural-language narrative for a single section.

        Returns an empty string if LLM is disabled, unavailable, or if
        the content has no notable information.
        """
        if not self._enabled or self._client is None:
            return ""

        if not structured_content:
            return ""

        prompt = (
            f"Section: {section_name}\n"
            f"Data: {json.dumps(structured_content, default=str)}\n\n"
            "Write a 1–3 sentence briefing summary for this section. "
            "Be concise and actionable. Do not repeat the raw data literally."
        )
        try:
            narrative = self._client.complete(_SYSTEM_PROMPT, prompt, max_tokens)
            self._tokens_used += max_tokens  # conservative accounting
            return narrative.strip()
        except Exception as exc:
            logger.warning("LLM enhancement failed for section '%s': %s", section_name, exc)
            return ""

    def enhance_briefing(
        self,
        briefing: Briefing,
        max_tokens_per_section: int = 150,
    ) -> Briefing:
        """Run the LLM enhancement pass on all active sections.

        Modifies section.narrative in-place. Returns the updated briefing.
        Falls back to structured-only content on any LLM failure.
        """
        if not self._enabled or self._client is None:
            return briefing

        for section in briefing.active_sections():
            if section.suppressed:
                continue
            section.narrative = self.enhance_section(
                section.name,
                section.content,
                max_tokens=max_tokens_per_section,
            )

        return briefing
