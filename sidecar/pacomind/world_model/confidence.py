"""Confidence scoring for World Model entities and properties."""

# Confidence initialization rules (entity-level)
CONFIDENCE_BY_SOURCE = {
    "structured_import": 0.95,   # calendar sync, contact import — highly reliable
    "document_extraction": 0.70, # named entity from PDF, email body
    "conversation_ner": 0.55,    # NER from message text
    "inferred": 0.30,            # deduced from context, not explicitly stated
    "unverified_mention": 0.20,  # single passing mention
}

# Confidence boost per corroborating observation
CORROBORATION_BOOST = 0.05

# Confidence ceiling
MAX_CONFIDENCE = 0.98

# Minimum confidence for an entity to appear in query results by default
DEFAULT_CONFIDENCE_THRESHOLD = 0.30

# Minimum confidence for storage
MIN_CONFIDENCE_FOR_STORAGE = 0.20


def compute_property_confidence(
    extraction_source: str,
    corroboration_count: int = 0,
) -> float:
    """Compute property-level confidence from source and corroboration.

    Args:
        extraction_source: Source type key from CONFIDENCE_BY_SOURCE.
        corroboration_count: Number of additional observations corroborating this value.

    Returns:
        Confidence in [0.0, MAX_CONFIDENCE].
    """
    base = CONFIDENCE_BY_SOURCE.get(extraction_source, 0.30)
    boosted = base + corroboration_count * CORROBORATION_BOOST
    return min(boosted, MAX_CONFIDENCE)


def boost_confidence(current: float, amount: float = CORROBORATION_BOOST) -> float:
    """Boost confidence by amount, capped at MAX_CONFIDENCE."""
    return min(current + amount, MAX_CONFIDENCE)


def apply_extraction_adjustments(base_confidence: float, adjustments: list[float]) -> float:
    """Apply a list of signed confidence adjustments to a base value.

    Clamps result to [0.0, MAX_CONFIDENCE].
    """
    result = base_confidence + sum(adjustments)
    return max(0.0, min(result, MAX_CONFIDENCE))
