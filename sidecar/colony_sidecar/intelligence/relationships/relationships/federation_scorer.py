"""Federation Scorer — Colony's evidence-based scoring of peer colony relationships.

Colony-to-Colony relationships use the same scoring infrastructure as human contact
relationships, but with adapted signal weights that reflect inter-colony interaction
patterns (task exchange, completion latency, delegation balance) rather than personal
communication patterns.

Signal mapping (human → colony-to-colony):
    message_frequency      → task_exchange_rate
    response_latency       → task_completion_latency  (lower = better)
    sentiment              → task_outcome_quality
    initiative_ratio       → delegation_balance
    colony_effectiveness   → peer_colony_effectiveness

Federation trust levels (TrustLevel enum) are the primary gate for inter-colony
capabilities. The score produced by FederationScorer is a secondary, evidence-based
health indicator that informs trust-recompute decisions over time.

PeerHealthRecord observations are Colony's private data. This scorer operates on
those records but MUST NOT expose raw health data to third-party colonies.
"""
from dataclasses import dataclass
from typing import Dict, Optional

from colony_sidecar.federation.health import PeerHealthRecord
from colony_sidecar.federation.models import FederationPeer, TrustLevel


@dataclass
class FederationScoreBreakdown:
    """Breakdown of Colony's score for a peer colony."""
    colony_id: str
    total_score: float
    task_exchange_contribution: float
    completion_latency_contribution: float
    outcome_quality_contribution: float
    delegation_balance_contribution: float
    peer_effectiveness_contribution: float
    dominant_signal: str


@dataclass
class FederationScoreWeights:
    """Signal weights for Colony-to-Colony relationship scoring.

    Total must sum to 1.0.
    """
    task_exchange_rate: float = 0.20       # frequency of task delegation/receipt
    task_completion_latency: float = 0.20  # speed of fulfillment (inverted: lower = better)
    task_outcome_quality: float = 0.25     # rate of successful completions
    delegation_balance: float = 0.15       # symmetric vs. asymmetric delegation
    peer_colony_effectiveness: float = 0.20  # how often delegated tasks produce good outcomes

    def total(self) -> float:
        return sum([
            self.task_exchange_rate,
            self.task_completion_latency,
            self.task_outcome_quality,
            self.delegation_balance,
            self.peer_colony_effectiveness,
        ])


