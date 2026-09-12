"""Legacy goal records and optional planner compatibility.

New execution belongs to native runtime tasks and canonical commitments.
The default GoalEngine stores accepted goals without an execution backend.
Planner exports remain for existing readers and stored-goal migrations;
their presence does not establish live execution.
"""

from importlib import import_module

from .config import GoalEngineConfig
from .models import (
    Goal,
    GoalDAG,
    GoalOutcome,
    GoalPriority,
    GoalSource,
    GoalStatus,
    GoalSummary,
    GoalTransitionRecord,
    Subtask,
    SubtaskStatus,
)
from .store import GoalNotFoundError, GoalStore

# A reader importing goals.models/store must not load the optional planner.
# Preserve public exports for explicitly configured legacy callers, including
# class identity and pickle module names, without constructing another facade.
_PLANNER_EXPORTS = {
    name: module for module, names in (
        ('engine', ('GoalEngine',)),
        ('decomposer', ('DecompositionTemplate', 'GoalDecomposer', 'SubtaskSpec')),
        ('inference', ('ConversationMessage', 'GoalDeduplicator', 'GoalInferencePipeline',
                       'GoalSimilarity', 'InferenceCandidate', 'IntentSignal')),
        ('priority', ('GoalProgressTracker', 'GoalPriorityScorer', 'PriorityScore',
                      'UserPreferenceProfile')),
        ('queue_bridge', ('GoalQueueBridge', 'InMemoryQueueBackend')),
        ('replan', ('FailureAnalysis', 'FailureClass', 'ReplanEngine', 'ReplanResult',
                    'ReplanStrategy')),
    ) for name in names
}


def __getattr__(name):
    module = _PLANNER_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    value = getattr(import_module('.' + module, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))

__all__ = [
    # Config
    "GoalEngineConfig",
    # Engine
    "GoalEngine",
    # Models
    "Goal",
    "GoalDAG",
    "GoalOutcome",
    "GoalPriority",
    "GoalSource",
    "GoalStatus",
    "GoalSummary",
    "GoalTransitionRecord",
    "Subtask",
    "SubtaskStatus",
    # Inference
    "ConversationMessage",
    "GoalDeduplicator",
    "GoalInferencePipeline",
    "GoalSimilarity",
    "InferenceCandidate",
    "IntentSignal",
    # Decomposer
    "DecompositionTemplate",
    "GoalDecomposer",
    "SubtaskSpec",
    # Priority / Progress
    "GoalProgressTracker",
    "GoalPriorityScorer",
    "PriorityScore",
    "UserPreferenceProfile",
    # Queue Bridge
    "GoalQueueBridge",
    "InMemoryQueueBackend",
    # Replan
    "FailureAnalysis",
    "FailureClass",
    "ReplanEngine",
    "ReplanResult",
    "ReplanStrategy",
    # Store
    "GoalNotFoundError",
    "GoalStore",
]
