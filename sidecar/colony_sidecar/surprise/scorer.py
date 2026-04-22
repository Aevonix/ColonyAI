"""Surprise Scorer — compute surprise scores for observations.

Scoring logic:
- No matching pattern: surprise = 0.7 (moderately surprising)
- Pattern violated: surprise = 0.5 + (pattern_confidence * 0.5)
- Low-frequency pattern match: surprise = 0.2 (rare but known)
- High-frequency pattern match: surprise = 0.0 (expected)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def compute_surprise(
    observation: str,
    *,
    pattern_store: Any = None,
    observation_type: str = "",
    observation_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute a surprise score for an observation.

    Returns dict with:
      - surprise_score: float (0.0 to 1.0)
      - expected: what was expected (if a pattern was found)
      - pattern_id: the matched/violated pattern ID (if any)
      - reasoning: human-readable explanation
    """
    if pattern_store is None:
        return {
            "surprise_score": 0.5,
            "expected": None,
            "pattern_id": None,
            "reasoning": "No pattern store available, defaulting to moderate surprise",
        }

    # Look for matching patterns by searching descriptions.
    try:
        all_patterns = pattern_store.list_patterns(active_only=True, limit=100)
    except Exception:
        return {
            "surprise_score": 0.5,
            "expected": None,
            "pattern_id": None,
            "reasoning": "Could not query pattern store",
        }

    # Find patterns that mention entities from the observation.
    matching = []
    obs_lower = observation.lower()
    obs_words = set(obs_lower.split())

    for pattern in all_patterns.get("patterns", []):
        desc_lower = pattern.get("description", "").lower()
        # Check for word overlap.
        desc_words = set(desc_lower.split())
        overlap = obs_words & desc_words
        if overlap or observation_type == pattern.get("pattern_type", ""):
            matching.append(pattern)

    if not matching:
        return {
            "surprise_score": 0.7,
            "expected": None,
            "pattern_id": None,
            "reasoning": "No matching pattern found for this observation",
        }

    # Check if the observation fits the pattern or violates it.
    best_pattern = max(matching, key=lambda p: p.get("frequency", 0) * p.get("confidence", 0.5))
    freq = best_pattern.get("frequency", 1)
    conf = best_pattern.get("confidence", 0.5)

    if freq >= 5:
        # High-frequency pattern match — expected.
        return {
            "surprise_score": 0.0,
            "expected": best_pattern["description"],
            "pattern_id": best_pattern["id"],
            "reasoning": f"Matches high-frequency pattern (seen {freq} times)",
        }
    elif freq >= 2:
        # Low-frequency pattern match — rare but known.
        return {
            "surprise_score": 0.2,
            "expected": best_pattern["description"],
            "pattern_id": best_pattern["id"],
            "reasoning": f"Matches low-frequency pattern (seen {freq} times)",
        }
    else:
        # Pattern seen only once — could be a violation.
        score = 0.5 + (conf * 0.5)
        return {
            "surprise_score": round(score, 4),
            "expected": best_pattern["description"],
            "pattern_id": best_pattern["id"],
            "reasoning": f"Pattern seen only once (confidence {conf:.1f}), possible violation",
        }
