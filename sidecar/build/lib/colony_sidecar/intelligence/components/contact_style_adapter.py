"""Contact Style Adapter — Colony's per-contact communication style learning.

Colony observes which style attributes correlate with positive engagement
(higher sentiment, faster responses, continued conversation) and adapts its
communication style for each contact over time.

Style profiles are Colony's own — they do not reflect owner preferences and
are not shared with the owner unless Colony surfaces them. Two different colonies
may develop different styles with the same person.

This is distinct from PreferenceLearner, which learns the **owner's** preferences
for how Colony communicates. ContactStyleAdapter handles Colony's per-contact
adaptation.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

import logging

logger = logging.getLogger(__name__)


@dataclass
class ContactStyleProfile:
    """Colony's learned communication style for a specific contact."""
    person_id: str
    formality: float = 0.5          # 0.0 = very casual, 1.0 = very formal
    response_length: float = 0.5    # 0.0 = terse, 1.0 = expansive
    tone: str = "neutral"           # "warm" | "neutral" | "professional"
    preferred_time_offset: int = 0  # minutes before/after contact's preferred hour
    emoji_appropriate: bool = True
    n_adaptations: int = 0
    last_updated: Optional[datetime] = None
    confidence: float = 0.0         # 0.0–1.0, requires n_adaptations >= 10 for full


