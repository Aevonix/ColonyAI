"""Merge workflow: automatic and proposed entity merges."""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..store import WorldModelStore


@dataclass
class MergeProposal:
    id: str                          # mp-<timestamp>-<random7>
    candidate_id: str                # the incoming entity
    existing_id: str                 # the entity to merge into
    match_confidence: float
    match_reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"          # "pending" | "approved" | "rejected" | "expired"
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None


class MergeWorkflow:
    """Manages proposed and automatic entity merges."""

    def __init__(self, store: "WorldModelStore") -> None:
        self._store = store

    async def propose_merge(
        self,
        candidate_id: str,
        existing_id: str,
        confidence: float,
        reason: str,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> MergeProposal:
        """Create a merge proposal and enqueue for owner review."""
        proposal_id = await self._store._backend.create_merge_proposal(
            candidate_id=candidate_id,
            existing_id=existing_id,
            confidence=confidence,
            reason=reason,
            evidence=evidence,
        )
        return MergeProposal(
            id=proposal_id,
            candidate_id=candidate_id,
            existing_id=existing_id,
            match_confidence=confidence,
            match_reason=reason,
            evidence=evidence or {},
        )

    async def execute_merge(self, proposal_id: str) -> None:
        """Execute an approved merge. The candidate entity is retired.

        All relationships pointing to candidate_id are repointed to
        existing_id. A merge audit record is created.
        """
        proposal = await self._store._backend.get_merge_proposal(proposal_id)
        if not proposal:
            raise ValueError(f"Merge proposal not found: {proposal_id}")
        if proposal["status"] not in ("pending", "approved"):
            raise ValueError(
                f"Cannot execute merge in status '{proposal['status']}'"
            )

        await self._store._backend.execute_merge(
            surviving_id=proposal["existing_id"],
            retired_id=proposal["candidate_id"],
            executed_by="owner_approved",
            proposal_id=proposal_id,
        )
        await self._store._backend.update_merge_proposal_status(
            proposal_id, "approved"
        )

    async def execute_auto_merge(
        self,
        surviving_id: str,
        retired_id: str,
    ) -> str:
        """Execute an automatic high-confidence merge without owner review.

        Returns the merge audit record ID.
        """
        return await self._store._backend.execute_merge(
            surviving_id=surviving_id,
            retired_id=retired_id,
            executed_by="auto",
            proposal_id=None,
        )

    async def reject_merge(self, proposal_id: str) -> None:
        """Reject a merge proposal.

        Both entities remain separate. The candidate's aliases list is
        updated to avoid re-proposing the same merge.
        """
        proposal = await self._store._backend.get_merge_proposal(proposal_id)
        if not proposal:
            raise ValueError(f"Merge proposal not found: {proposal_id}")

        await self._store._backend.update_merge_proposal_status(
            proposal_id, "rejected"
        )

        # Add candidate's name as alias on existing to avoid re-proposing
        candidate = await self._store.get_entity(proposal["candidate_id"])
        if candidate:
            await self._store.add_entity_alias(
                proposal["existing_id"], candidate.name
            )

    async def get_pending_proposals(self) -> list:
        """Return all pending merge proposals."""
        return await self._store._backend.get_pending_merge_proposals()