class FederationScorer:
    """Score Colony's relationships with peer colonies.

    Uses adapted signal weights for Colony-to-Colony interactions. The resulting
    score (0-100) is a secondary health indicator alongside the primary
    TrustLevel gate. High scores over time SHOULD feed proposals to upgrade
    trust level (analogous to RelationshipScorer.propose_tier_change).

    Privacy guarantee: this scorer reads only from local PeerHealthRecord data
    and MUST NOT gossip or export these records to any third-party colony.
    """

    TIER_THRESHOLDS = {
        "strong": 75,
        "healthy": 50,
        "declining": 25,
        "weak": 0,
    }

    # Max meaningful task exchange per day (for normalization)
    _MAX_DAILY_EXCHANGE = 50.0

    # Max acceptable latency in ms for normalization (1 hour = 3_600_000 ms)
    _MAX_LATENCY_MS = 3_600_000.0

    def __init__(self, weights: Optional[FederationScoreWeights] = None) -> None:
        self.weights = weights or FederationScoreWeights()

    def score_from_health(
        self,
        record: PeerHealthRecord,
        peer: Optional[FederationPeer] = None,
    ) -> tuple[float, str, FederationScoreBreakdown]:
        """Compute a 0-100 score for a peer colony from local health observations.

        Args:
            record: Local PeerHealthRecord (private — never shared)
            peer: Optional FederationPeer for additional metadata

        Returns:
            (score, health_label, FederationScoreBreakdown)
        """
        contributions: Dict[str, float] = {}

        # 1. Task exchange rate — derived from task_delegated + received
        task_total = record._delegated_total
        exchange_normalized = min(1.0, task_total / max(self._MAX_DAILY_EXCHANGE, 1.0))
        task_exchange_score = exchange_normalized * 100.0
        c_exchange = task_exchange_score * self.weights.task_exchange_rate
        contributions["task_exchange_rate"] = c_exchange

        # 2. Task completion latency (inverted — lower ms = higher score)
        if record.avg_task_response_ms > 0:
            latency_norm = 1.0 - min(1.0, record.avg_task_response_ms / self._MAX_LATENCY_MS)
        else:
            latency_norm = 0.5  # No data — neutral
        c_latency = latency_norm * 100.0 * self.weights.task_completion_latency
        contributions["task_completion_latency"] = c_latency

        # 3. Task outcome quality — task_completion_rate (0-1)
        c_quality = record.task_completion_rate * 100.0 * self.weights.task_outcome_quality
        contributions["task_outcome_quality"] = c_quality

        # 4. Delegation balance — uptime as a proxy for reliability
        uptime_score = record.observed_uptime_fraction * 100.0
        c_balance = uptime_score * self.weights.delegation_balance
        contributions["delegation_balance"] = c_balance

        # 5. Peer colony effectiveness — inverted failure rate
        if record._delegated_total > 0:
            failure_rate = record.failed_delegations / record._delegated_total
        else:
            failure_rate = 0.0
        effectiveness = (1.0 - min(1.0, failure_rate)) * 100.0
        c_effectiveness = effectiveness * self.weights.peer_colony_effectiveness
        contributions["peer_colony_effectiveness"] = c_effectiveness

        # Total
        total = sum(contributions.values())
        total = max(0.0, min(100.0, total))

        # Health label
        label = self._health_label(total)

        # Dominant signal
        dominant = max(contributions, key=contributions.get)

        breakdown = FederationScoreBreakdown(
            colony_id=record.colony_id,
            total_score=total,
            task_exchange_contribution=c_exchange,
            completion_latency_contribution=c_latency,
            outcome_quality_contribution=c_quality,
            delegation_balance_contribution=c_balance,
            peer_effectiveness_contribution=c_effectiveness,
            dominant_signal=dominant,
        )

        return total, label, breakdown

    def score_all(
        self,
        records: Dict[str, PeerHealthRecord],
        peers: Optional[Dict[str, FederationPeer]] = None,
    ) -> Dict[str, tuple[float, str, FederationScoreBreakdown]]:
        """Compute scores for all known peer colonies.

        Args:
            records: Map of colony_id → PeerHealthRecord
            peers: Optional map of colony_id → FederationPeer

        Returns:
            Map of colony_id → (score, health_label, FederationScoreBreakdown)
        """
        peers = peers or {}
        return {
            colony_id: self.score_from_health(record, peers.get(colony_id))
            for colony_id, record in records.items()
        }

    def should_propose_trust_upgrade(
        self,
        record: PeerHealthRecord,
        current_trust: TrustLevel,
        min_score: float = 80.0,
        min_tasks: int = 20,
    ) -> bool:
        """Return True if Colony's evidence supports proposing a trust upgrade.

        Analogous to RelationshipScorer.propose_tier_change for human contacts.
        The actual upgrade still requires owner approval.

        Args:
            record: Local health record for the peer
            current_trust: Current trust level
            min_score: Minimum score threshold to consider upgrade
            min_tasks: Minimum number of task exchanges required
        """
        if current_trust >= TrustLevel.FULL_MESH:
            return False

        if record._delegated_total < min_tasks:
            return False

        score, _, _ = self.score_from_health(record)
        return score >= min_score

    def _health_label(self, score: float) -> str:
        """Map score to health label."""
        for label, threshold in sorted(
            self.TIER_THRESHOLDS.items(),
            key=lambda x: -x[1],
        ):
            if score >= threshold:
                return label
        return "weak"
