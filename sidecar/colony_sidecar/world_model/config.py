"""World Model configuration dataclass."""

from dataclasses import dataclass, field
from typing import List


@dataclass
class WorldModelConfig:
    # ── Feature flags ──────────────────────────────────────────────────
    enabled: bool = True
    auto_extract_conversations: bool = True
    auto_extract_documents: bool = True
    structured_import_enabled: bool = True
    entity_resolution_enabled: bool = True

    # ── Backend ────────────────────────────────────────────────────────
    backend: str = "sqlite"         # "neo4j" | "sqlite"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_database: str = "colony"
    sqlite_path: str = "colony_world_model.db"

    # ── Confidence thresholds ──────────────────────────────────────────
    min_confidence_for_storage: float = 0.20
    min_confidence_for_query: float = 0.30
    auto_merge_confidence_threshold: float = 0.85
    propose_merge_confidence_threshold: float = 0.70

    # ── Resolution ─────────────────────────────────────────────────────
    string_similarity_auto_merge: float = 0.92
    string_similarity_propose: float = 0.75

    # ── Extraction ─────────────────────────────────────────────────────
    max_extraction_ms: int = 50     # per-message budget
    min_message_length_for_extraction: int = 20
    extraction_spacy_model: str = "en_core_web_sm"

    # ── Graph traversal ────────────────────────────────────────────────
    max_graph_hops: int = 5
    default_graph_hops: int = 2
    max_traversal_nodes: int = 200

    # ── Retention ─────────────────────────────────────────────────────
    low_confidence_entity_ttl_days: int = 90
    merge_proposal_expiry_days: int = 30

    # ── Briefing limits ────────────────────────────────────────────────
    max_world_model_briefing_items_per_day: int = 3
