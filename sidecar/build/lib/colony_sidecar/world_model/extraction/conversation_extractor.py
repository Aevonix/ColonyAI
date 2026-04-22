"""Lightweight post-message entity extractor.

Uses rule-based NER and regex patterns. Zero LLM calls.
MUST complete within 50ms for single-message inputs.
"""

import re
import time
from dataclasses import dataclass, field
from typing import List, Optional

from ..confidence import CONFIDENCE_BY_SOURCE, apply_extraction_adjustments


# ── Regex patterns for structured entity signals ──────────────────────────────

_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)
_DOMAIN_RE = re.compile(
    r"\b(?:https?://)?([A-Za-z0-9\-]+\.(?:com|org|net|io|ai|co|app|dev|tech))\b",
    re.IGNORECASE,
)
_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b(?=\s*(?:stock|shares|ticker|NYSE|NASDAQ|$))")
_PHONE_RE = re.compile(
    r"\+?\d[\d\s\-\(\)]{7,}\d"
)
_URL_RE = re.compile(
    r"https?://[^\s<>\"']+", re.IGNORECASE
)

# Capitalized-word person/org heuristic (1–4 consecutive title-cased tokens).
# Single-word matches are filtered against _NAME_STOPWORDS before use.
_CAPITALIZED_NAME_RE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b"
)

# Common English words that appear capitalized but are not proper names.
# Filtered out when the regex produces a single-word match.
_NAME_STOPWORDS = frozenset({
    "The", "A", "An", "This", "That", "These", "Those",
    "He", "She", "It", "We", "They", "You", "I",
    "His", "Her", "Its", "Our", "Their", "My", "Your",
    "Is", "Are", "Was", "Were", "Be", "Been", "Being",
    "Has", "Have", "Had", "Do", "Does", "Did",
    "Will", "Would", "Could", "Should", "May", "Might", "Must",
    "In", "At", "To", "By", "For", "Of", "On", "Off",
    "With", "From", "About", "Into", "Through", "During",
    "And", "Or", "But", "If", "As", "So", "Yet", "Nor",
    "Not", "No", "Yes", "Ok", "Please", "Thanks", "Thank",
    "Here", "There", "Now", "Then", "When", "Where", "How", "Why",
    "What", "Which", "Who", "Whose",
    "Also", "Just", "Very", "More", "Most", "Some", "Any",
    "All", "Both", "Each", "Few", "Many", "Much", "Other",
    "After", "Before", "Over", "Under", "Above", "Below",
    "Between", "Among", "Against", "Along", "Around",
    "Can", "Cannot", "Well", "Even", "Still", "Already",
    "Never", "Always", "Often", "Again", "Since", "Once",
    "Hi", "Hey", "Hello", "Okay", "Sure", "Yeah",
})

# Common org suffixes
_ORG_SUFFIX_RE = re.compile(
    r"\b([A-Z][A-Za-z\s&,\.]{2,50}?)"
    r"(?:\s+(?:Inc\.?|Corp\.?|LLC|Ltd\.?|Co\.?|Group|Foundation|Institute|University|"
    r"School|Hospital|Agency|Association|Society|Department))\b"
)

# Location indicators
_LOCATION_INDICATORS = frozenset({
    "in", "at", "from", "to", "near", "visit", "located", "based", "headquartered",
    "office", "city", "country", "region", "state", "province",
})


@dataclass
class ExtractionCandidate:
    """A candidate entity or relationship found in text."""
    text: str                     # the extracted text span
    entity_type: str              # tentative entity type
    start_char: int
    end_char: int
    confidence: float
    context_window: str           # surrounding text for disambiguation
    linked_entity_id: Optional[str] = None  # if resolved to existing entity


@dataclass
class ExtractionResult:
    entities: List[ExtractionCandidate] = field(default_factory=list)
    relationships: List[dict] = field(default_factory=list)
    extraction_ms: float = 0.0
    source_id: str = ""           # message ID that was processed


