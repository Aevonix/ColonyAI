"""Colony Vector Store — query and result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class VectorResult:
    """A single result from vector search."""

    id: str
    score: float  # cosine similarity in [0, 1]
    text: str  # original embedded text
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorQuery:
    """Parameters for a vector similarity search."""

    vector: list[float]
    limit: int = 10
    filter: Optional[str] = None  # LanceDB SQL filter expression
    min_score: float = 0.0


@dataclass
class VectorItem:
    """An item to insert into the vector store."""

    id: str
    text: str
    vector: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HybridQuery:
    """Combined dense + sparse (keyword) query — future use."""

    vector: list[float]
    text: str  # keyword query for BM25 sparse search
    limit: int = 10
    filter: Optional[str] = None
    min_score: float = 0.0
    dense_weight: float = 0.7
    sparse_weight: float = 0.3
