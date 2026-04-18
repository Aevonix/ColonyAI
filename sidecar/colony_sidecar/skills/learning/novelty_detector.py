"""Colony Skills — novelty detection for task solutions.

Uses the local vector store (LanceDB) for scalable semantic distance
computation.  Falls back to structural + dependency scoring when vector
search is unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, TYPE_CHECKING

import numpy as np

from colony_sidecar.skills.registry import SkillRegistry
from colony_sidecar.skills.models import SkillSummary, TaskSolution

if TYPE_CHECKING:
    from colony_sidecar.vector.embedder import EmbeddingPipeline
    from colony_sidecar.vector.store import VectorStore

logger = logging.getLogger(__name__)

_NOVELTY_THRESHOLD_DEFAULT = 0.65


@dataclass
class NoveltyResult:
    """Result of a novelty evaluation."""
    score: float                   # 0.0 = redundant, 1.0 = entirely new
    closest_skill_id: Optional[str]
    closest_similarity: float      # cosine similarity to nearest skill
    recommendation: str            # "capture" | "skip" | "update_existing"
    reasoning: str


class NoveltyDetector:
    """Scores a TaskSolution against the skill registry to determine
    whether it is sufficiently novel to warrant packaging.

    Uses three signals with weighted combination:
      - Semantic embedding distance (weight 0.50) — via vector store
      - Structural fingerprint distance (weight 0.30)
      - Dependency Jaccard distance (weight 0.20)

    When no vector store is configured, semantic weight is redistributed
    to structural (0.60) and dependency (0.40).
    """

    def __init__(
        self,
        registry: SkillRegistry,
        novelty_threshold: float = _NOVELTY_THRESHOLD_DEFAULT,
    ) -> None:
        self._registry = registry
        self._threshold = novelty_threshold
        self._vector_store: Optional["VectorStore"] = None
        self._embedder: Optional["EmbeddingPipeline"] = None

    def set_vector_store(
        self, store: "VectorStore", embedder: "EmbeddingPipeline"
    ) -> None:
        """Wire vector store + embedder for scalable semantic search."""
        self._vector_store = store
        self._embedder = embedder

    async def score(self, solution: TaskSolution) -> NoveltyResult:
        """Score the novelty of a task solution."""
        summaries: List[SkillSummary] = await self._registry.list_summaries()
        if not summaries:
            return NoveltyResult(
                score=1.0,
                closest_skill_id=None,
                closest_similarity=0.0,
                recommendation="capture",
                reasoning="Registry is empty; all solutions are novel.",
            )

        semantic_scores = await self._semantic_distances(solution, summaries)
        structural_scores = self._structural_distances(solution, summaries)
        dep_scores = self._dependency_distances(solution, summaries)

        # If vector store is available, use standard weights.
        # Otherwise, redistribute semantic weight to other signals.
        has_semantic = self._vector_store is not None and solution.embedding is not None
        if has_semantic:
            combined = (
                0.50 * semantic_scores
                + 0.30 * structural_scores
                + 0.20 * dep_scores
            )
        else:
            combined = 0.60 * structural_scores + 0.40 * dep_scores

        best_idx = int(np.argmin(combined))
        similarity = float(1.0 - combined[best_idx])
        novelty = float(combined[best_idx])

        if novelty >= self._threshold:
            recommendation = "capture"
            reasoning = f"Novel solution (score {novelty:.2f} ≥ threshold {self._threshold})."
        elif similarity >= 0.92:
            recommendation = "skip"
            reasoning = (
                f"Near-duplicate of {summaries[best_idx].skill_id} "
                f"(similarity {similarity:.2f})."
            )
        else:
            recommendation = "update_existing"
            reasoning = (
                f"Partial match to {summaries[best_idx].skill_id}; "
                f"consider version bump."
            )

        return NoveltyResult(
            score=novelty,
            closest_skill_id=summaries[best_idx].skill_id,
            closest_similarity=similarity,
            recommendation=recommendation,
            reasoning=reasoning,
        )

    async def _semantic_distances(
        self, solution: TaskSolution, summaries: List[SkillSummary]
    ) -> np.ndarray:
        """Compute semantic distances via vector store ANN search.

        Falls back to O(1) uniform distance if vector store is not available.
        """
        if solution.embedding is None or not summaries:
            return np.ones(len(summaries))

        # Use vector store for scalable ANN search
        if self._vector_store is not None:
            try:
                from colony_sidecar.vector.collections import Collection
                results = await self._vector_store.search(
                    collection=Collection.SKILLS,
                    query_vector=solution.embedding,
                    limit=min(20, len(summaries)),
                )
                # Build a skill_id → distance map
                score_map = {r.metadata.get("skill_id", r.id): 1.0 - r.score for r in results}
                distances = []
                for s in summaries:
                    distances.append(score_map.get(s.skill_id, 1.0))
                return np.array(distances)
            except Exception as exc:
                logger.warning("Vector store skill search failed, falling back to in-memory: %s", exc)

        # Fallback: in-memory cosine (original O(n) implementation)
        sol_vec = np.array(solution.embedding, dtype=float)
        skill_vecs_list = [s.embedding for s in summaries if s.embedding is not None]
        if not skill_vecs_list:
            return np.ones(len(summaries))
        skill_vecs = np.array(skill_vecs_list, dtype=float)
        if skill_vecs.ndim < 2:
            return np.ones(len(summaries))
        norms = np.linalg.norm(skill_vecs, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        normalized = skill_vecs / norms
        sol_norm_val = np.linalg.norm(sol_vec)
        sol_normalized = sol_vec / (sol_norm_val if sol_norm_val != 0 else 1.0)
        cosine_sim = normalized @ sol_normalized
        return 1.0 - np.clip(cosine_sim, 0.0, 1.0)

    def _structural_distances(
        self, solution: TaskSolution, summaries: List[SkillSummary]
    ) -> np.ndarray:
        sol_steps = solution.step_fingerprint or []
        distances = []
        for summary in summaries:
            ref_steps = summary.step_fingerprint or []
            dist = self._normalized_edit_distance(sol_steps, ref_steps)
            distances.append(dist)
        return np.array(distances)

    def _dependency_distances(
        self, solution: TaskSolution, summaries: List[SkillSummary]
    ) -> np.ndarray:
        sol_deps = set(solution.dependencies or [])
        distances = []
        for summary in summaries:
            ref_deps = set(summary.dependencies or [])
            if not sol_deps and not ref_deps:
                distances.append(0.0)
            else:
                intersection = len(sol_deps & ref_deps)
                union = len(sol_deps | ref_deps)
                distances.append(1.0 - intersection / union if union > 0 else 1.0)
        return np.array(distances)

    @staticmethod
    def _normalized_edit_distance(a: List[str], b: List[str]) -> float:
        if not a and not b:
            return 0.0
        m, n = len(a), len(b)
        dp = list(range(n + 1))
        for i in range(1, m + 1):
            prev, dp[0] = dp[0], i
            for j in range(1, n + 1):
                temp = dp[j]
                dp[j] = prev if a[i - 1] == b[j - 1] else 1 + min(prev, dp[j], dp[j - 1])
                prev = temp
        return dp[n] / max(m, n)
