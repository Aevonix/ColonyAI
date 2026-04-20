"""Colony's interaction signal collection — zero LLM, pure computation.

Colony extracts behavioral signals from every message it exchanges across connected
gateways. These signals build Colony's own picture of each person it interacts with:
how they communicate, when they're engaged, their emotional tone. Colony's relational
intelligence is built from this stream of first-hand observations, not from the
owner's address book or stated opinions.
"""
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)
from datetime import datetime
from typing import List, Optional, Protocol
import re

@dataclass
class Signal:
    signal_type: str
    raw_value: float
    normalized_value: float
    timestamp: datetime
    person_id: str
    source: str  # "message", "reaction", "call"
    context: Optional[dict] = None


class BaselineStore(Protocol):
    """Protocol for baseline store dependency."""
    async def get(self, person_id: str) -> "PersonBaseline": ...
    async def record_message(self, person_id: str, length: int, hour: int) -> None: ...


class ColonyGraph(Protocol):
    """Protocol for graph dependency."""
    async def store_signal(self, signal: Signal) -> None: ...
    async def get_recent_signals(self, person_id: str, hours: int, signal_type: Optional[str] = None) -> List[Signal]: ...


class PersonBaseline(Protocol):
    """Protocol for person baseline."""
    length_mean: float
    length_std: float
    preferred_hours: List[int]


class Message(Protocol):
    """Protocol for message input."""
    sender_id: str
    content: str
    timestamp: datetime
    reply_to_id: Optional[str] = None
    has_media: bool = False


