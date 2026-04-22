"""Section engagement tracking with EMA scoring."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from colony_sidecar.briefings.models import SectionEngagementRecord

# Signal weights for EMA scoring
_SIGNAL_WEIGHTS: Dict[str, float] = {
    "read": 0.7,
    "acted": 1.0,
    "shared": 0.9,
    "dismissed": 0.0,
}

_EMA_ALPHA = 0.3
_BASELINE = 0.5


@dataclass
class _SectionStats:
    score: float = _BASELINE
    total: int = 0
    dismissals: int = 0


class SectionEngagementTracker:
    """Tracks per-section engagement scores using exponential moving average."""

    def __init__(self, store=None) -> None:
        self._store = store
        self._stats: Dict[str, _SectionStats] = defaultdict(_SectionStats)

        if store is not None:
            for r in store.get_engagement_records():
                self._apply(r, persist=False)

    def _apply(self, record: SectionEngagementRecord, persist: bool = True) -> None:
        stats = self._stats[record.section_name]
        weight = _SIGNAL_WEIGHTS.get(record.signal, 0.0)
        stats.score = _EMA_ALPHA * weight + (1 - _EMA_ALPHA) * stats.score
        stats.total += 1
        if record.signal == "dismissed":
            stats.dismissals += 1
        if persist and self._store is not None:
            self._store.record_engagement(record)

    def record(self, record: SectionEngagementRecord) -> None:
        self._apply(record)

    def get_section_scores(self) -> Dict[str, float]:
        return {name: s.score for name, s in self._stats.items()}

    def get_suppression_candidates(
        self,
        min_dismissals: int = 5,
        dismissal_rate_threshold: float = 0.80,
    ) -> List[str]:
        candidates = []
        for name, stats in self._stats.items():
            if stats.dismissals >= min_dismissals and stats.total > 0:
                rate = stats.dismissals / stats.total
                if rate >= dismissal_rate_threshold:
                    candidates.append(name)
        return candidates

    def record_briefing_read(self, briefing_id: str, section_names: List[str]) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for name in section_names:
            self.record(SectionEngagementRecord(
                section_name=name,
                briefing_id=briefing_id,
                signal="read",
                recorded_at=now,
            ))

    def record_dismissal(self, briefing_id: str, section_name: str) -> None:
        from datetime import datetime, timezone
        self.record(SectionEngagementRecord(
            section_name=section_name,
            briefing_id=briefing_id,
            signal="dismissed",
            recorded_at=datetime.now(timezone.utc),
        ))

    def record_action(self, briefing_id: str, section_name: str) -> None:
        from datetime import datetime, timezone
        self.record(SectionEngagementRecord(
            section_name=section_name,
            briefing_id=briefing_id,
            signal="acted",
            recorded_at=datetime.now(timezone.utc),
        ))
