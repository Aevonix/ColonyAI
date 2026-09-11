"""Initiative delivery classification.

Splits initiative *types* into two disjoint routing classes:

* **reach-out** -- user-facing initiatives whose natural outcome is a message
  to a person (a follow-up, a relationship touch, an introduction...). These
  are delivered through the guarded proactive-delivery path so the host agent
  can compose and send them in its own voice, under the delivery rate limiter
  and approval gates.
* **internal** -- everything else (self-maintenance, health, research
  bookkeeping, data quality, capability gaps...). These are processed in-place
  by the sidecar's own execution backend and never messaged to a person.

The reach-out set is a small, conservative default and is fully overridable
from the environment so a deployment can widen or narrow it without a code
change. No deployment-specific identifiers live here.
"""

from __future__ import annotations

import os
from typing import FrozenSet

# Conservative default: types that inherently mean "contact a person".
# Anything not in this set is treated as internal / self-maintenance.
_DEFAULT_REACHOUT_TYPES: FrozenSet[str] = frozenset({
    "follow_up",
    "relationship",
    "introduction",
    "scheduling",
    "commitment",
    "calendar",
})

# Env override: comma-separated list of initiative types. When set (non-empty)
# it fully replaces the default set.
_ENV_VAR = "COLONY_REACHOUT_TYPES"


def reachout_types() -> FrozenSet[str]:
    """Return the set of initiative types routed to proactive delivery."""
    raw = os.environ.get(_ENV_VAR, "").strip()
    if raw:
        return frozenset(t.strip() for t in raw.split(",") if t.strip())
    return _DEFAULT_REACHOUT_TYPES


def is_reachout(initiative_type: str) -> bool:
    """True if this initiative type should be delivered to a person."""
    return bool(initiative_type) and initiative_type in reachout_types()
