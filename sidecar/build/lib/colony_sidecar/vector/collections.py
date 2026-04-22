"""Colony Vector Store — collection definitions and schemas.

Each collection maps to a separate LanceDB table.  All tables share a
common base schema; per-collection metadata is stored as a JSON string.
"""

from __future__ import annotations

from enum import Enum


class Collection(str, Enum):
    """Named vector collections — each becomes a LanceDB table."""

    MEMORIES = "memories"          # Text + images mixed
    SKILLS = "skills"              # Text only
    ENTITIES = "entities"          # Text only
    DOCUMENTS = "documents"        # Text + scanned images
    CONTACTS = "contacts"          # Text only
    COGNITION = "cognition"        # Text only
    IDENTITY = "identity"          # Text only
    CONVERSATIONS = "conversations" # Text + images (shared photos)
    MEDIA = "media"                # Images primarily, with captions


# Per-collection metadata key documentation (informational; stored as JSON)
COLLECTION_METADATA_KEYS: dict[Collection, list[str]] = {
    Collection.MEMORIES: [
        "memory_id", "type", "strength", "importance",
        "person_id", "tags", "created_at",
    ],
    Collection.SKILLS: [
        "skill_id", "name", "fingerprint_hash",
        "dependency_hash", "created_at",
    ],
    Collection.ENTITIES: [
        "entity_id", "type", "canonical_name", "aliases", "domain",
    ],
    Collection.DOCUMENTS: [
        "doc_id", "chunk_index", "source_url", "title", "research_task_id",
    ],
    Collection.CONTACTS: [
        "contact_id", "platform", "handle", "last_seen_at",
    ],
    Collection.COGNITION: [
        "cycle_id", "cpi_score", "gaps", "adjustments", "cycle_at",
    ],
    Collection.IDENTITY: [
        "assertion_id", "aspect", "confidence",
    ],
}
