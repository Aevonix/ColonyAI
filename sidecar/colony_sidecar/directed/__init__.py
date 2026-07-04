"""Directed action: owner directive -> gated, delegated, audited execution.

Option A architecture: Colony never mutates external systems itself. It
scopes the directive deterministically, gates it (boundaries first, then
approval), dispatches the contract to an env-configured delegate, audits
what actually happened against the granted scope, and reports back through
the guarded reach-out path.
"""

from colony_sidecar.directed.models import (
    ScopedTask, ScopedTaskStore, ScopeLimits, READ_OPS, MUTATE_OPS,
)
from colony_sidecar.directed.intake import scope_from_directive, resolve_targets
from colony_sidecar.directed.audit import audit_completion, audit_via_report
from colony_sidecar.directed.service import DirectedActionService, directed_mode

__all__ = [
    "ScopedTask", "ScopedTaskStore", "ScopeLimits", "READ_OPS", "MUTATE_OPS",
    "scope_from_directive", "resolve_targets",
    "audit_completion", "audit_via_report",
    "DirectedActionService", "directed_mode",
]
