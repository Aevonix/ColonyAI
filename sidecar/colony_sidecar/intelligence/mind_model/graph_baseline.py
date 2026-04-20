"""GraphBaselineStore — person behavioral baselines stored on Neo4j Person nodes.

Uses Welford's online algorithm for O(1) incremental mean/std updates.
No cache, no TTL — Neo4j is the single source of truth.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import List, Optional

from colony_sidecar.intelligence.mind_model.signal_collector import (
    BaselineStore,
    PersonBaseline,
)

logger = logging.getLogger(__name__)

# Default preferred hours when insufficient data exists
_DEFAULT_PREFERRED_HOURS = [9, 10, 11, 14, 15, 16]


@dataclass
class PersonBaselineImpl:
    """Concrete PersonBaseline implementation."""
    length_mean: float
    length_std: float
    preferred_hours: List[int]


class GraphBaselineStore:
    """BaselineStore backed by Person node properties in Neo4j.

    Baselines live as properties on the :Person node — no new nodes,
    no new infrastructure.  Welford's algorithm gives O(1) incremental
    updates for mean and standard deviation without storing raw history.

    Falls back to neutral defaults when Neo4j is unavailable.
    """

    def __init__(self, graph) -> None:
        self._graph = graph

    async def get(self, person_id: str) -> PersonBaselineImpl:
        """Read baseline from Person node. Returns empty baseline on miss."""
        try:
            row = await self._graph._run_get_baseline(person_id)
            if row is None:
                return PersonBaselineImpl(
                    length_mean=0.0,
                    length_std=0.0,
                    preferred_hours=list(_DEFAULT_PREFERRED_HOURS),
                )
            return PersonBaselineImpl(
                length_mean=row.get("length_mean") or 0.0,
                length_std=row.get("length_std") or 0.0,
                preferred_hours=self._preferred_hours(
                    row.get("hour_histogram")
                ),
            )
        except Exception as exc:
            logger.debug("GraphBaselineStore.get failed for %s: %s", person_id, exc)
            return PersonBaselineImpl(
                length_mean=0.0,
                length_std=0.0,
                preferred_hours=list(_DEFAULT_PREFERRED_HOURS),
            )

    async def record_message(self, person_id: str, length: int, hour: int) -> None:
        """Incrementally update baseline on the Person node via Welford."""
        try:
            current = await self._graph._run_get_baseline(person_id)
            count = (current or {}).get("msg_count", 0) or 0
            mean = (current or {}).get("length_mean", 0.0) or 0.0
            m2 = (current or {}).get("length_m2", 0.0) or 0.0
            hist_raw = (current or {}).get("hour_histogram")

            # Parse histogram
            if hist_raw:
                try:
                    histogram = json.loads(hist_raw)
                    if len(histogram) != 24:
                        histogram = [0] * 24
                except (json.JSONDecodeError, TypeError):
                    histogram = [0] * 24
            else:
                histogram = [0] * 24

            # Welford update
            count += 1
            delta = length - mean
            mean += delta / count
            delta2 = length - mean
            m2 += delta * delta2
            std = math.sqrt(m2 / count) if count > 1 else 0.0

            # Hour histogram
            histogram[hour] = histogram[hour] + 1

            # Write back
            await self._graph._run_update_baseline(
                person_id=person_id,
                msg_count=count,
                length_mean=mean,
                length_m2=m2,
                length_std=std,
                hour_histogram=json.dumps(histogram),
            )
        except Exception as exc:
            logger.debug("GraphBaselineStore.record_message failed for %s: %s", person_id, exc)

    async def store_signal(self, signal: "Signal") -> None:
        """Persist a signal to the graph. Best-effort — logs on failure."""
        try:
            await self._graph.store_signal(signal)
        except Exception as exc:
            logger.debug("GraphBaselineStore.store_signal failed: %s", exc)

    @staticmethod
    def _preferred_hours(histogram_json: Optional[str]) -> List[int]:
        """Hours above the median activity level."""
        if not histogram_json:
            return list(_DEFAULT_PREFERRED_HOURS)
        try:
            counts = json.loads(histogram_json)
        except (json.JSONDecodeError, TypeError):
            return list(_DEFAULT_PREFERRED_HOURS)
        if len(counts) != 24 or sum(counts) < 5:
            return list(_DEFAULT_PREFERRED_HOURS)
        median = sorted(counts)[12]
        if median == 0:
            return list(_DEFAULT_PREFERRED_HOURS)
        return [h for h, c in enumerate(counts) if c >= median]
