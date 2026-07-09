"""Directive / boundary memory: durable owner boundaries + enforcement.

Public surface:
    DirectiveManager  -- capture + store + enforce (single entry point)
    DirectiveStore    -- SQLite persistence
    DirectiveGuard    -- boundary enforcement (check an Action -> Verdict)
    Action, Verdict   -- the enforcement contract
    Directive, Polarity, DirectiveStatus -- the data model
"""

from colony_sidecar.directives.models import (
    Directive, Polarity, DirectiveStatus, normalize_terms,
)
from colony_sidecar.directives.store import DirectiveStore
from colony_sidecar.directives.guard import (
    DirectiveGuard, Action, Verdict, boundary_fail_closed,
)
from colony_sidecar.directives.service import DirectiveManager

__all__ = [
    "Directive", "Polarity", "DirectiveStatus", "normalize_terms",
    "DirectiveStore", "DirectiveGuard", "Action", "Verdict", "DirectiveManager",
    "boundary_fail_closed",
]
