"""Anomaly Detector — detect unusual patterns in behavior and data.

Detects:
    - Unusual communication patterns
    - Anomalous health metrics
    - Abnormal relationship signals
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import logging

logger = logging.getLogger(__name__)

# Minimum fractional deviation from baseline to generate an anomaly
_ANOMALY_DEVIATION_THRESHOLD = 0.3


class AnomalyType(str, Enum):
    """Categories of detectable anomalies."""

    COMMUNICATION = "communication"
    HEALTH = "health"
    BEHAVIOR = "behavior"
    RELATIONSHIP = "relationship"
    # Phase E: conversation-sourced anomaly types
    CONVERSATION_TIMING = "conversation_timing"    # unusual message time vs baseline
    TONE_SHIFT = "tone_shift"                      # sentiment significantly changed
    SUDDEN_SILENCE = "sudden_silence"              # person who messages daily gone quiet
    TOPIC_ANOMALY = "topic_anomaly"                # far outside normal topic pattern


@dataclass
class Anomaly:
    """A detected anomaly.

    Attributes:
        id: Unique anomaly identifier
        type: Category of anomaly
        description: Human-readable description
        severity: How severe the anomaly is (0-1, higher = more severe)
        entity_id: Optional related entity (person, device, etc.)
        baseline_value: Expected/normal value
        observed_value: Actual observed value
        detected_at: When the anomaly was detected
        context: Additional anomaly-specific metadata
    """

    id: str
    type: AnomalyType
    description: str
    severity: float
    entity_id: Optional[str] = None
    baseline_value: Any = None
    observed_value: Any = None
    detected_at: datetime = field(default_factory=datetime.now)
    context: Dict[str, Any] = field(default_factory=dict)


class AnomalyDetector:
    """Detect anomalies across domains.

    Compares observed values against learned baselines to surface
    unusual patterns in communication, health, and behavior.

    Observations are added via ``add_observation()``.  Each observation
    is tagged with an ``AnomalyType`` so detection methods can filter
    domain-specific data.  Severity is computed as the fractional
    deviation from the baseline, capped at 1.0.

    Args:
        graph_client: Colony graph client for baseline data
        event_bus: Colony event bus for anomaly alert events
    """

    def __init__(self, graph_client: Any, event_bus: Any) -> None:
        self.graph = graph_client
        self.events = event_bus
        self._baselines: Dict[str, Any] = {}
        # key → list of (observed_value, anomaly_type) tuples
        self._observations: Dict[str, List[Tuple[float, AnomalyType]]] = {}

    # ------------------------------------------------------------------
    # Observation ingestion
    # ------------------------------------------------------------------

    async def add_observation(
        self,
        entity_id: str,
        metric: str,
        value: float,
        anomaly_type: AnomalyType = AnomalyType.BEHAVIOR,
    ) -> None:
        """Record an observed metric value for a given entity.

        Does not immediately raise an alert — call ``detect()`` to check
        all recorded observations against their baselines.

        Args:
            entity_id: Entity the metric belongs to
            metric: Metric name (e.g., "message_frequency")
            value: Observed value
            anomaly_type: Domain this observation belongs to
        """
        key = f"{entity_id}.{metric}"
        if key not in self._observations:
            self._observations[key] = []
        self._observations[key].append((value, anomaly_type))
        logger.debug("Recorded observation: %s = %s (%s)", key, value, anomaly_type.value)

    # ------------------------------------------------------------------
    # Baseline management
    # ------------------------------------------------------------------

    async def update_baseline(
        self,
        entity_id: str,
        metric: str,
        value: Any,
    ) -> None:
        """Update the baseline for a metric.

        Args:
            entity_id: Entity the metric belongs to
            metric: Metric name
            value: New baseline value
        """
        key = f"{entity_id}.{metric}"
        self._baselines[key] = value
        logger.debug("Updated baseline: %s = %s", key, value)

    async def get_baseline(self, entity_id: str, metric: str) -> Optional[Any]:
        """Get the current baseline for a metric.

        Args:
            entity_id: Entity the metric belongs to
            metric: Metric name

        Returns:
            Baseline value or None if not set
        """
        key = f"{entity_id}.{metric}"
        return self._baselines.get(key)

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Phase E: Conversation-pattern observation helpers
    # ------------------------------------------------------------------

    async def observe_message_timing(
        self,
        entity_id: str,
        hour_of_day: int,
        baseline_hours: Optional[list] = None,
    ) -> None:
        """Record a message hour observation and update baseline if provided.

        Args:
            entity_id: Person who sent the message
            hour_of_day: UTC hour (0-23) the message was sent
            baseline_hours: If provided, the list of typical hours for this person
        """
        key = f"{entity_id}.message_hour"
        if baseline_hours and len(baseline_hours) > 0:
            # Use midpoint of typical hours as baseline
            avg_hour = sum(baseline_hours) / len(baseline_hours)
            await self.update_baseline(entity_id, "message_hour", avg_hour)
        await self.add_observation(entity_id, "message_hour", float(hour_of_day),
                                   anomaly_type=AnomalyType.CONVERSATION_TIMING)
        logger.debug("Timing observation for %s: hour=%d", entity_id, hour_of_day)

    async def observe_sentiment(
        self,
        entity_id: str,
        sentiment_score: float,
        baseline_sentiment: Optional[float] = None,
    ) -> None:
        """Record a message sentiment observation.

        Args:
            entity_id: Person whose sentiment was measured
            sentiment_score: Current sentiment (-1.0 to 1.0)
            baseline_sentiment: Expected sentiment baseline for this person
        """
        if baseline_sentiment is not None:
            await self.update_baseline(entity_id, "sentiment", baseline_sentiment)
        await self.add_observation(entity_id, "sentiment", sentiment_score,
                                   anomaly_type=AnomalyType.TONE_SHIFT)

    async def observe_silence_days(
        self,
        entity_id: str,
        days_since_last_message: float,
        baseline_days: float = 1.0,
    ) -> None:
        """Record a silence duration observation.

        Args:
            entity_id: Person who has been silent
            days_since_last_message: How many days since their last message
            baseline_days: Expected messaging cadence in days
        """
        await self.update_baseline(entity_id, "message_cadence_days", baseline_days)
        await self.add_observation(entity_id, "message_cadence_days", days_since_last_message,
                                   anomaly_type=AnomalyType.SUDDEN_SILENCE)

    async def detect(
        self,
        domain: Optional[AnomalyType] = None,
        threshold: float = 0.7,
    ) -> List[Anomaly]:
        """Detect anomalies, optionally filtered by domain.

        Runs detection across all domains (or a specific one) and
        returns anomalies above the severity threshold.

        Args:
            domain: If provided, only check this domain
            threshold: Minimum severity to include (0-1)

        Returns:
            List of anomalies at or above the threshold
        """
        anomalies: List[Anomaly] = []

        if domain in (None, AnomalyType.COMMUNICATION):
            anomalies.extend(await self._detect_communication_anomalies())

        if domain in (None, AnomalyType.HEALTH):
            anomalies.extend(await self._detect_health_anomalies())

        if domain in (None, AnomalyType.BEHAVIOR):
            anomalies.extend(await self._detect_behavior_anomalies())

        if domain in (None, AnomalyType.RELATIONSHIP):
            anomalies.extend(await self._detect_relationship_anomalies())

        # Phase E: conversation-sourced anomaly types
        if domain in (None, AnomalyType.CONVERSATION_TIMING):
            anomalies.extend(await self._detect_conversation_timing_anomalies())

        if domain in (None, AnomalyType.TONE_SHIFT):
            anomalies.extend(await self._detect_tone_shift_anomalies())

        if domain in (None, AnomalyType.SUDDEN_SILENCE):
            anomalies.extend(await self._detect_sudden_silence_anomalies())

        filtered = [a for a in anomalies if a.severity >= threshold]

        logger.debug(
            "Anomaly detection complete: %d found, %d above threshold %.2f",
            len(anomalies),
            len(filtered),
            threshold,
        )
        return filtered

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_anomaly(
        self,
        entity_id: str,
        metric: str,
        observed: float,
        baseline: float,
        anomaly_type: AnomalyType,
    ) -> Optional[Anomaly]:
        """Compute an Anomaly if the observed value deviates significantly.

        Severity = |observed - baseline| / max(|baseline|, 1.0), capped at 1.0.
        Returns None if severity < _ANOMALY_DEVIATION_THRESHOLD.
        """
        if baseline == 0:
            severity = min(1.0, abs(observed) / max(abs(observed), 1.0)) if observed != 0 else 0.0
        else:
            severity = min(1.0, abs(observed - baseline) / abs(baseline))

        if severity < _ANOMALY_DEVIATION_THRESHOLD:
            return None

        direction = "above" if observed > baseline else "below"
        return Anomaly(
            id=f"anomaly-{entity_id}-{metric}-{datetime.now().isoformat()}",
            type=anomaly_type,
            description=(
                f"{metric} for {entity_id} is {direction} baseline: "
                f"observed={observed}, baseline={baseline}"
            ),
            severity=severity,
            entity_id=entity_id,
            baseline_value=baseline,
            observed_value=observed,
        )

    def _detect_for_type(self, target_type: AnomalyType) -> List[Anomaly]:
        """Detect anomalies for observations of a given type."""
        anomalies: List[Anomaly] = []
        for key, observations in self._observations.items():
            for obs_value, obs_type in observations:
                if obs_type != target_type:
                    continue
                baseline = self._baselines.get(key)
                if baseline is None:
                    continue
                # key is "entity_id.metric" — split on last dot
                dot_idx = key.rfind(".")
                if dot_idx < 0:
                    continue
                entity_id = key[:dot_idx]
                metric = key[dot_idx + 1:]
                anomaly = self._compute_anomaly(entity_id, metric, obs_value, baseline, target_type)
                if anomaly:
                    anomalies.append(anomaly)
        return anomalies

    async def _detect_communication_anomalies(self) -> List[Anomaly]:
        """Detect unusual communication patterns.

        Compares message frequency, response times, and contact-pattern
        observations against their stored baselines.
        """
        return self._detect_for_type(AnomalyType.COMMUNICATION)

    async def _detect_health_anomalies(self) -> List[Anomaly]:
        """Detect unusual health metrics (e.g., Oura/Whoop data)."""
        return self._detect_for_type(AnomalyType.HEALTH)

    async def _detect_behavior_anomalies(self) -> List[Anomaly]:
        """Detect unusual behavior patterns (activity, schedule adherence)."""
        return self._detect_for_type(AnomalyType.BEHAVIOR)

    async def _detect_relationship_anomalies(self) -> List[Anomaly]:
        """Detect unusual relationship signals (score trajectories)."""
        return self._detect_for_type(AnomalyType.RELATIONSHIP)

    async def _detect_conversation_timing_anomalies(self) -> List[Anomaly]:
        """Detect messages arriving at unusual times relative to baseline hours."""
        anomalies = self._detect_for_type(AnomalyType.CONVERSATION_TIMING)
        # Enrich descriptions for timing anomalies
        for a in anomalies:
            if "message_hour" in a.description:
                obs_hour = int(a.observed_value) if a.observed_value is not None else "?"
                baseline_hour = int(a.baseline_value) if a.baseline_value is not None else "?"
                a.description = (
                    f"Unusual message timing for {a.entity_id}: received at hour {obs_hour}, "
                    f"baseline is hour {baseline_hour}"
                )
        return anomalies

    async def _detect_tone_shift_anomalies(self) -> List[Anomaly]:
        """Detect significant sentiment shifts from baseline."""
        anomalies = self._detect_for_type(AnomalyType.TONE_SHIFT)
        for a in anomalies:
            if "sentiment" in a.description:
                direction = "more negative" if (a.observed_value or 0) < (a.baseline_value or 0) else "more positive"
                a.description = (
                    f"Tone shift for {a.entity_id}: sentiment is {direction} than baseline "
                    f"(observed={a.observed_value:.2f}, baseline={a.baseline_value:.2f})"
                )
        return anomalies

    async def _detect_sudden_silence_anomalies(self) -> List[Anomaly]:
        """Detect unusual silence from a person who normally messages regularly."""
        anomalies = self._detect_for_type(AnomalyType.SUDDEN_SILENCE)
        for a in anomalies:
            if "message_cadence" in a.description:
                obs_days = f"{a.observed_value:.1f}" if a.observed_value is not None else "?"
                baseline_days = f"{a.baseline_value:.1f}" if a.baseline_value is not None else "?"
                a.description = (
                    f"Sudden silence: {a.entity_id} hasn't messaged in {obs_days} days "
                    f"(baseline: every {baseline_days} day(s))"
                )
        return anomalies

    # ------------------------------------------------------------------
    # Convenience API for the autonomy loop
    # ------------------------------------------------------------------

    def get_recent(
        self,
        min_severity: float = 0.6,
        limit: int = 20,
    ) -> List[Anomaly]:
        """Return recently detected anomalies above min_severity.

        Synchronous convenience wrapper over the in-memory observation
        store. Does not re-run full async detection — uses cached results
        from the last ``detect()`` call if available, otherwise runs
        synchronous detection on the current observation set.

        Args:
            min_severity: Minimum severity threshold (0–1)
            limit: Maximum number of anomalies to return

        Returns:
            List of Anomaly objects at or above min_severity, newest first.
        """
        anomalies: List[Anomaly] = []
        for anom_type in AnomalyType:
            anomalies.extend(self._detect_for_type(anom_type))

        filtered = [a for a in anomalies if a.severity >= min_severity]
        # Sort by severity descending, then by detection time descending
        filtered.sort(key=lambda a: (a.severity, a.detected_at.timestamp()), reverse=True)
        return filtered[:limit]
