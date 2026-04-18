"""Merge audit record dataclass."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class MergeAuditRecord:
    id: str                   # ma-<timestamp>-<random7>
    surviving_id: str         # entity that survived
    retired_id: str           # entity that was merged away
    relationships_repointed: int
    properties_updated: int
    executed_by: str          # "auto" | "owner_approved"
    merge_proposal_id: Optional[str]
    created_at: str
