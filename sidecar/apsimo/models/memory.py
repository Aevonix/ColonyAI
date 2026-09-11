"""Memory storage models.

Colony's memory system stores three types of knowledge:
- Episodic: Events and conversations (what happened)
- Semantic: Facts and preferences (what is known)
- Procedural: Workflows and processes (how to do things)

Memories support semantic search via embeddings and decay/reinforce
based on access patterns.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class MemoryType(str, Enum):
    """Classification of memory content.

    EPISODIC: Events, conversations, interactions
    SEMANTIC: Facts, preferences, knowledge
    PROCEDURAL: How-to knowledge, workflows, processes
    """

    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryStrength(str, Enum):
    """How strongly a memory is retained.

    LOW: Weak retention, score < 0.3. Candidate for decay.
    MEDIUM: Normal retention, score 0.3-0.7.
    HIGH: Strong retention, score > 0.7. Frequently accessed or important.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class Memory:
    """A single memory unit in Colony's long-term store.

    Memories are created from interactions, observations, and explicit input.
    They support vector similarity search via the embedding field and have
    strength/importance scores that evolve with access patterns.

    Attributes:
        id: Unique identifier
        type: Category of memory (episodic, semantic, procedural)
        content: The actual memory content as text
        person_id: Related person, if the memory is about someone specific
        strength: Retention strength 0-1, decays without access
        importance: How important this memory is 0-1
        tags: Categorization tags for filtering
        embedding: Vector embedding for semantic search
        source: Where this memory originated (channel, file, etc.)
        created_at: When the memory was first stored
        last_accessed: Most recent retrieval timestamp
        access_count: How many times this memory has been retrieved
        metadata: Additional structured data
    """

    id: str
    type: MemoryType
    content: str
    person_id: Optional[str] = None
    strength: float = 0.5
    importance: float = 0.5
    tags: List[str] = field(default_factory=list)
    embedding: Optional[List[float]] = None
    source: Optional[str] = None
    created_at: Optional[datetime] = None
    last_accessed: Optional[datetime] = None
    access_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
