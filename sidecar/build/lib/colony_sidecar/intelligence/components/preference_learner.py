"""Preference Learner — extract and apply user preferences.

Tracks:
    - Communication style preferences
    - Response length preferences
    - Topic interests
    - Scheduling preferences
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword maps for feedback parsing
# ---------------------------------------------------------------------------

_LENGTH_SHORT = frozenset({"short", "brief", "concise", "terse", "quick", "compact", "succinct"})
_LENGTH_LONG = frozenset({"long", "detailed", "comprehensive", "verbose", "thorough", "elaborate", "in-depth"})
_LENGTH_MEDIUM = frozenset({"medium", "moderate", "balanced", "average"})

_FORMAT_BULLETS = frozenset({"bullet", "bullets", "list", "points", "numbered", "itemize"})
_FORMAT_PROSE = frozenset({"paragraph", "prose", "narrative", "flowing", "text"})
_FORMAT_CODE = frozenset({"code", "snippet", "example", "examples", "implementation"})
_FORMAT_TABLE = frozenset({"table", "grid", "tabular", "spreadsheet"})

_STYLE_FORMAL = frozenset({"formal", "professional", "business", "official", "academic"})
_STYLE_CASUAL = frozenset({"casual", "informal", "friendly", "conversational", "relaxed"})
_STYLE_TECHNICAL = frozenset({"technical", "expert", "advanced", "detailed", "deep"})

_TIMING_MORNING = frozenset({"morning", "early", "am", "dawn", "breakfast"})
_TIMING_EVENING = frozenset({"evening", "pm", "afternoon", "night", "after work"})

# Action keyword → (category, key, value)
_ACTION_RULES: List[Tuple[Set[str], str, str, Any]] = [
    ({"short", "brief", "concise"}, "communication_style", "length", "short"),
    ({"long", "detailed", "verbose"}, "communication_style", "length", "long"),
    ({"dark"}, "display", "theme", "dark"),
    ({"light"}, "display", "theme", "light"),
    ({"bullet", "list"}, "communication_style", "format", "bullet_points"),
    ({"prose", "paragraph"}, "communication_style", "format", "prose"),
    ({"code", "snippet"}, "communication_style", "format", "code_examples"),
]


@dataclass
class Preference:
    """A learned user preference.

    Attributes:
        category: Preference domain ("communication_style", "response_length", etc.)
        key: Specific preference key within the category
        value: The preference value
        confidence: How confident we are (0-1)
        learned_from: How the preference was learned ("explicit", "implicit", "inferred")
        last_updated: Most recent update timestamp
    """

    category: str
    key: str
    value: Any
    confidence: float
    learned_from: str
    last_updated: datetime = field(default_factory=datetime.now)


class PreferenceLearner:
    """Learn and apply user preferences.

    Tracks preferences from two sources:
        - Explicit feedback (high confidence, 0.9)
        - Observed behavior (lower confidence, 0.5, accumulates with repetition)

    Feedback parsing uses keyword matching to extract structured preferences
    from natural-language feedback strings.

    Args:
        graph_client: Colony graph client for persistent preference storage
    """

    def __init__(self, graph_client: Any) -> None:
        self.graph = graph_client
        self._preferences: Dict[str, Preference] = {}

    async def learn_from_feedback(
        self,
        category: str,
        feedback: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Learn preference from explicit user feedback.

        Explicit feedback gets high confidence (0.9) since the user
        directly stated a preference.  The raw feedback is parsed into
        a (key, value) pair using keyword matching.

        Args:
            category: Preference domain
            feedback: The user's stated preference
            context: Optional additional context
        """
        key, value = self._parse_feedback(category, feedback)

        pref_key = f"{category}.{key}"
        self._preferences[pref_key] = Preference(
            category=category,
            key=key,
            value=value,
            confidence=0.9,
            learned_from="explicit",
        )

        logger.debug("Learned explicit preference: %s = %s", pref_key, value)

    async def learn_from_behavior(
        self,
        action: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Learn preference from observed user behavior.

        Implicit preferences start at 0.5 confidence and accumulate
        with repeated observation (capped at 1.0).

        Args:
            action: The observed user action (e.g., "clicked_short_response")
            context: Optional additional context
        """
        category, key, value = self._infer_from_action(action, context)

        if not category:
            return

        pref_key = f"{category}.{key}"
        existing = self._preferences.get(pref_key)

        if existing:
            existing.value = value
            existing.confidence = min(1.0, existing.confidence + 0.1)
            existing.learned_from = "implicit"
            existing.last_updated = datetime.now()
        else:
            self._preferences[pref_key] = Preference(
                category=category,
                key=key,
                value=value,
                confidence=0.5,
                learned_from="implicit",
            )

        logger.debug("Learned implicit preference: %s.%s = %s", category, key, value)

    async def get_preference(
        self,
        category: str,
        key: str,
        default: Any = None,
    ) -> Any:
        """Get a learned preference value.

        Args:
            category: Preference domain
            key: Specific preference key
            default: Value to return if preference not found

        Returns:
            The preference value, or default if not found
        """
        pref_key = f"{category}.{key}"
        pref = self._preferences.get(pref_key)
        return pref.value if pref else default

    async def get_all_preferences(self, category: Optional[str] = None) -> List[Preference]:
        """Get all preferences, optionally filtered by category.

        Args:
            category: If provided, only return preferences in this category

        Returns:
            List of matching preferences
        """
        prefs = list(self._preferences.values())
        if category:
            prefs = [p for p in prefs if p.category == category]
        return prefs

    def _parse_feedback(self, category: str, feedback: str) -> Tuple[str, Any]:
        """Parse explicit feedback into a (key, value) preference pair.

        Uses keyword matching to identify the most likely preference
        being expressed.  Falls back to storing the raw feedback under
        the 'general' key.

        Args:
            category: The preference category (used for context)
            feedback: Natural-language feedback string

        Returns:
            Tuple of (preference_key, preference_value)
        """
        words = set(re.findall(r"\b\w+\b", feedback.lower()))

        if words & _LENGTH_SHORT:
            return ("length", "short")
        if words & _LENGTH_LONG:
            return ("length", "long")
        if words & _LENGTH_MEDIUM:
            return ("length", "medium")

        if words & _FORMAT_BULLETS:
            return ("format", "bullet_points")
        if words & _FORMAT_PROSE:
            return ("format", "prose")
        if words & _FORMAT_CODE:
            return ("format", "code_examples")
        if words & _FORMAT_TABLE:
            return ("format", "table")

        if words & _STYLE_FORMAL:
            return ("style", "formal")
        if words & _STYLE_CASUAL:
            return ("style", "casual")
        if words & _STYLE_TECHNICAL:
            return ("style", "technical")

        if words & _TIMING_MORNING:
            return ("timing", "morning")
        if words & _TIMING_EVENING:
            return ("timing", "evening")

        return ("general", feedback)

    def _infer_from_action(
        self,
        action: str,
        context: Optional[Dict[str, Any]],
    ) -> Tuple[Optional[str], Optional[str], Any]:
        """Infer preference from an observed action pattern.

        Tokenises the action string on underscores/spaces and matches
        against known keyword rules.

        Args:
            action: Action identifier (e.g., "clicked_short_response")
            context: Optional additional context

        Returns:
            Tuple of (category, key, value) or (None, None, None) if not recognised
        """
        # Split on underscores and spaces (not \b which treats _ as word char)
        parts = set(re.findall(r"[a-z]+", action.lower()))

        for keywords, category, key, value in _ACTION_RULES:
            if parts & keywords:
                return (category, key, value)

        return (None, None, None)