class ContactStyleAdapter:
    """Learn and apply Colony's communication style per contact.

    Colony observes which style attributes correlate with positive engagement
    (higher sentiment, faster responses, continued conversation) and adapts
    over time. Style profiles are Colony's own — they do not reflect owner
    preferences and are not shared with the owner unless Colony surfaces them.

    Colony MUST NOT apply style adaptation to messages the owner is composing
    themselves. Style adaptation is Colony's own communication behavior.
    """

    MIN_SAMPLES_FOR_ADAPTATION = 10  # Colony needs sufficient history to adapt

    # Learning rate for style updates (Exponential Moving Average)
    _STYLE_ALPHA = 0.1

    # Sentiment thresholds for style adjustment direction
    _POSITIVE_SENTIMENT_THRESHOLD = 0.3
    _NEGATIVE_SENTIMENT_THRESHOLD = -0.3

    def __init__(self) -> None:
        self._profiles: Dict[str, ContactStyleProfile] = {}

    async def get_style(self, person_id: str) -> ContactStyleProfile:
        """Return Colony's current style profile for this person.

        Returns a default profile if no adaptations have been made yet.
        """
        if person_id not in self._profiles:
            self._profiles[person_id] = ContactStyleProfile(person_id=person_id)
        return self._profiles[person_id]

    async def update_style(
        self,
        person_id: str,
        signal: "Signal",
        response_sentiment: float,
    ) -> None:
        """Update style profile based on new engagement signal.

        Uses exponential moving average to gradually shift the style profile
        toward what correlates with positive engagement.

        Args:
            person_id: The contact whose style profile to update
            signal: The signal representing Colony's recent action or contact's response
            response_sentiment: Sentiment of the contact's response (-1.0 to 1.0)
        """
        profile = await self.get_style(person_id)

        signal_type = getattr(signal, "signal_type", "")
        context = getattr(signal, "context", {}) or {}

        # Infer style adjustment direction from signal context
        if response_sentiment > self._POSITIVE_SENTIMENT_THRESHOLD:
            # Positive response: reinforce current style slightly
            adjustment = self._STYLE_ALPHA
        elif response_sentiment < self._NEGATIVE_SENTIMENT_THRESHOLD:
            # Negative response: adjust away from current style
            adjustment = -self._STYLE_ALPHA
        else:
            # Neutral: minor decay toward default
            adjustment = 0.0

        if adjustment != 0.0:
            # Adjust formality based on signal direction
            if signal_type in ("sentiment", "contact_response_to_colony"):
                formality_hint = context.get("formality_hint")
                if formality_hint is not None:
                    profile.formality = self._ema(profile.formality, float(formality_hint), adjustment)

            # Adjust response length based on contact's message length patterns
            length_hint = context.get("length_hint")
            if length_hint is not None:
                profile.response_length = self._ema(profile.response_length, float(length_hint), adjustment)

            # Clamp
            profile.formality = max(0.0, min(1.0, profile.formality))
            profile.response_length = max(0.0, min(1.0, profile.response_length))

        # Infer emoji preference from negative signals
        if response_sentiment < self._NEGATIVE_SENTIMENT_THRESHOLD:
            emoji_used = context.get("emoji_used", False)
            if emoji_used:
                profile.emoji_appropriate = False

        profile.n_adaptations += 1
        profile.last_updated = datetime.now()

        # Confidence grows toward 1.0 as we accumulate adaptations
        profile.confidence = min(1.0, profile.n_adaptations / (self.MIN_SAMPLES_FOR_ADAPTATION * 2))

        logger.debug(
            "Updated style profile for %s: formality=%.2f, length=%.2f, n=%d",
            person_id,
            profile.formality,
            profile.response_length,
            profile.n_adaptations,
        )

    async def apply_style(
        self,
        person_id: str,
        draft: str,
    ) -> str:
        """Apply style profile to a draft message before Colony sends it.

        Only applies adaptation when Colony has sufficient history
        (n_adaptations >= MIN_SAMPLES_FOR_ADAPTATION). Returns draft unchanged
        if there is insufficient history or low confidence.

        Colony MUST NOT call this on messages the owner is composing.

        Args:
            person_id: The contact this message will be sent to
            draft: The draft message text

        Returns:
            The adapted message (may be unchanged if insufficient history)
        """
        profile = await self.get_style(person_id)

        if profile.n_adaptations < self.MIN_SAMPLES_FOR_ADAPTATION:
            logger.debug(
                "Skipping style adaptation for %s: only %d/%d adaptations",
                person_id,
                profile.n_adaptations,
                self.MIN_SAMPLES_FOR_ADAPTATION,
            )
            return draft

        adapted = draft

        # Apply emoji suppression if learned
        if not profile.emoji_appropriate:
            adapted = self._strip_emoji(adapted)

        # Length adaptation: trim if brevity preferred, leave expansive alone
        if profile.response_length < 0.3 and len(adapted) > 200:
            # Contact prefers brevity — truncate at sentence boundary
            adapted = self._truncate_to_length(adapted, target_chars=150)

        logger.debug(
            "Applied style for %s (confidence=%.2f, formality=%.2f, length=%.2f)",
            person_id,
            profile.confidence,
            profile.formality,
            profile.response_length,
        )

        return adapted

    async def update_from_profiler_profile(
        self,
        person_id: str,
        profiler_profile: Any,
    ) -> None:
        """Update style profile using profiler dimension scores.

        Translates profiler behavioral dimensions to ContactStyleAdapter
        attributes, providing observation-backed signals that supplement
        the EMA-based style updates from individual interactions.

        Args:
            person_id: Contact whose style profile to update.
            profiler_profile: A ContactProfile from colony_sidecar.profiler.store with
                a `.dimensions` dict of DimensionScore objects.
        """
        profile = await self.get_style(person_id)
        dimensions = getattr(profiler_profile, "dimensions", {}) or {}

        # formality dimension (−1 to +1) → style formality (0 to 1)
        formality_dim = dimensions.get("formality")
        if formality_dim is not None and getattr(formality_dim, "confidence", 0.0) > 0.3:
            normalized = (formality_dim.score + 1.0) / 2.0
            profile.formality = self._ema(profile.formality, normalized, self._STYLE_ALPHA)
            profile.formality = max(0.0, min(1.0, profile.formality))

        # communication_style dimension (−1 to +1) → response_length (0 to 1)
        style_dim = dimensions.get("communication_style")
        if style_dim is not None and getattr(style_dim, "confidence", 0.0) > 0.3:
            normalized = (style_dim.score + 1.0) / 2.0
            profile.response_length = self._ema(profile.response_length, normalized, self._STYLE_ALPHA)
            profile.response_length = max(0.0, min(1.0, profile.response_length))

        # emotional_expressiveness dimension → emoji_appropriate
        emoji_dim = dimensions.get("emotional_expressiveness")
        if emoji_dim is not None and getattr(emoji_dim, "confidence", 0.0) > 0.5:
            profile.emoji_appropriate = emoji_dim.score > -0.3

        profile.n_adaptations += 1
        profile.last_updated = datetime.now()
        profile.confidence = min(1.0, profile.n_adaptations / (self.MIN_SAMPLES_FOR_ADAPTATION * 2))

        logger.debug(
            "Updated style profile from profiler for %s: formality=%.2f, length=%.2f, emoji=%s",
            person_id,
            profile.formality,
            profile.response_length,
            profile.emoji_appropriate,
        )

    def get_style_summary(self, person_id: str) -> str:
        """Return a human-readable summary of Colony's style for this person.

        Used when the owner queries 'How does Colony communicate with [person]?'
        """
        profile = self._profiles.get(person_id)
        if not profile or profile.n_adaptations == 0:
            return f"No style data yet for {person_id} — Colony has not adapted its style."

        formality_label = "formal" if profile.formality > 0.7 else ("casual" if profile.formality < 0.3 else "neutral")
        length_label = "brief" if profile.response_length < 0.35 else ("expansive" if profile.response_length > 0.65 else "moderate")
        emoji_label = "emoji-friendly" if profile.emoji_appropriate else "no emoji"

        return (
            f"Colony uses a {formality_label}, {length_label} style with this contact "
            f"({emoji_label}). Confidence: {profile.confidence:.0%} "
            f"(based on {profile.n_adaptations} interaction(s))."
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ema(current: float, target: float, alpha: float) -> float:
        """Exponential moving average update."""
        return current + alpha * (target - current)

    @staticmethod
    def _strip_emoji(text: str) -> str:
        """Remove common emoji patterns from text."""
        import re
        emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"
            "\U0001F300-\U0001F5FF"
            "\U0001F680-\U0001F6FF"
            "\U0001F700-\U0001F77F"
            "\U0001F780-\U0001F7FF"
            "\U0001F800-\U0001F8FF"
            "\U0001F900-\U0001F9FF"
            "\U0001FA00-\U0001FA6F"
            "\U0001FA70-\U0001FAFF"
            "\U00002702-\U000027B0"
            "\U000024C2-\U0001F251"
            "]+",
            flags=re.UNICODE,
        )
        return emoji_pattern.sub("", text).strip()

    @staticmethod
    def _truncate_to_length(text: str, target_chars: int) -> str:
        """Truncate text to approximately target_chars at a sentence boundary."""
        if len(text) <= target_chars:
            return text

        # Find last sentence end before target
        for i in range(target_chars, max(0, target_chars - 60), -1):
            if text[i] in ".!?":
                return text[:i + 1].strip()

        # Fall back to word boundary
        truncated = text[:target_chars]
        last_space = truncated.rfind(" ")
        if last_space > target_chars // 2:
            return truncated[:last_space].strip() + "..."
        return truncated.strip() + "..."