class ConversationExtractor:
    """Lightweight post-message entity extractor.

    Uses rule-based NER and regex patterns. Zero LLM calls.
    MUST complete within 50ms for single-message inputs.
    """

    def __init__(
        self,
        min_message_length: int = 20,
        existing_entities: Optional[List[str]] = None,
    ) -> None:
        self._min_length = min_message_length
        self._known_names: List[str] = list(existing_entities or [])

    def _context(self, text: str, start: int, end: int, window: int = 40) -> str:
        return text[max(0, start - window): min(len(text), end + window)]

    async def extract(
        self,
        message_text: str,
        source_id: str,
        existing_entities: Optional[List[str]] = None,
    ) -> ExtractionResult:
        """Extract entity candidates from a single message.

        Args:
            message_text: Raw message content.
            source_id: The message ID for provenance tracking.
            existing_entities: List of known entity names for fast linking.

        Returns:
            ExtractionResult with candidates and elapsed time.
        """
        t0 = time.monotonic()
        result = ExtractionResult(source_id=source_id)

        if len(message_text) < self._min_length:
            result.extraction_ms = (time.monotonic() - t0) * 1000
            return result

        known = set(existing_entities or []) | set(self._known_names)
        seen_spans: List[tuple] = []  # (start, end) to avoid duplicates

        def _overlaps(s: int, e: int) -> bool:
            for a, b in seen_spans:
                if s < b and e > a:
                    return True
            return False

        # ── Emails → person (high confidence) ────────────────────────────────
        for m in _EMAIL_RE.finditer(message_text):
            if _overlaps(m.start(), m.end()):
                continue
            adj = [0.15]  # email pattern bonus
            if m.group() in known:
                adj.append(0.20)
            conf = apply_extraction_adjustments(
                CONFIDENCE_BY_SOURCE["conversation_ner"], adj
            )
            result.entities.append(ExtractionCandidate(
                text=m.group(),
                entity_type="person",
                start_char=m.start(),
                end_char=m.end(),
                confidence=conf,
                context_window=self._context(message_text, m.start(), m.end()),
            ))
            seen_spans.append((m.start(), m.end()))

        # ── URLs → product/company ─────────────────────────────────────────
        for m in _URL_RE.finditer(message_text):
            if _overlaps(m.start(), m.end()):
                continue
            conf = apply_extraction_adjustments(
                CONFIDENCE_BY_SOURCE["conversation_ner"], [0.05]
            )
            result.entities.append(ExtractionCandidate(
                text=m.group(),
                entity_type="product",
                start_char=m.start(),
                end_char=m.end(),
                confidence=conf,
                context_window=self._context(message_text, m.start(), m.end()),
            ))
            seen_spans.append((m.start(), m.end()))

        # ── Org suffix → company ──────────────────────────────────────────
        for m in _ORG_SUFFIX_RE.finditer(message_text):
            full = m.group().strip()
            if _overlaps(m.start(), m.end()):
                continue
            adj = []
            if full in known:
                adj.append(0.20)
            conf = apply_extraction_adjustments(
                CONFIDENCE_BY_SOURCE["conversation_ner"], adj
            )
            result.entities.append(ExtractionCandidate(
                text=full,
                entity_type="company",
                start_char=m.start(),
                end_char=m.end(),
                confidence=conf,
                context_window=self._context(message_text, m.start(), m.end()),
            ))
            seen_spans.append((m.start(), m.end()))

        # ── Capitalized names → person ────────────────────────────────────
        for m in _CAPITALIZED_NAME_RE.finditer(message_text):
            name = m.group().strip()
            if _overlaps(m.start(), m.end()):
                continue
            # Skip single-word all-caps (likely acronym/ticker)
            if name.isupper():
                continue
            is_single_word = " " not in name
            # Skip single-word matches that are common English stopwords
            if is_single_word and name in _NAME_STOPWORDS:
                continue
            adj = []
            if name in known:
                adj.append(0.20)
            # Single-word proper names get lower confidence than multi-word names
            if is_single_word:
                adj.append(-0.10)
            conf = apply_extraction_adjustments(
                CONFIDENCE_BY_SOURCE["conversation_ner"], adj
            )
            # Heuristic: check if preceded by location indicator
            prefix = message_text[max(0, m.start() - 30):m.start()].lower()
            etype = "person"
            for indicator in _LOCATION_INDICATORS:
                if indicator in prefix.split():
                    etype = "location"
                    break
            result.entities.append(ExtractionCandidate(
                text=name,
                entity_type=etype,
                start_char=m.start(),
                end_char=m.end(),
                confidence=conf,
                context_window=self._context(message_text, m.start(), m.end()),
            ))
            seen_spans.append((m.start(), m.end()))

        # ── Domain names → company ────────────────────────────────────────
        for m in _DOMAIN_RE.finditer(message_text):
            if _overlaps(m.start(), m.end()):
                continue
            conf = apply_extraction_adjustments(
                CONFIDENCE_BY_SOURCE["conversation_ner"], [0.05]
            )
            result.entities.append(ExtractionCandidate(
                text=m.group(1),
                entity_type="company",
                start_char=m.start(),
                end_char=m.end(),
                confidence=conf,
                context_window=self._context(message_text, m.start(), m.end()),
            ))
            seen_spans.append((m.start(), m.end()))

        result.extraction_ms = (time.monotonic() - t0) * 1000
        return result
