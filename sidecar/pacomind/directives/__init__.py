"""Directive / boundary memory: durable owner boundaries + enforcement.

Public surface:
    DirectiveManager  -- capture + store + enforce (single entry point)
    DirectiveStore    -- SQLite persistence
    DirectiveGuard    -- boundary enforcement (check an Action -> Verdict)
    Action, Verdict   -- the enforcement contract
    Directive, Polarity, DirectiveStatus -- the data model
"""

from pacomind.directives.models import (
    Directive, Polarity, DirectiveStatus, normalize_terms,
)
from pacomind.directives.store import DirectiveStore
from pacomind.directives.guard import (
    DirectiveGuard, DirectiveStoreUnavailable, Action, Verdict,
    boundary_fail_closed,
)
from pacomind.directives.service import DirectiveManager

__all__ = [
    "Directive", "Polarity", "DirectiveStatus", "normalize_terms",
    "DirectiveStore", "DirectiveGuard", "DirectiveStoreUnavailable", "Action",
    "Verdict", "DirectiveManager", "boundary_fail_closed",
]
