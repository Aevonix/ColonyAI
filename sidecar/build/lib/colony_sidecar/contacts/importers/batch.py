"""Batch import result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class ImportOutcome(str, Enum):
    CREATED = "created"
    MERGED = "merged"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class ImportRecord:
    """Result for a single record in a batch import."""
    raw_display_name: Optional[str]
    outcome: ImportOutcome
    contact_id: Optional[str] = None       # set on CREATED or MERGED
    merged_into_id: Optional[str] = None   # set on MERGED
    error: Optional[str] = None            # set on FAILED


@dataclass
class BatchImportResult:
    """Summary of a completed batch import operation."""
    source: str
    total: int
    created: int = 0
    merged: int = 0
    skipped: int = 0
    failed: int = 0
    handle_conflicts: int = 0
    records: List[ImportRecord] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 1.0
        return (self.created + self.merged + self.skipped) / self.total
