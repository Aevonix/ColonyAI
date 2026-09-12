"""Entity resolver: resolves extraction candidates against the world model.

Resolution priority order (first match wins):
1. Exact external ID match   → MERGE (confidence ≥ 0.98)
2. Exact canonical name match (case-insensitive) → MERGE (confidence ≥ 0.90)
3. Alias list match          → MERGE (confidence ≥ 0.85)
4. High string similarity (≥ 0.92) + same type → PROPOSE MERGE
5. Property match (domain, email, ticker) → MERGE (confidence ≥ 0.90)
6. Moderate similarity (0.75–0.92) + same type → FLAG
7. No match                  → CREATE NEW ENTITY
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..store import WorldModelStore
from ..extraction.conversation_extractor import ExtractionCandidate
from ..entities import BaseEntity


def _jaro_winkler(s1: str, s2: str) -> float:
    """Compute Jaro-Winkler similarity between two strings.

    Returns a value in [0.0, 1.0].
    Pure Python implementation; no external dependencies.
    """
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0

    s1 = s1.lower()
    s2 = s2.lower()
    len1, len2 = len(s1), len(s2)

    match_distance = max(len1, len2) // 2 - 1
    match_distance = max(match_distance, 0)

    s1_matches = [False] * len1
    s2_matches = [False] * len2

    matches = 0
    transpositions = 0

    for i in range(len1):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    jaro = (
        matches / len1 + matches / len2 + (matches - transpositions / 2) / matches
    ) / 3

    # Winkler prefix bonus (up to 4 chars)
    prefix = 0
    for i in range(min(4, len1, len2)):
        if s1[i] == s2[i]:
            prefix += 1
        else:
            break

    return jaro + prefix * 0.1 * (1 - jaro)


class ResolutionAction(str, Enum):
    CREATE = "create"           # no match found; create new entity
    MERGE = "merge"             # confident match; merge immediately
    PROPOSE_MERGE = "propose"   # uncertain match; queue for review
    FLAG = "flag"               # possible match; log and continue as new


@dataclass
class ResolutionResult:
    action: ResolutionAction
    matched_entity_id: Optional[str]  # existing entity ID if matched
    match_confidence: float
    match_reason: str              # human-readable explanation
    candidate_id: Optional[str] = None  # new entity ID if CREATE


class EntityResolver:
    """Resolves extraction candidates against existing world model entities."""

    def __init__(
        self,
        store: "WorldModelStore",
        auto_merge_threshold: float = 0.85,
        propose_merge_threshold: float = 0.70,
        similarity_auto_merge: float = 0.92,
        similarity_propose: float = 0.75,
    ) -> None:
        self._store = store
        self._auto_merge_threshold = auto_merge_threshold
        self._propose_threshold = propose_merge_threshold
        self._sim_auto = similarity_auto_merge
        self._sim_propose = similarity_propose

    async def resolve(
        self,
        candidate: ExtractionCandidate,
        entity_type: str,
    ) -> ResolutionResult:
        """Attempt to resolve a candidate to an existing entity.

        Runs the priority-ordered resolution algorithm.
        """
        text = candidate.text.strip()

        # ── Step 1: External ID match ─────────────────────────────────────
        if "@" in text:
            existing = await self._store.get_entity_by_external_id("email", text)
            if existing:
                return ResolutionResult(
                    action=ResolutionAction.MERGE,
                    matched_entity_id=existing.id,
                    match_confidence=0.98,
                    match_reason="Exact external ID match (email)",
                )

        # ── Step 2: Exact canonical name match ────────────────────────────
        candidates = await self._store.find_entities(
            query=text, entity_type=entity_type, min_confidence=0.0, limit=50
        )
        for entity in candidates:
            if entity.name.lower() == text.lower():
                return ResolutionResult(
                    action=ResolutionAction.MERGE,
                    matched_entity_id=entity.id,
                    match_confidence=0.90,
                    match_reason="Exact canonical name match",
                )

        # ── Step 3: Alias list match ──────────────────────────────────────
        for entity in candidates:
            for alias in entity.aliases:
                if alias.lower() == text.lower():
                    return ResolutionResult(
                        action=ResolutionAction.MERGE,
                        matched_entity_id=entity.id,
                        match_confidence=0.85,
                        match_reason=f"Alias match: '{alias}'",
                    )

        # ── Steps 4 & 6: String similarity ───────────────────────────────
        best_sim = 0.0
        best_entity: Optional[BaseEntity] = None
        for entity in candidates:
            sim = await self.compute_string_similarity(text, entity.name)
            if sim > best_sim:
                best_sim = sim
                best_entity = entity

        if best_entity and best_sim >= self._sim_auto:
            return ResolutionResult(
                action=ResolutionAction.PROPOSE_MERGE,
                matched_entity_id=best_entity.id,
                match_confidence=best_sim,
                match_reason=f"High string similarity ({best_sim:.2f})",
            )

        if best_entity and best_sim >= self._sim_propose:
            return ResolutionResult(
                action=ResolutionAction.FLAG,
                matched_entity_id=best_entity.id,
                match_confidence=best_sim,
                match_reason=f"Moderate string similarity ({best_sim:.2f})",
            )

        # ── Step 7: No match — create new ────────────────────────────────
        return ResolutionResult(
            action=ResolutionAction.CREATE,
            matched_entity_id=None,
            match_confidence=0.0,
            match_reason="No match found",
        )

    async def compute_string_similarity(self, a: str, b: str) -> float:
        """Jaro-Winkler similarity between two name strings."""
        return _jaro_winkler(a, b)

    async def resolve_entity_against_store(
        self,
        entity: BaseEntity,
    ) -> ResolutionResult:
        """Resolve a full BaseEntity against the store.

        More thorough than ExtractionCandidate resolution: checks external IDs,
        domain, ticker, etc.
        """
        # External ID matches
        for key, value in entity.external_ids.items():
            existing = await self._store.get_entity_by_external_id(key, value)
            if existing and existing.id != entity.id:
                return ResolutionResult(
                    action=ResolutionAction.MERGE,
                    matched_entity_id=existing.id,
                    match_confidence=0.98,
                    match_reason=f"Exact external ID match ({key}={value})",
                )

        # Name-based resolution
        from ..extraction.conversation_extractor import ExtractionCandidate
        candidate = ExtractionCandidate(
            text=entity.name,
            entity_type=entity.entity_type,
            start_char=0,
            end_char=len(entity.name),
            confidence=entity.confidence,
            context_window="",
        )
        return await self.resolve(candidate, entity.entity_type)
