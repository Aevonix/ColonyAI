"""Proposals: well-formed findings/suggestions delivered to the owner via the
guarded (shadow-gated, boundary-checked, rate-limited) reach-out path."""

from apsimo.proposals.models import Proposal, ProposalStore
from apsimo.proposals.engine import (
    build_from_thinker, build_from_research, proposal_to_payload,
)

__all__ = [
    "Proposal", "ProposalStore",
    "build_from_thinker", "build_from_research", "proposal_to_payload",
]
