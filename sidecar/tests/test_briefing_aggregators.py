"""Real briefing aggregators over the anomaly detector and the connection
discoverer: mapping, severity labels, tz handling, dismissal filtering."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from colony_sidecar.briefings.aggregators import (
    AnomalyDetectorAggregator,
    DiscovererSynthesisAggregator,
)


class _AType(str, Enum):
    SUDDEN_SILENCE = "sudden_silence"
    TONE_SHIFT = "tone_shift"


@dataclass
class _FakeAnomaly:
    id: str
    type: _AType
    description: str
    severity: float
    detected_at: Optional[datetime] = None


class _FakeDetector:
    def __init__(self, anomalies):
        self._anomalies = anomalies
        self.last_floor = None

    def get_recent(self, min_severity=0.6, limit=20):
        self.last_floor = min_severity
        return [a for a in self._anomalies if a.severity >= min_severity][:limit]


def test_anomaly_aggregator_maps_and_labels():
    naive = datetime.now()                          # detector stamps naive local
    det = _FakeDetector([
        _FakeAnomaly("a1", _AType.SUDDEN_SILENCE, "gone quiet", 0.9, naive),
        _FakeAnomaly("a2", _AType.TONE_SHIFT, "tone colder", 0.65, naive),
        _FakeAnomaly("a3", _AType.TONE_SHIFT, "minor blip", 0.4, naive),
    ])
    agg = AnomalyDetectorAggregator(detector_provider=lambda: det)

    out = agg.get_active_anomalies(min_severity="warning")
    assert det.last_floor == 0.6
    assert [(a.anomaly_id, a.severity) for a in out] == [
        ("a1", "critical"), ("a2", "warning")]
    assert out[0].source == "sudden_silence"
    assert all(a.detected_at.tzinfo is not None for a in out)   # tz-aware out

    crit = agg.get_active_anomalies(min_severity="critical")
    assert [a.anomaly_id for a in crit] == ["a1"]


def test_anomaly_aggregator_new_since_and_missing_detector():
    old = datetime.now() - timedelta(days=3)
    new = datetime.now()
    det = _FakeDetector([
        _FakeAnomaly("old", _AType.TONE_SHIFT, "stale", 0.7, old),
        _FakeAnomaly("new", _AType.TONE_SHIFT, "fresh", 0.7, new),
    ])
    agg = AnomalyDetectorAggregator(detector_provider=lambda: det)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert [a.anomaly_id for a in agg.get_new_since(since)] == ["new"]

    empty = AnomalyDetectorAggregator(detector_provider=lambda: None)
    assert empty.get_active_anomalies() == []


@dataclass
class _FakeConnection:
    id: str
    description: str
    confidence: float
    source_domain: str = "communication"
    target_domain: str = "health"
    observation_count: int = 1


class _FakeDiscoverer:
    def __init__(self, conns):
        self._conns = conns
        self.last_min_conf = None

    async def discover_connections(self, person_id=None, domain=None,
                                   min_confidence=0.5):
        self.last_min_conf = min_confidence
        return [c for c in self._conns if c.confidence >= min_confidence]


class _FakeInsightStore:
    def __init__(self, dismissed):
        self._d = set(dismissed)

    def list_dismissed(self):
        return self._d


def test_synthesis_aggregator_maps_filters_dismissed():
    disc = _FakeDiscoverer([
        _FakeConnection("c1", "late nights precede short replies", 0.9,
                        observation_count=4),
        _FakeConnection("c2", "dismissed pattern", 0.85),
        _FakeConnection("c3", "weak hunch", 0.55),
    ])
    agg = DiscovererSynthesisAggregator(
        discoverer_provider=lambda: disc,
        insight_store_provider=lambda: _FakeInsightStore({"c2"}))

    out = agg.get_high_confidence_insights(min_confidence=0.8)
    assert disc.last_min_conf == 0.8
    assert [i.insight_id for i in out] == ["c1"]
    assert out[0].domains == ["communication", "health"]
    assert out[0].confidence == 0.9

    pats = agg.get_weekly_patterns(datetime.now(timezone.utc),
                                   datetime.now(timezone.utc))
    assert pats == ["late nights precede short replies"]   # only obs_count > 1


def test_synthesis_aggregator_missing_subsystems():
    agg = DiscovererSynthesisAggregator(discoverer_provider=lambda: None,
                                        insight_store_provider=lambda: None)
    assert agg.get_high_confidence_insights() == []
    assert agg.get_weekly_patterns(None, None) == []
