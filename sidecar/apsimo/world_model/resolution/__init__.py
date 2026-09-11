"""Entity resolution and merge workflow."""
from .entity_resolver import EntityResolver, ResolutionAction, ResolutionResult
from .merge_workflow import MergeWorkflow, MergeProposal
from .merge_audit import MergeAuditRecord

__all__ = [
    "EntityResolver",
    "ResolutionAction",
    "ResolutionResult",
    "MergeWorkflow",
    "MergeProposal",
    "MergeAuditRecord",
]
