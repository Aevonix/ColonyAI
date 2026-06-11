"""Preference Learner — capture and apply the owner's stated preferences.

This is the *explicit-directive* lane of preference learning: when the owner
says how they want their assistant to communicate ("be concise", "use bullet
points", "stop with the emoji"), it is captured deterministically at high
confidence and surfaced back every turn. It is deliberately distinct from the
EngagementStore, which passively *infers* a contact's OCEAN/style over many
observations — here we record what the owner directly told us.

Tracks:
    - Communication style preferences (length / format / tone / emoji)
    - Topic interests
    - Scheduling preferences

Preferences persist to SQLite when a ``db_path`` is supplied, so an explicit
directive survives a sidecar restart instead of resetting to nothing.
"""

import json
import re
import sqlite3
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

_EMOJI_WORDS = frozenset({"emoji", "emojis", "emoticon", "emoticons"})
_NEGATION = frozenset({"no", "not", "dont", "don", "stop", "without", "never", "drop", "avoid", "less", "skip"})

# Tokens that signal the owner is *directing* how to communicate (vs. merely
# using a style word in passing). A directive needs both a style keyword AND
# one of these cues, which keeps false positives off ordinary messages.
_DIRECTIVE_CUES = frozenset({
    "be", "keep", "make", "use", "using", "give", "show", "stop", "do", "dont", "don",
    "prefer", "want", "always", "never", "instead", "please", "less", "more", "shorter",
    "longer", "reply", "replies", "respond", "response", "responses", "answer", "answers",
    "you", "your", "from", "now", "on", "going", "forward",
})
_DIRECTIVE_MAX_TOKENS = 30  # directives are short; ignore long messages that merely contain a style word

