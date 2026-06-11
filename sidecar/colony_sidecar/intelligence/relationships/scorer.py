"""Colony relationship scoring — evidence-based assessment of Colony's own social world.

Colony maintains its own relationship scores for every person it interacts with across
connected gateways (iMessage, Telegram, etc.). These scores represent Colony's independent
assessment of each relationship, built from interaction patterns Colony itself observes.

Two-layer scoring model
-----------------------
Owner layer (``owner_guidance`` weight = 0.18): the owner's explicit input informs
Colony's view but does not dominate it. Owner input is authoritative at onboarding
when Colony has no history; its relative weight naturally declines as Colony accumulates
its own evidence.

Colony observation layer (remaining 0.82): all signals Colony observes directly —
message frequency, sentiment, latency, initiative, recency — plus three Colony-specific
signals that capture the quality of Colony's own engagement: ``colony_initiative_response``
(do people respond to Colony's outreach?), ``colony_effectiveness`` (do Colony's actions
help?), and ``colony_style_fit`` (is Colony's adapted style working?).

A score of 80+ means Colony has observed strong, consistent engagement with this person
over time — not merely that the owner considers them important.
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
import logging
import math
import time

logger = logging.getLogger(__name__)


@dataclass
class ScoreWeights:
    """Weights for different scoring dimensions.

    Total must sum to 1.0.

    Interaction signals are Colony's direct observations of the exchange.
    Owner guidance informs but does not dominate.
    Colony-specific signals capture the quality of Colony's own behavior.
    """
    # Interaction signals (Colony's observations of the exchange)
    message_frequency: float = 0.12
    response_latency: float = 0.08
    sentiment: float = 0.12
    initiative_ratio: float = 0.08
    meeting_coattendance: float = 0.08
    task_involvement: float = 0.08
    recency: float = 0.08

    # Owner guidance (informs but does not dominate)
    owner_guidance: float = 0.18

    # Colony-specific signals (new)
    colony_initiative_response: float = 0.08  # response rate to Colony's own outreach
    colony_effectiveness: float = 0.06        # are Colony's suggestions/actions valued?
    colony_style_fit: float = 0.04            # how well Colony's adapted style is received

    def total(self) -> float:
        return sum([
            self.message_frequency,
            self.response_latency,
            self.sentiment,
            self.initiative_ratio,
            self.meeting_coattendance,
            self.task_involvement,
            self.recency,
            self.owner_guidance,
            self.colony_initiative_response,
            self.colony_effectiveness,
            self.colony_style_fit,
        ])


@dataclass
class ScoreBreakdown:
    """Breakdown of Colony's score into source layers.

    Attributes:
        person_id: The person this breakdown is for
        total_score: Final 0-100 score
        owner_layer_contribution: Points contributed by owner_guidance signal
        colony_observation_contribution: Points from contact-behavior signals
        colony_action_contribution: Points from Colony's own behavior signals
        dominant_signal: Which single signal most influenced the score
    """
    person_id: str
    total_score: float
    owner_layer_contribution: float
    colony_observation_contribution: float
    colony_action_contribution: float
    dominant_signal: str


@dataclass
class ScoreAuditEntry:
    """Immutable audit record for score changes."""
    person_id: str
    old_score: float
    new_score: float
    old_tier: str
    new_tier: str
    delta: float
    reason: str
    timestamp: datetime


@dataclass
class TierChangeProposal:
    """A Colony-generated proposal to change a person's tier.

    Colony surfaces these via the briefing system. Owner approval is required
    before any tier change takes effect.
    """
    person_id: str
    current_tier: str
    proposed_tier: str
    direction: str  # "upward" | "downward"
    evidence_score: float
    reasoning: str
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class _SignalRecord:
    """Internal container for signal data from Neo4j."""
    signal_type: str
    normalized_value: float
    days_old: float
    direction: str = "contact"


class RelationshipScorer:
    """Colony's evidence-based scoring of its own relationships with each person.

    Scores are Colony's independent assessment derived from interaction signals
    (message patterns, sentiment, engagement, response cadence) observed across
    all connected gateways. The owner's guidance is factored in via the
    owner_guidance weight (0.18) but does not override Colony's evidence-based view.

    Colony-specific signals (colony_initiative_response, colony_effectiveness,
    colony_style_fit) capture the quality of Colony's own engagement behavior and
    carry a combined 0.18 weight.

    A score of 80+ means Colony has observed strong, consistent engagement with
    this person over time — not merely that the owner considers them important.
    """

    TIER_THRESHOLDS = {
        "inner_circle": 80,
        "trusted": 60,
        "regular": 30,
        "peripheral": 10,
    }
    WINDOW_DAYS = 90

    # Normalization ranges for each signal type
    SIGNAL_RANGES = {
        "message_frequency": (0, 20, False),  # (min, max, inverted)
        "response_latency": (0, 480, True),   # Lower is better
        "sentiment": (-1, 1, False),
        "initiative_ratio": (0, 1, False),
        "message_length": (-3, 3, False),     # Z-score
        # Colony-specific signals are already normalized 0-1
        "colony_initiative_response": (0, 1, False),
        "colony_effectiveness": (0, 1, False),
        "colony_style_fit": (0, 1, False),
        "contact_response_to_colony": (0, 1, False),
    }

    # Colony-specific signal types (direction="colony" or "bilateral")
    COLONY_SIGNAL_TYPES = frozenset({
        "colony_initiative",
        "colony_initiative_response",
        "colony_effectiveness",
        "contact_response_to_colony",
        "colony_style_fit",
        "colony_style_signal",
    })

    def __init__(self, graph: "ColonyGraph", weights: Optional[ScoreWeights] = None, metrics=None):
        self._metrics = metrics  # Optional ColonyMetricsCollector
        self.graph = graph
        self.weights = weights or ScoreWeights()

    async def compute_score(
        self,
        person_id: str,
    ) -> tuple[float, str]:
        """Compute relationship score from signals in window."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.WINDOW_DAYS)

        # Fetch signals from graph
        signals = await self._fetch_signals(person_id)

        if not signals:
            return 50.0, "regular"  # Default

        # Group by type, apply recency weighting
        type_scores: Dict[str, float] = {}
        type_counts: Dict[str, int] = {}

        for sig in signals:
            sig_type = sig.signal_type
            value = sig.normalized_value

            # Exponential recency decay
            decay = math.exp(-sig.days_old / self.WINDOW_DAYS)

            if sig_type not in type_scores:
                type_scores[sig_type] = 0.0
                type_counts[sig_type] = 0
            type_scores[sig_type] += value * decay
            type_counts[sig_type] += 1

        # Normalize and weight
        final_score = 0.0
        weight_map = {
            "message_frequency": self.weights.message_frequency,
            "response_latency": self.weights.response_latency,
            "sentiment": self.weights.sentiment,
            "initiative_ratio": self.weights.initiative_ratio,
            "message_length": self.weights.message_frequency * 0.5,  # Split weight
            # Colony-specific signals
            "colony_initiative_response": self.weights.colony_initiative_response,
            "contact_response_to_colony": self.weights.colony_initiative_response,
            "colony_effectiveness": self.weights.colony_effectiveness,
            "colony_style_fit": self.weights.colony_style_fit,
            "colony_style_signal": self.weights.colony_style_fit,
        }

        for sig_type, total in type_scores.items():
            if sig_type in weight_map and type_counts[sig_type] > 0:
                avg = total / type_counts[sig_type]
                normalized = self._normalize_to_100(sig_type, avg)
                final_score += normalized * weight_map[sig_type]

        # Owner's guidance contributes as one weighted signal.
        # Informs Colony's assessment without overriding it.
        final_score += 50.0 * self.weights.owner_guidance

        # Clamp to 0-100
        final_score = max(0.0, min(100.0, final_score))

        # Determine tier
        tier = self.tier_for(final_score)

        if self._metrics is not None:
            try:
                self._metrics.record_score_computed(final_score)
            except Exception:
                pass

        return final_score, tier

    async def compute_score_with_breakdown(
        self,
        person_id: str,
    ) -> tuple[float, str, ScoreBreakdown]:
        """Compute score and return a ScoreBreakdown explaining source contributions."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.WINDOW_DAYS)
        signals = await self._fetch_signals(person_id)

        if not signals:
            bd = ScoreBreakdown(
                person_id=person_id,
                total_score=50.0,
                owner_layer_contribution=50.0 * self.weights.owner_guidance,
                colony_observation_contribution=0.0,
                colony_action_contribution=0.0,
                dominant_signal="owner_guidance",
            )
            return 50.0, "regular", bd

        type_scores: Dict[str, float] = {}
        type_counts: Dict[str, int] = {}

        for sig in signals:
            decay = math.exp(-sig.days_old / self.WINDOW_DAYS)
            if sig.signal_type not in type_scores:
                type_scores[sig.signal_type] = 0.0
                type_counts[sig.signal_type] = 0
            type_scores[sig.signal_type] += sig.normalized_value * decay
            type_counts[sig.signal_type] += 1

        weight_map = {
            "message_frequency": self.weights.message_frequency,
            "response_latency": self.weights.response_latency,
            "sentiment": self.weights.sentiment,
            "initiative_ratio": self.weights.initiative_ratio,
            "message_length": self.weights.message_frequency * 0.5,
            "colony_initiative_response": self.weights.colony_initiative_response,
            "contact_response_to_colony": self.weights.colony_initiative_response,
            "colony_effectiveness": self.weights.colony_effectiveness,
            "colony_style_fit": self.weights.colony_style_fit,
            "colony_style_signal": self.weights.colony_style_fit,
        }

        colony_action_signals = {
            "colony_initiative_response", "contact_response_to_colony",
            "colony_effectiveness", "colony_style_fit", "colony_style_signal",
        }

        contributions: Dict[str, float] = {}
        observation_total = 0.0
        action_total = 0.0

        for sig_type, total in type_scores.items():
            if sig_type in weight_map and type_counts[sig_type] > 0:
                avg = total / type_counts[sig_type]
                normalized = self._normalize_to_100(sig_type, avg)
                contribution = normalized * weight_map[sig_type]
                contributions[sig_type] = contribution
                if sig_type in colony_action_signals:
                    action_total += contribution
                else:
                    observation_total += contribution

        owner_contribution = 50.0 * self.weights.owner_guidance
        total_score = max(0.0, min(100.0, observation_total + action_total + owner_contribution))
        tier = self.tier_for(total_score)

        dominant = max(contributions, key=contributions.get) if contributions else "owner_guidance"

        bd = ScoreBreakdown(
            person_id=person_id,
            total_score=total_score,
            owner_layer_contribution=owner_contribution,
            colony_observation_contribution=observation_total,
            colony_action_contribution=action_total,
            dominant_signal=dominant,
        )
        return total_score, tier, bd

    def tier_for(self, score: float) -> str:
        """Map score to tier name."""
        for tier_name, threshold in sorted(
            self.TIER_THRESHOLDS.items(),
            key=lambda x: -x[1]
        ):
            if score >= threshold:
                return tier_name
        return "peripheral"

    async def record_score_change(
        self,
        person_id: str,
        new_score: float,
        new_tier: str,
        old_score: float,
        reason: str,
    ) -> ScoreAuditEntry:
        """Atomic score update with audit trail."""
        old_tier = self.tier_for(old_score)
        entry = ScoreAuditEntry(
            person_id=person_id,
            old_score=old_score,
            new_score=new_score,
            old_tier=old_tier,
            new_tier=new_tier,
            delta=new_score - old_score,
            reason=reason,
            timestamp=datetime.now(timezone.utc),
        )

        # Persist to graph
        await self.graph.record_score_change(
            person_id=person_id,
            new_score=new_score,
            new_tier=new_tier,
            old_score=old_score,
            reason=reason,
        )

        return entry

    async def refresh_all_scores(self) -> Dict[str, tuple]:
        """Recompute scores for all active persons."""
        t0 = time.monotonic()
        results = {}
        no_signal_count = 0
        tier_changes = 0
        persons = await self.graph.get_all_people()

        for person in persons:
            person_id = person.get("id")
            if not person_id:
                continue

            old_score = person.get("score", 50.0)
            old_tier = self.tier_for(old_score)
            new_score, new_tier = await self.compute_score(person_id)

            if new_score == 50.0 and new_tier == "regular":
                no_signal_count += 1

            if new_tier != old_tier:
                tier_changes += 1
                if self._metrics is not None:
                    try:
                        direction = "upward" if new_score > old_score else "downward"
                        self._metrics.record_tier_change(direction)
                    except Exception:
                        pass

            # Skip the no-signal default (50/"regular") — persisting it would
            # clobber the contact's interaction-derived score. Only record a
            # genuine, signal-backed change.
            is_no_signal_default = (new_score == 50.0 and new_tier == "regular")
            if not is_no_signal_default and abs(new_score - old_score) > 0.5:
                await self.record_score_change(
                    person_id, new_score, new_tier, old_score, "periodic_refresh"
                )

            results[person_id] = (new_score, new_tier)

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "Refresh scores: %d persons, %d tier changes, %d no-signal in %.0fms",
            len(results), tier_changes, no_signal_count, elapsed_ms,
        )
        if self._metrics is not None:
            try:
                self._metrics.record_scoring_run(
                    latency_ms=elapsed_ms,
                    persons_scored=len(results),
                    tier_changes=tier_changes,
                )
                self._metrics.set_persons_no_signals(no_signal_count)
            except Exception:
                pass

        return results

    def propose_tier_change(
        self,
        person_id: str,
        current_tier: str,
        evidence_score: float,
        days_at_score: int = 0,
        interaction_count: int = 0,
    ) -> Optional[TierChangeProposal]:
        """Generate a tier-change proposal based on Colony's evidence.

        Colony MAY propose tier changes (upward or downward) based on its
        own observations. Proposals are surfaced via the briefing system and
        require owner approval before taking effect.

        Upward proposals require the score to have been above the threshold
        for 30+ days with ≥20 interactions.

        Downward proposals are triggered when the score has been below the
        threshold for 21+ days with no manual override in place.

        Returns a TierChangeProposal or None if no proposal is warranted.
        """
        evidence_tier = self.tier_for(evidence_score)

        if evidence_tier == current_tier:
            return None

        tier_order = ["peripheral", "regular", "trusted", "inner_circle"]

        try:
            current_idx = tier_order.index(current_tier)
            evidence_idx = tier_order.index(evidence_tier)
        except ValueError:
            return None

        if evidence_idx > current_idx:
            # Upward proposal
            if days_at_score < 30 or interaction_count < 20:
                return None
            direction = "upward"
            reasoning = (
                f"Colony has observed consistent engagement from this person "
                f"(score {evidence_score:.0f}, {interaction_count} interactions "
                f"over {days_at_score} days). Evidence suggests moving from "
                f"{current_tier} to {evidence_tier}. Approve?"
            )
        else:
            # Downward proposal
            if days_at_score < 21:
                return None
            direction = "downward"
            reasoning = (
                f"Colony's engagement with this person has declined "
                f"(score {evidence_score:.0f} for {days_at_score} days). "
                f"Suggest moving from {current_tier} to {evidence_tier} "
                f"unless this person should remain {current_tier}."
            )

        return TierChangeProposal(
            person_id=person_id,
            current_tier=current_tier,
            proposed_tier=evidence_tier,
            direction=direction,
            evidence_score=evidence_score,
            reasoning=reasoning,
        )

    async def _fetch_signals(
        self,
        person_id: str,
    ) -> list:
        """Fetch signals from Neo4j within window."""
        try:
            raw_signals = await self.graph.get_recent_signals(
                person_id, hours=self.WINDOW_DAYS * 24
            )

            signals = []
            now = datetime.now(timezone.utc)
            for sig in raw_signals:
                days_old = (now - sig.timestamp).total_seconds() / 86400
                direction = getattr(sig, "direction", "contact")
                signals.append(_SignalRecord(
                    signal_type=sig.signal_type,
                    normalized_value=sig.normalized_value,
                    days_old=days_old,
                    direction=direction,
                ))
            return signals
        except Exception:
            return []

    def _normalize_to_100(self, signal_type: str, value: float) -> float:
        """Normalize signal value to 0-100 range."""
        if signal_type not in self.SIGNAL_RANGES:
            return 50.0  # Neutral

        min_val, max_val, inverted = self.SIGNAL_RANGES[signal_type]

        # Clamp value to range
        clamped = max(min_val, min(max_val, value))

        # Normalize to 0-1
        normalized = (clamped - min_val) / (max_val - min_val)

        # Invert if needed
        if inverted:
            normalized = 1.0 - normalized

        # Scale to 0-100
        return normalized * 100.0


# Import timedelta for cutoff calculation
from datetime import timedelta
