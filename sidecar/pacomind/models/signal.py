"""Behavioral signal models.

Signals are atomic observations about a person's communication patterns,
sentiment, and availability. They feed into the mind model for state
estimation and relationship scoring.

PacoMind is a first-class participant in its own signal stream — not merely an
observer. PacoMind-sourced signals (``direction="pacomind"``) capture PacoMind's own
behavior: when it reaches out, how effective its actions are, how well its
adapted style is received. Contact-sourced signals (``direction="contact"``)
capture the other person's behavior. Bilateral signals (``direction="bilateral"``)
describe the exchange pattern between PacoMind and a contact.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class SignalType(str, Enum):
    """Categories of behavioral signals.

    Communication patterns track message flow characteristics.
    Sentiment signals capture emotional tone.
    Interaction quality measures depth of engagement.
    Availability tracks when someone is typically reachable.

    PacoMind signals (new) capture PacoMind's own behavior in the signal stream.
    These feed the pacomind_initiative_response, pacomind_effectiveness, and
    pacomind_style_fit scoring dimensions.
    """

    # Communication patterns
    MESSAGE_FREQUENCY = "message_frequency"
    RESPONSE_TIME = "response_time"
    INITIATION_RATE = "initiation_rate"

    # Sentiment
    SENTIMENT_POSITIVE = "sentiment_positive"
    SENTIMENT_NEGATIVE = "sentiment_negative"
    SENTIMENT_NEUTRAL = "sentiment_neutral"

    # Interaction quality
    ENGAGEMENT_DEPTH = "engagement_depth"
    TOPIC_DIVERSITY = "topic_diversity"

    # Availability
    TYPICAL_ACTIVE_HOURS = "typical_active_hours"
    AVAILABILITY_PATTERN = "availability_pattern"

    # PacoMind-sourced signals (PacoMind is a participant, not just an observer)
    PACOMIND_INITIATIVE = "pacomind_initiative"           # PacoMind reached out first
    PACOMIND_SUGGESTION = "pacomind_suggestion"           # PacoMind made a suggestion/action
    PACOMIND_EFFECTIVENESS = "pacomind_effectiveness"     # Outcome of PacoMind's action
    CONTACT_RESPONSE_TO_PACOMIND = "contact_response_to_pacomind"  # Response to PacoMind-origin
    PACOMIND_STYLE_SIGNAL = "pacomind_style_signal"       # Style adaptation feedback

    # Legacy aliases kept for migration compatibility
    OWNER_EXPLICIT = "owner_explicit"


class SignalStrength(str, Enum):
    """Confidence level of a signal observation.

    WEAK: Low confidence, sparse data
    MODERATE: Reasonable confidence, sufficient data
    STRONG: High confidence, abundant data
    """

    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


@dataclass
class Signal:
    """A single behavioral observation about a person.

    Signals are the raw building blocks of PacoMind's understanding.
    Each signal captures one dimension of behavior at a point in time.

    Attributes:
        id: Unique identifier
        person_id: The person this signal relates to
        signal_type: What kind of behavior was observed
        value: Normalized value between 0 and 1
        raw_value: Original unnormalized value (for debugging/recalibration)
        strength: Confidence level of this observation
        source: How the signal was generated ("observed", "inferred", "explicit")
        timestamp: When the signal was captured
        context: Additional metadata (channel, message_id, etc.)
        direction: Who the signal is about.
            "contact" — describes the contact's behavior (existing signals).
            "pacomind"  — describes PacoMind's own behavior (pacomind_initiative etc.).
            "bilateral" — describes the exchange pattern (e.g. initiation_ratio).
    """

    id: str
    person_id: str
    signal_type: SignalType
    value: float
    raw_value: Optional[float] = None
    strength: SignalStrength = SignalStrength.MODERATE
    source: str = "observed"
    timestamp: Optional[datetime] = None
    context: Optional[Dict[str, Any]] = None
    direction: str = "contact"  # "contact" | "pacomind" | "bilateral"