class SignalCollector:
    """Extract behavioral signals from Colony's own interactions — zero LLM, pure computation.

    Each message Colony sends or receives is an observation. SignalCollector turns
    those observations into quantified signals (frequency, sentiment, latency, etc.)
    that feed Colony's mind model and relationship scoring. Colony builds its social
    intelligence from this first-hand evidence, independent of the owner's stated views.
    """

    POSITIVE_WORDS = {"happy", "great", "love", "thanks", "awesome", "excited", 
                      "good", "wonderful", "amazing", "fantastic", "perfect"}
    NEGATIVE_WORDS = {"sad", "angry", "frustrated", "sorry", "bad", "worried",
                      "terrible", "awful", "horrible", "hate", "upset"}
    NEGATION_WORDS = {"not", "no", "never", "don't", "doesn't", "didn't", "wasn't"}
    EMOJI_PATTERN = re.compile(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F700-\U0001F77F\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002702-\U000027B0]')

    def __init__(self, baseline_store: BaselineStore, graph: ColonyGraph, metrics=None):
        self.baselines = baseline_store
        self.graph = graph
        self._metrics = metrics  # Optional ColonyMetricsCollector

    async def collect(self, message: Message) -> List[Signal]:
        """Extract all signals from a single message in Colony's interaction stream."""
        t0 = time.monotonic()

    async def ingest_raw(self, signal_data: dict) -> None:
        """Ingest a pre-computed signal from an external source."""
        try:
            from colony_sidecar.models.signal import Signal, SignalType, SignalStrength
            sig_type = SignalType.ENGAGEMENT_DEPTH  # default
            raw_type = signal_data.get("type", "")
            for st in SignalType:
                if st.value == raw_type or st.name.lower() == raw_type.lower():
                    sig_type = st
                    break
            sig = Signal(
                id=signal_data.get("id", f"raw-{time.monotonic_ns()}"),
                person_id=signal_data.get("person_id", ""),
                signal_type=sig_type,
                value=float(signal_data.get("value", 0.5)),
                source=signal_data.get("source", "api"),
                context=signal_data.get("data"),
            )
            await self.baselines.store_signal(sig)
        except Exception as e:
            logger.warning("ingest_raw failed: %s", e)
        signals = []
        person_id = message.sender_id

        try:
            baseline = await self.baselines.get(person_id)
        except (LookupError, AttributeError, OSError, RuntimeError):
            baseline = None

        # 1. Message length (Z-score normalized)
        length = len(message.content)
        if baseline and baseline.length_std > 0:
            z_score = (length - baseline.length_mean) / baseline.length_std
        else:
            z_score = 0.0
        signals.append(Signal(
            signal_type="message_length",
            raw_value=length,
            normalized_value=z_score,
            timestamp=message.timestamp,
            person_id=person_id,
            source="message",
        ))

        # 2. Sentiment (lexicon-based, no LLM)
        sentiment = self._compute_sentiment(message.content)
        signals.append(Signal(
            signal_type="sentiment",
            raw_value=sentiment,
            normalized_value=sentiment,  # Already -1 to 1
            timestamp=message.timestamp,
            person_id=person_id,
            source="message",
        ))

        # 3. Emoji usage
        emoji_count = len(self.EMOJI_PATTERN.findall(message.content))
        emoji_per_100 = (emoji_count / max(len(message.content), 1)) * 100
        signals.append(Signal(
            signal_type="emoji_usage",
            raw_value=emoji_count,
            normalized_value=emoji_per_100,
            timestamp=message.timestamp,
            person_id=person_id,
            source="message",
        ))

        # 4. Media usage
        signals.append(Signal(
            signal_type="media_usage",
            raw_value=1.0 if message.has_media else 0.0,
            normalized_value=1.0 if message.has_media else 0.0,
            timestamp=message.timestamp,
            person_id=person_id,
            source="message",
        ))

        # 5. Time of day
        hour = message.timestamp.hour
        if baseline and hasattr(baseline, 'preferred_hours') and baseline.preferred_hours:
            if hour in baseline.preferred_hours:
                tod_bucket = 0  # preferred
            elif 8 <= hour <= 22:
                tod_bucket = 1  # off-hours but reasonable
            else:
                tod_bucket = 2  # late-night
        else:
            if 8 <= hour <= 22:
                tod_bucket = 0
            elif 6 <= hour < 8 or 22 < hour <= 24:
                tod_bucket = 1
            else:
                tod_bucket = 2
        signals.append(Signal(
            signal_type="time_of_day",
            raw_value=float(hour),
            normalized_value=float(tod_bucket),
            timestamp=message.timestamp,
            person_id=person_id,
            source="message",
        ))

        # 6. Message frequency (24h rolling)
        recent_count = await self._get_message_frequency(person_id, hours=24)
        signals.append(Signal(
            signal_type="message_frequency",
            raw_value=float(recent_count + 1),  # +1 for this message
            normalized_value=min(float(recent_count + 1) / 10.0, 10.0),  # Cap at 10
            timestamp=message.timestamp,
            person_id=person_id,
            source="message",
        ))

        # 7. Response latency (if replying)
        if message.reply_to_id:
            latency = await self._compute_latency(message)
            if latency is not None:
                signals.append(Signal(
                    signal_type="response_latency",
                    raw_value=latency,
                    normalized_value=min(latency / 60.0, 24.0),  # Hours, cap at 24
                    timestamp=message.timestamp,
                    person_id=person_id,
                    source="message",
                ))

        # 8. Initiation ratio (30-day window)
        init_ratio = await self._compute_initiation_ratio(person_id, days=30)
        signals.append(Signal(
            signal_type="initiation_ratio",
            raw_value=init_ratio,
            normalized_value=init_ratio,  # Already 0-1
            timestamp=message.timestamp,
            person_id=person_id,
            source="message",
        ))

        # Persist all signals to graph
        for sig in signals:
            try:
                await self.graph.store_signal(sig)
            except (OSError, RuntimeError) as exc:
                logger.debug("SignalCollector: failed to store signal %s: %s", sig.signal_type, exc)

        # Update baseline with this message
        try:
            if hasattr(self.baselines, 'record_message'):
                await self.baselines.record_message(person_id, length, hour)
        except (OSError, RuntimeError, AttributeError) as exc:
            logger.debug("SignalCollector: failed to update baseline for %s: %s", person_id, exc)

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info("Collected %d signals for %s in %.1fms", len(signals), person_id, elapsed_ms)
        if self._metrics is not None:
            try:
                self._metrics.record_signal_collection(
                    latency_ms=elapsed_ms,
                    signal_count=len(signals),
                    signal_types=[s.signal_type for s in signals],
                )
            except Exception:
                pass

        return signals

    def _compute_sentiment(self, text: str) -> float:
        """Lexicon-based sentiment with negation handling."""
        raw_words = text.lower().split()
        # Strip punctuation so "great!" matches "great", etc.
        words = [w.strip(".,!?;:\"'()[]{}") for w in raw_words]
        pos_count = 0
        neg_count = 0

        for i, word in enumerate(words):
            # Check for negation in previous 3 words
            negated = False
            for j in range(max(0, i - 3), i):
                if words[j] in self.NEGATION_WORDS:
                    negated = True
                    break

            if word in self.POSITIVE_WORDS:
                if negated:
                    neg_count += 0.5  # Negated positive = weak negative
                else:
                    pos_count += 1
            elif word in self.NEGATIVE_WORDS:
                if negated:
                    pos_count += 0.5  # Negated negative = weak positive
                else:
                    neg_count += 1

        total = pos_count + neg_count
        if total == 0:
            return 0.0
        return (pos_count - neg_count) / total

    async def _get_message_frequency(self, person_id: str, hours: int = 24) -> int:
        """Count messages in the last N hours."""
        try:
            signals = await self.graph.get_recent_signals(
                person_id, hours=hours, signal_type="message_length"
            )
            return len(signals)
        except (OSError, RuntimeError):
            return 0

    async def _compute_latency(self, message: Message) -> Optional[float]:
        """Compute response latency in minutes for a reply message.

        Uses the most recent prior signal as a proxy for the original
        message timestamp, since Signal.context doesn't carry message IDs.
        Returns None if no prior signals exist or on error.
        """
        if not message.reply_to_id:
            return None
        try:
            # Fetch recent signals ordered by timestamp DESC; the most recent
            # signal before this message is the best proxy for what's being
            # replied to.
            prior_signals = await self.graph.get_recent_signals(
                message.sender_id, hours=24 * 7, signal_type="message_length"
            )
            for sig in prior_signals:
                if sig.timestamp < message.timestamp:
                    delta = (message.timestamp - sig.timestamp).total_seconds() / 60.0
                    return max(0.0, delta)
        except (OSError, RuntimeError):
            pass
        return None

    async def _compute_initiation_ratio(self, person_id: str, days: int = 30) -> float:
        """Compute conversation initiation ratio over past N days.

        A new conversation starts when there is a 4-hour gap since the
        previous message from this person.  Since we only track signals
        from this person (not Colony's outbound messages), we approximate
        the initiation ratio as (person-initiated conversations) / total.
        A signal with ``source="inbound"`` means the person sent it;
        ``source="outbound"`` means Colony sent first.  If direction info
        is unavailable (all ``source="message"``), we fall back to the
        ratio of conversation-starts to total messages as an activity proxy.

        Returns a value in [0, 1].  Returns 0.5 when there is insufficient data.
        """
        GAP_HOURS = 4
        try:
            signals = await self.graph.get_recent_signals(
                person_id, hours=days * 24, signal_type="message_length"
            )
            if not signals:
                return 0.5

            signals.sort(key=lambda s: s.timestamp)
            conversation_starts = 0
            person_starts = 0
            prev_ts = None
            has_direction = any(s.source in ("inbound", "outbound") for s in signals)

            for sig in signals:
                is_new_convo = (
                    prev_ts is None
                    or (sig.timestamp - prev_ts).total_seconds() > GAP_HOURS * 3600
                )
                if is_new_convo:
                    conversation_starts += 1
                    if has_direction:
                        if sig.source == "inbound":
                            person_starts += 1
                    else:
                        # No direction info — count all conversation starts
                        # as person-initiated (best available approximation)
                        person_starts += 1
                prev_ts = sig.timestamp

            if conversation_starts == 0:
                return 0.5
            return person_starts / conversation_starts
        except (OSError, RuntimeError):
            return 0.5
