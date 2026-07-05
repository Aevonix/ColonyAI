"""Self-model / trust engine: earned, graduated autonomy (item 4, Amendment 1).

Measures what the system is actually good at from real outcomes (executor
completions, project steps, directed audits, delivery pushes, worker jobs),
renders a compact brief injected into reasoning prompts, and, as the central
trust engine, converts that track record into per-class autonomy: act above
earned confidence (with journaling), ask when unsure, hold in calibration,
never self-decide the immutable floor. Circuit breakers demote a class on
clustered failures or any audit violation.

Measurement, journal and brief run live by default
(COLONY_SELF_MODEL_ENABLED, default true); gating applies wherever a
capability consults TrustEngine.gate().
"""

from colony_sidecar.self_model.store import CompetenceStore, SelfModel, self_model_enabled
from colony_sidecar.self_model.brief import self_brief
from colony_sidecar.self_model.journal import ActionJournal
from colony_sidecar.self_model.trust import TrustEngine, floor_class, autograduate_enabled
from colony_sidecar.self_model.params import (
    AdaptiveParamStore,
    register_core_params,
    PARAM_CONSOLIDATION_THRESHOLD,
    PARAM_RECALL_MIN_RELEVANCE,
)

__all__ = [
    "CompetenceStore", "SelfModel", "self_brief", "self_model_enabled",
    "ActionJournal", "TrustEngine", "floor_class", "autograduate_enabled",
    "AdaptiveParamStore", "register_core_params",
    "PARAM_CONSOLIDATION_THRESHOLD", "PARAM_RECALL_MIN_RELEVANCE",
]