# All style keywords that can anchor a directive.
_STYLE_KEYWORDS = (
    _LENGTH_SHORT | _LENGTH_LONG | _LENGTH_MEDIUM
    | _FORMAT_BULLETS | _FORMAT_PROSE | _FORMAT_CODE | _FORMAT_TABLE
    | _STYLE_FORMAL | _STYLE_CASUAL | _STYLE_TECHNICAL
    | _EMOJI_WORDS
)

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
        graph_client: Optional Colony graph client (reserved for graph mirroring).
        db_path: Optional SQLite path. When given, preferences are loaded on
            construction and persisted on every update, so explicit owner
            directives survive a restart.
    """

    def __init__(self, graph_client: Any = None, db_path: Optional[str] = None) -> None:
        self.graph = graph_client
        self._preferences: Dict[str, Preference] = {}
        self._conn: Optional[sqlite3.Connection] = None
        if db_path:
            try:
                self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute(
                    """CREATE TABLE IF NOT EXISTS owner_preferences (
                        pref_key TEXT PRIMARY KEY,
                        category TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        learned_from TEXT NOT NULL,
                        last_updated TEXT NOT NULL
                    )"""
                )
                self._conn.commit()
                self._load()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("PreferenceLearner persistence disabled (%s)", exc)
                self._conn = None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._conn is None:
            return
        for row in self._conn.execute("SELECT * FROM owner_preferences"):
            try:
                value = json.loads(row["value"])
            except (TypeError, ValueError):
                value = row["value"]
            self._preferences[row["pref_key"]] = Preference(
                category=row["category"],
                key=row["key"],
                value=value,
                confidence=row["confidence"],
                learned_from=row["learned_from"],
                last_updated=datetime.fromisoformat(row["last_updated"]),
            )

    def _persist(self, pref_key: str, pref: "Preference") -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                """INSERT INTO owner_preferences
                       (pref_key, category, key, value, confidence, learned_from, last_updated)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(pref_key) DO UPDATE SET
                       category=excluded.category, key=excluded.key, value=excluded.value,
                       confidence=excluded.confidence, learned_from=excluded.learned_from,
                       last_updated=excluded.last_updated""",
                (
                    pref_key, pref.category, pref.key, json.dumps(pref.value),
                    float(pref.confidence), pref.learned_from,
                    pref.last_updated.isoformat(),
                ),
            )
            self._conn.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("PreferenceLearner persist failed: %s", exc)

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
        self._persist(pref_key, self._preferences[pref_key])

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
        self._persist(pref_key, self._preferences[pref_key])

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

    def detect_directive(self, message: str) -> Optional[Tuple[str, str, Any]]:
        """Detect an explicit communication directive in a raw owner message.

        Returns ``(category, key, value)`` only when the message both contains a
        style keyword and reads like an instruction (a directive cue token, and
        not an overly long message). Returns ``None`` otherwise, so ordinary
        conversation never trips preference learning.

        Examples that match::

            "be more concise"            -> ("communication_style", "length", "short")
            "use bullet points please"   -> ("communication_style", "format", "bullet_points")
            "stop using emoji"           -> ("communication_style", "emoji", "off")
            "keep your replies formal"   -> ("communication_style", "style", "formal")
        """
        if not message:
            return None
        words = set(re.findall(r"\b\w+\b", message.lower()))
        if len(words) > _DIRECTIVE_MAX_TOKENS:
            return None
        if not (words & _STYLE_KEYWORDS) or not (words & _DIRECTIVE_CUES):
            return None
        key, value = self._parse_feedback("communication_style", message)
        if key == "general":
            return None
        return ("communication_style", key, value)

    def _parse_all(self, message: str) -> List[Tuple[str, Any]]:
        """Extract every communication-style ``(key, value)`` present in *message*.

        Unlike ``_parse_feedback`` (first match only), this captures multiple
        directives in one sentence, e.g. "be concise and use bullet points".
        Length wins ties over the technical tone bucket on shared words like
        "detailed", so a word is never double-counted across dimensions.
        """
        words = set(re.findall(r"\b\w+\b", message.lower()))
        out: List[Tuple[str, Any]] = []

        if words & _EMOJI_WORDS:
            out.append(("emoji", "off" if (words & _NEGATION) else "on"))

        if words & _LENGTH_SHORT:
            out.append(("length", "short"))
        elif words & _LENGTH_LONG:
            out.append(("length", "long"))
        elif words & _LENGTH_MEDIUM:
            out.append(("length", "medium"))

        if words & _FORMAT_BULLETS:
            out.append(("format", "bullet_points"))
        elif words & _FORMAT_PROSE:
            out.append(("format", "prose"))
        elif words & _FORMAT_CODE:
            out.append(("format", "code_examples"))
        elif words & _FORMAT_TABLE:
            out.append(("format", "table"))

        style_words = words - _LENGTH_LONG  # don't let 'detailed' also claim 'technical'
        if style_words & _STYLE_FORMAL:
            out.append(("style", "formal"))
        elif style_words & _STYLE_CASUAL:
            out.append(("style", "casual"))
        elif style_words & _STYLE_TECHNICAL:
            out.append(("style", "technical"))

        return out

    async def learn_directive(self, message: str) -> Optional[Tuple[str, str, Any]]:
        """If *message* is an explicit communication directive, learn every style
        preference it contains (explicit confidence) and return the primary
        ``(category, key, value)``; else ``None``.

        The gate (``detect_directive``) keeps ordinary conversation from being
        learned; once gated in, all directives in the message are captured.
        """
        primary = self.detect_directive(message)
        if primary is None:
            return None
        for key, value in self._parse_all(message):
            pref_key = f"communication_style.{key}"
            self._preferences[pref_key] = Preference(
                category="communication_style", key=key, value=value,
                confidence=0.9, learned_from="explicit",
            )
            self._persist(pref_key, self._preferences[pref_key])
        logger.info("Learned owner directive(s) from: %r", message[:80])
        return primary

    # Human-readable rendering of each communication-style preference.
    _BRIEF_LINES: Dict[Tuple[str, Any], str] = {
        ("length", "short"): "Keep replies short and to the point.",
        ("length", "long"): "Give thorough, detailed replies.",
        ("length", "medium"): "Aim for medium-length replies.",
        ("format", "bullet_points"): "Prefer bullet points over prose.",
        ("format", "prose"): "Prefer flowing prose over bullet lists.",
        ("format", "code_examples"): "Include code examples when relevant.",
        ("format", "table"): "Use tables where they help.",
        ("style", "formal"): "Keep a formal, professional tone.",
        ("style", "casual"): "Keep a casual, relaxed tone.",
        ("style", "technical"): "Be technical and precise.",
        ("emoji", "off"): "Don't use emoji.",
        ("emoji", "on"): "Emoji are fine.",
    }

    def build_brief(self, min_confidence: float = 0.5) -> str:
        """Render the owner's stated communication preferences as a short brief.

        Only ``communication_style`` preferences at or above *min_confidence* are
        included (explicit directives are 0.9). Returns '' when there is nothing
        confident to assert.
        """
        lines: List[str] = []
        for pref in self._preferences.values():
            if pref.category != "communication_style" or pref.confidence < min_confidence:
                continue
            line = self._BRIEF_LINES.get((pref.key, pref.value))
            if line and line not in lines:
                lines.append(line)
        return "\n".join(f"- {ln}" for ln in lines)

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

        if words & _EMOJI_WORDS:
            return ("emoji", "off" if (words & _NEGATION) else "on")

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
