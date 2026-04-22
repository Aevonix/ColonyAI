"""Relationship anomaly detection."""
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime, timedelta
from enum import Enum


class AnomalyType(str, Enum):
    # Contact-behavior anomalies (existing)
    SUDDEN_SILENCE = "sudden_silence"
    SENTIMENT_SHIFT = "sentiment_shift"
    ENGAGEMENT_DROP = "engagement_drop"
    ABNORMAL_LATENCY = "abnormal_latency"
    UNUSUAL_FREQUENCY = "unusual_frequency"

    # Colony-behavior anomalies (new)
    # Colony's own actions can be anomalous: if Colony's last 5 initiatives all
    # went unanswered, that is significant relational information.
    COLONY_INITIATIVE_IGNORED = "colony_initiative_ignored"
    COLONY_EFFECTIVENESS_DECLINE = "colony_effectiveness_decline"


@dataclass
class Anomaly:
    anomaly_type: AnomalyType
    person_id: str
    severity: float  # 0-1
    description: str
    detected_at: datetime
    evidence: dict
    # "contact" | "colony" | "bilateral" — who the anomaly is about
    colony_layer_context: str = "contact"


class AnomalyDetector:
    """Detect relationship anomalies."""

    # Thresholds for anomaly detection
    SILENCE_DAYS_THRESHOLD = 14  # Days without contact for inner_circle
    SENTIMENT_SHIFT_THRESHOLD = 0.5  # Change in sentiment mean
    ENGAGEMENT_DROP_THRESHOLD = 0.5  # 50% drop in frequency
    LATENCY_MULTIPLIER = 3.0  # Z-score for latency anomaly

    async def detect(self, person_id: str, baseline: "PersonBaseline", recent_signals: List["Signal"]) -> List[Anomaly]:
        """Detect anomalies for a person."""
        anomalies = []

        # 1. Sudden silence
        silence = self._detect_silence(person_id, baseline, recent_signals)
        if silence:
            anomalies.append(silence)

        # 2. Sentiment shift
        sentiment = self._detect_sentiment_shift(person_id, baseline, recent_signals)
        if sentiment:
            anomalies.append(sentiment)

        # 3. Engagement drop
        engagement = self._detect_engagement_drop(person_id, baseline, recent_signals)
        if engagement:
            anomalies.append(engagement)

        # 4. Abnormal latency
        latency = self._detect_abnormal_latency(person_id, baseline, recent_signals)
        if latency:
            anomalies.append(latency)

        # 5. Colony initiative ignored
        colony_ignored = self._detect_colony_initiative_ignored(person_id, recent_signals)
        if colony_ignored:
            anomalies.append(colony_ignored)

        # 6. Colony effectiveness decline
        effectiveness_decline = self._detect_colony_effectiveness_decline(person_id, recent_signals)
        if effectiveness_decline:
            anomalies.append(effectiveness_decline)

        return anomalies

    def _detect_silence(self, person_id: str, baseline: "PersonBaseline", signals: List["Signal"]) -> Optional[Anomaly]:
        """Detect sudden silence from expected pattern."""
        if not signals:
            # No recent signals at all
            return Anomaly(
                anomaly_type=AnomalyType.SUDDEN_SILENCE,
                person_id=person_id,
                severity=1.0,
                description="No recent communication",
                detected_at=datetime.now(),
                evidence={"days_silent": 999},
            )

        last_signal = max(s.timestamp for s in signals)
        days_since = (datetime.now() - last_signal).days

        # Scale threshold by expected frequency
        expected_gap = 7.0 / max(baseline.messages_per_day, 0.1)
        threshold = min(days_since, self.SILENCE_DAYS_THRESHOLD)

        if days_since > expected_gap * 2 and days_since > 7:
            severity = min(days_since / 30.0, 1.0)
            return Anomaly(
                anomaly_type=AnomalyType.SUDDEN_SILENCE,
                person_id=person_id,
                severity=severity,
                description=f"No contact for {days_since} days (expected ~{expected_gap:.0f} days)",
                detected_at=datetime.now(),
                evidence={"days_silent": days_since, "expected_gap": expected_gap},
            )

        return None

    def _detect_sentiment_shift(self, person_id: str, baseline: "PersonBaseline", signals: List["Signal"]) -> Optional[Anomaly]:
        """Detect significant sentiment shift."""
        sentiment_signals = [s for s in signals if s.signal_type == "sentiment"]
        if not sentiment_signals or baseline.sample_count < 10:
            return None

        recent_mean = sum(s.normalized_value for s in sentiment_signals) / len(sentiment_signals)
        shift = abs(recent_mean - baseline.sentiment_mean)

        if shift > self.SENTIMENT_SHIFT_THRESHOLD:
            direction = "more positive" if recent_mean > baseline.sentiment_mean else "more negative"
            return Anomaly(
                anomaly_type=AnomalyType.SENTIMENT_SHIFT,
                person_id=person_id,
                severity=min(shift / 1.0, 1.0),
                description=f"Sentiment shifted {direction} (Δ={shift:.2f})",
                detected_at=datetime.now(),
                evidence={"shift": shift, "recent_mean": recent_mean, "baseline_mean": baseline.sentiment_mean},
            )

        return None

    def _detect_engagement_drop(self, person_id: str, baseline: "PersonBaseline", signals: List["Signal"]) -> Optional[Anomaly]:
        """Detect sudden drop in engagement."""
        if baseline.sample_count < 10:
            return None

        # Count messages in last 7 days
        week_ago = datetime.now() - timedelta(days=7)
        recent_count = len([s for s in signals if s.timestamp > week_ago and s.signal_type == "message_length"])

        expected = baseline.messages_per_day * 7
        if expected > 0:
            drop_ratio = 1.0 - (recent_count / expected)

            if drop_ratio > self.ENGAGEMENT_DROP_THRESHOLD:
                return Anomaly(
                    anomaly_type=AnomalyType.ENGAGEMENT_DROP,
                    person_id=person_id,
                    severity=drop_ratio,
                    description=f"Engagement dropped {drop_ratio*100:.0f}%",
                    detected_at=datetime.now(),
                    evidence={"recent_count": recent_count, "expected": expected, "drop_ratio": drop_ratio},
                )

        return None

    def _detect_colony_initiative_ignored(
        self,
        person_id: str,
        signals: List["Signal"],
        ignored_threshold: int = 3,
    ) -> Optional[Anomaly]:
        """Detect when Colony's recent initiatives have all been ignored.

        If Colony has sent N+ initiatives with no contact response, that is a
        significant relational signal. This is a Colony-layer anomaly.
        """
        initiative_signals = [
            s for s in signals
            if getattr(s, "signal_type", "") in ("colony_initiative",)
        ]
        response_signals = [
            s for s in signals
            if getattr(s, "signal_type", "") == "contact_response_to_colony"
        ]

        if len(initiative_signals) < ignored_threshold:
            return None

        # Check if the most recent initiatives have responses
        if initiative_signals:
            last_initiative_ts = max(
                getattr(s, "timestamp", datetime.min) for s in initiative_signals
            )
            responses_after = [
                s for s in response_signals
                if getattr(s, "timestamp", datetime.min) >= last_initiative_ts
            ]
            if not responses_after:
                # Count consecutive ignored initiatives
                severity = min(len(initiative_signals) / 5.0, 1.0)
                return Anomaly(
                    anomaly_type=AnomalyType.COLONY_INITIATIVE_IGNORED,
                    person_id=person_id,
                    severity=severity,
                    description=(
                        f"Colony's last {len(initiative_signals)} initiative(s) "
                        f"received no response"
                    ),
                    detected_at=datetime.now(),
                    evidence={
                        "initiative_count": len(initiative_signals),
                        "response_count": len(responses_after),
                    },
                    colony_layer_context="colony",
                )
        return None

    def _detect_colony_effectiveness_decline(
        self,
        person_id: str,
        signals: List["Signal"],
        min_samples: int = 5,
        decline_threshold: float = 0.4,
    ) -> Optional[Anomaly]:
        """Detect when Colony's effectiveness scores are declining.

        Colony tracks the outcome of its actions. A sustained decline in
        effectiveness is a Colony-layer anomaly.
        """
        effectiveness_signals = [
            s for s in signals
            if getattr(s, "signal_type", "") == "colony_effectiveness"
        ]

        if len(effectiveness_signals) < min_samples:
            return None

        sorted_signals = sorted(
            effectiveness_signals,
            key=lambda s: getattr(s, "timestamp", datetime.min),
        )

        values = [getattr(s, "normalized_value", getattr(s, "value", 0.5)) for s in sorted_signals]
        mid = len(values) // 2
        early_avg = sum(values[:mid]) / mid if mid > 0 else 0.5
        late_avg = sum(values[mid:]) / (len(values) - mid)

        decline = early_avg - late_avg
        if decline > decline_threshold:
            return Anomaly(
                anomaly_type=AnomalyType.COLONY_EFFECTIVENESS_DECLINE,
                person_id=person_id,
                severity=min(decline, 1.0),
                description=(
                    f"Colony effectiveness declining (Δ={decline:.2f}): "
                    f"early avg {early_avg:.2f} → recent avg {late_avg:.2f}"
                ),
                detected_at=datetime.now(),
                evidence={
                    "early_avg": early_avg,
                    "late_avg": late_avg,
                    "decline": decline,
                    "sample_count": len(effectiveness_signals),
                },
                colony_layer_context="colony",
            )
        return None

    def _detect_abnormal_latency(self, person_id: str, baseline: "PersonBaseline", signals: List["Signal"]) -> Optional[Anomaly]:
        """Detect abnormal response latency."""
        latency_signals = [s for s in signals if s.signal_type == "response_latency"]
        if not latency_signals or baseline.latency_std == 0:
            return None

        recent_latencies = [s.raw_value for s in latency_signals[-5:]]
        if not recent_latencies:
            return None

        recent_mean = sum(recent_latencies) / len(recent_latencies)
        z_score = abs(recent_mean - baseline.latency_mean) / baseline.latency_std

        if z_score > self.LATENCY_MULTIPLIER:
            direction = "slower" if recent_mean > baseline.latency_mean else "faster"
            return Anomaly(
                anomaly_type=AnomalyType.ABNORMAL_LATENCY,
                person_id=person_id,
                severity=min(z_score / 5.0, 1.0),
                description=f"Response time {direction} than usual (z={z_score:.1f})",
                detected_at=datetime.now(),
                evidence={"z_score": z_score, "recent_mean": recent_mean, "baseline_mean": baseline.latency_mean},
            )

        return None
