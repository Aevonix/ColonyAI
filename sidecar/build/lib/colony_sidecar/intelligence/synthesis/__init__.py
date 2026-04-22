"""Synthesis package — cross-domain insight engine.

Discovers connections across domains (health, work, relationships),
scores them for novelty, validates quality, and routes insights
to appropriate delivery channels.

Public API:
    - ``ConnectionDiscoverer`` — find patterns across domains
    - ``Connection`` / ``ConnectionType`` — connection models
    - ``NoveltyScorer`` / ``NoveltyScore`` — score insight novelty
    - ``CrossDomainAnalyzer`` / ``DomainInsight`` — multi-domain analysis
    - ``InsightValidator`` / ``ValidationResult`` — quality gates
    - ``InsightDeliverer`` / ``DeliveryDecision`` / ``DeliveryChannel`` — routing
"""

from .connection_discoverer import Connection, ConnectionDiscoverer, ConnectionType
from .cross_domain_analyzer import CrossDomainAnalyzer, DomainInsight
from .insight_deliverer import DeliveryChannel, DeliveryDecision, InsightDeliverer
from .insight_validator import InsightValidator, ValidationResult
from .novelty_scorer import NoveltyScore, NoveltyScorer

__all__ = [
    # Connection discovery
    "ConnectionDiscoverer",
    "Connection",
    "ConnectionType",
    # Novelty scoring
    "NoveltyScorer",
    "NoveltyScore",
    # Cross-domain analysis
    "CrossDomainAnalyzer",
    "DomainInsight",
    # Validation
    "InsightValidator",
    "ValidationResult",
    # Delivery
    "InsightDeliverer",
    "DeliveryDecision",
    "DeliveryChannel",
]
