"""Goal Inference Pipeline — detect implicit goals from conversation context.

Architecture:
  1. Rule-based pass (synchronous, < 5 ms) — keyword/pattern matching
  2. LLM pass (async, optional) — structured interpretation for low-confidence candidates
  3. GoalDeduplicator — semantic similarity check against active goals
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from colony_sidecar.goals.models import Goal, GoalPriority, GoalSource


class IntentSignal(str, Enum):
    """Conversation signal types that may indicate a goal."""
    DESIRE      = "desire"       # "I want to / I need to / I'd like to"
    OBLIGATION  = "obligation"   # "I have to / I must / I should"
    PROBLEM     = "problem"      # "I'm struggling with / This is broken"
    DEADLINE    = "deadline"     # Explicit time reference with task context
    FRUSTRATION = "frustration"  # Repeated problem mentions, negative sentiment
    RECURRING   = "recurring"    # "Every week / daily / each morning"
    DELEGATION  = "delegation"   # "Can you / Could you / Please"


@dataclass
class ConversationMessage:
    """A single message in a conversation."""
    message_id: str
    role: str       # "user" | "assistant" | "system"
    content: str
    timestamp: Optional[float] = None   # Unix epoch


@dataclass
class InferenceCandidate:
    """A candidate goal extracted from conversation context.

    Attributes:
        title:             Inferred goal title.
        description:       Full description including supporting context.
        signals:           Which signal types contributed to this inference.
        confidence:        0.0–1.0 confidence that this is a real goal.
        source_messages:   IDs of conversation messages that triggered inference.
        suggested_deadline: Extracted deadline if any.
        priority:          Inferred priority based on signal type.
    """
    title: str
    description: str
    signals: List[IntentSignal]
    confidence: float
    source_messages: List[str]
    suggested_deadline: Optional[str] = None   # ISO-8601 if detected
    priority: GoalPriority = GoalPriority.NORMAL

    def should_auto_accept(self, threshold: float = 0.85) -> bool:
        """Return True if confidence exceeds auto-accept threshold."""
        return (
            self.confidence >= threshold
            and IntentSignal.DELEGATION in self.signals
        )


@dataclass
class GoalSimilarity:
    """Result of comparing two goals for deduplication."""
    goal_id_a: str
    goal_id_b: str
    similarity_score: float    # 0.0–1.0
    shared_keywords: List[str]
    recommendation: str        # "duplicate" | "merge" | "related" | "distinct"


# ── Rule-based signal patterns ────────────────────────────────────────────────

_DESIRE_PATTERNS = re.compile(
    r"\b(i want to|i need to|i'd like to|i would like to|i wish|i hope to|"
    r"need to|want to|gotta|going to|planning to)\b",
    re.IGNORECASE,
)
_OBLIGATION_PATTERNS = re.compile(
    r"\b(i have to|i must|i should|i ought to|have to|must|should|"
    r"need to finish|deadline|due by|due on)\b",
    re.IGNORECASE,
)
_PROBLEM_PATTERNS = re.compile(
    r"\b(struggling with|broken|not working|failing|error|bug|issue|problem|"
    r"can't figure out|don't know how|stuck on|frustrated with|having trouble)\b",
    re.IGNORECASE,
)
_DEADLINE_PATTERNS = re.compile(
    r"\b(by (monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"by (tomorrow|tonight|end of (day|week|month))|"
    r"before (the meeting|the call|the deadline)|"
    r"(this|next) (monday|tuesday|wednesday|thursday|friday|week|month)|"
    r"in (\d+) (hours?|days?|weeks?))\b",
    re.IGNORECASE,
)
_RECURRING_PATTERNS = re.compile(
    r"\b(every (day|week|month|morning|evening|monday|tuesday|wednesday|"
    r"thursday|friday)|daily|weekly|monthly|each (morning|day|week))\b",
    re.IGNORECASE,
)
_DELEGATION_PATTERNS = re.compile(
    r"\b(can you|could you|please|would you|help me|remind me|schedule|"
    r"set up|create|make|send|write|draft|prepare|organize)\b",
    re.IGNORECASE,
)
_FRUSTRATION_PATTERNS = re.compile(
    r"\b(frustrated|annoying|ridiculous|terrible|awful|keeps (breaking|failing)|"
    r"still broken|why doesn'?t|why isn'?t|never works|always fails)\b",
    re.IGNORECASE,
)

_PRIORITY_SIGNALS = {
    IntentSignal.OBLIGATION: GoalPriority.HIGH,
    IntentSignal.DEADLINE: GoalPriority.HIGH,
    IntentSignal.FRUSTRATION: GoalPriority.HIGH,
    IntentSignal.DELEGATION: GoalPriority.NORMAL,
    IntentSignal.DESIRE: GoalPriority.NORMAL,
    IntentSignal.PROBLEM: GoalPriority.NORMAL,
    IntentSignal.RECURRING: GoalPriority.LOW,
}


def _extract_signals(text: str) -> List[Tuple[IntentSignal, float]]:
    """Extract signals from text, returning (signal, weight) pairs."""
    results: List[Tuple[IntentSignal, float]] = []
    if _DESIRE_PATTERNS.search(text):
        results.append((IntentSignal.DESIRE, 0.6))
    if _OBLIGATION_PATTERNS.search(text):
        results.append((IntentSignal.OBLIGATION, 0.7))
    if _PROBLEM_PATTERNS.search(text):
        results.append((IntentSignal.PROBLEM, 0.55))
    if _DEADLINE_PATTERNS.search(text):
        results.append((IntentSignal.DEADLINE, 0.65))
    if _RECURRING_PATTERNS.search(text):
        results.append((IntentSignal.RECURRING, 0.5))
    if _DELEGATION_PATTERNS.search(text):
        results.append((IntentSignal.DELEGATION, 0.75))
    if _FRUSTRATION_PATTERNS.search(text):
        results.append((IntentSignal.FRUSTRATION, 0.6))
    return results


def _compute_confidence(signals: List[Tuple[IntentSignal, float]]) -> float:
    """Combine multiple signal weights into a single confidence score."""
    if not signals:
        return 0.0
    # Combine with diminishing returns: 1 - product(1 - w)
    combined = 1.0
    for _, w in signals:
        combined *= (1.0 - w)
    return round(1.0 - combined, 4)


def _infer_priority(signals: List[IntentSignal]) -> GoalPriority:
    """Select highest priority implied by the signal set."""
    best = GoalPriority.BACKGROUND
    for sig in signals:
        p = _PRIORITY_SIGNALS.get(sig, GoalPriority.NORMAL)
        if p.value > best.value:
            best = p
    return best


def _extract_title(text: str) -> str:
    """Produce a short title from the message text (max 80 chars)."""
    # Strip leading/trailing whitespace and trim to 80 chars
    cleaned = re.sub(r"\s+", " ", text.strip())
    # Remove common filler phrases
    cleaned = re.sub(
        r"^(can you|could you|please|i need to|i want to|i have to|"
        r"i should|i must|help me)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    # Capitalize
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned[:80].rstrip(".,;:")


class GoalInferencePipeline:
    """Detect implicit goals from conversation messages.

    The rule-based pass runs synchronously on every user message.
    For low-confidence candidates the engine can optionally run an LLM
    interpretation pass (not implemented here — hook provided via override).
    """

    MIN_CONFIDENCE = 0.35   # Below this, no candidate is produced

    def __init__(self, min_confidence: float = MIN_CONFIDENCE) -> None:
        self.min_confidence = min_confidence

    def process_message(
        self,
        message: ConversationMessage,
        history: Optional[List[ConversationMessage]] = None,
    ) -> Optional[InferenceCandidate]:
        """Run the rule-based inference pass on a single message.

        Returns an InferenceCandidate if a goal-like signal is detected,
        otherwise None.

        Must complete in < 5 ms for typical inputs.
        """
        if message.role != "user":
            return None

        text = message.content
        signals_weighted = _extract_signals(text)
        if not signals_weighted:
            return None

        confidence = _compute_confidence(signals_weighted)
        if confidence < self.min_confidence:
            return None

        signals = [s for s, _ in signals_weighted]
        priority = _infer_priority(signals)
        title = _extract_title(text)
        if not title:
            return None

        # Extract deadline hint
        deadline_match = _DEADLINE_PATTERNS.search(text)
        suggested_deadline = deadline_match.group(0) if deadline_match else None

        return InferenceCandidate(
            title=title,
            description=text.strip(),
            signals=signals,
            confidence=confidence,
            source_messages=[message.message_id],
            suggested_deadline=suggested_deadline,
            priority=priority,
        )

    def process_history(
        self,
        history: List[ConversationMessage],
    ) -> List[InferenceCandidate]:
        """Process a conversation history, returning all detected candidates."""
        candidates = []
        for msg in history:
            cand = self.process_message(msg, history=history)
            if cand:
                candidates.append(cand)
        return candidates


# ── Goal Deduplication ────────────────────────────────────────────────────────

def _tokenize(text: str) -> set:
    """Simple word-level tokenizer for keyword overlap scoring."""
    # Remove stop words and short tokens
    stop_words = {
        "i", "a", "an", "the", "to", "is", "are", "was", "were",
        "be", "been", "being", "have", "has", "had", "do", "does",
        "did", "will", "would", "could", "should", "may", "might",
        "shall", "can", "need", "want", "going", "get", "got",
        "my", "me", "we", "us", "it", "its", "this", "that",
        "and", "or", "but", "if", "in", "on", "at", "by", "for",
        "with", "about", "as", "of", "from",
    }
    words = re.findall(r"[a-z]+", text.lower())
    return {w for w in words if w not in stop_words and len(w) > 2}


def _jaccard_similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class GoalDeduplicator:
    """Check and merge candidate goals against the active goal set."""

    DUPLICATE_THRESHOLD = 0.90
    MERGE_THRESHOLD = 0.70

    def check(
        self,
        candidate: InferenceCandidate,
        existing: List[Goal],
    ) -> Optional[GoalSimilarity]:
        """
        Return a GoalSimilarity if the candidate overlaps with an existing goal.
        Returns None if the candidate is distinct from all existing goals.
        """
        if not existing:
            return None

        candidate_tokens = _tokenize(candidate.title + " " + candidate.description)
        best: Optional[GoalSimilarity] = None

        for goal in existing:
            if goal.is_terminal():
                continue
            goal_tokens = _tokenize(goal.title + " " + goal.description)
            score = _jaccard_similarity(candidate_tokens, goal_tokens)
            shared = sorted(candidate_tokens & goal_tokens)

            if score >= self.MERGE_THRESHOLD:
                if score >= self.DUPLICATE_THRESHOLD:
                    rec = "duplicate"
                else:
                    rec = "merge"

                sim = GoalSimilarity(
                    goal_id_a="candidate",
                    goal_id_b=goal.goal_id,
                    similarity_score=score,
                    shared_keywords=shared[:10],
                    recommendation=rec,
                )
                if best is None or score > best.similarity_score:
                    best = sim

        return best

    def merge(self, base: Goal, candidate: InferenceCandidate) -> Goal:
        """Enrich an existing goal with additional context from a new candidate."""
        # Extend description if the candidate has more detail
        if len(candidate.description) > len(base.description):
            base.description = candidate.description

        # Adopt deadline if base has none
        if candidate.suggested_deadline and base.deadline is None:
            base.context["inferred_deadline_hint"] = candidate.suggested_deadline

        # Upgrade priority if candidate is higher
        if candidate.priority.value > base.priority.value:
            base.priority = candidate.priority

        # Merge source message IDs into context
        existing_msgs = base.context.get("source_messages", [])
        combined = list(set(existing_msgs + candidate.source_messages))
        base.context["source_messages"] = combined

        return base
