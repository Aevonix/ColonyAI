"""Runtime outcome history, owner-directed perspective and existing trust helpers.

Execution records retain their source and correction history. Their prompt
brief describes recorded runtime labels and current load without establishing
task quality or current-model competence. Source-backed owner preferences and
dated attention are maintained separately from those outcome labels.

SelfModel is enabled by default (COLONY_SELF_MODEL_ENABLED); individual outcome
producers must be wired and running to supply records. The existing TrustEngine
and its opt-in graduation behavior are unchanged by the perspective. Gating
applies wherever a capability explicitly consults TrustEngine.gate().
"""

from colony_sidecar.self_model.store import CompetenceStore, SelfModel, self_model_enabled
from colony_sidecar.self_model.brief import self_brief
from colony_sidecar.self_model.journal import ActionJournal
from colony_sidecar.self_model.trust import TrustEngine, floor_class, autograduate_enabled
from colony_sidecar.self_model.supervised import (
    REVERSIBLE_CONTRACT,
    effective_mode,
    reversible,
    supervised_enabled,
)
from colony_sidecar.self_model.params import (
    AdaptiveParamStore,
    register_core_params,
    PARAM_CONSOLIDATION_THRESHOLD,
    PARAM_RECALL_MIN_RELEVANCE,
)

__all__ = [
    "CompetenceStore", "SelfModel", "self_brief", "self_model_enabled",
    "ActionJournal", "TrustEngine", "floor_class", "autograduate_enabled",
    "REVERSIBLE_CONTRACT", "effective_mode", "reversible", "supervised_enabled",
    "AdaptiveParamStore", "register_core_params",
    "PARAM_CONSOLIDATION_THRESHOLD", "PARAM_RECALL_MIN_RELEVANCE",
]
