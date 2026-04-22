"""Colony Goal Engine — DAG-based goal decomposition and lifecycle management."""

from .config import GoalEngineConfig
from .decomposer import DecompositionTemplate, GoalDecomposer, SubtaskSpec
from .engine import GoalEngine
from .inference import (
    ConversationMessage,
    GoalDeduplicator,
    GoalInferencePipeline,
    GoalSimilarity,
    InferenceCandidate,
    IntentSignal,
)
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
from .priority import GoalProgressTracker, GoalPriorityScorer, PriorityScore, UserPreferenceProfile
from .queue_bridge import GoalQueueBridge, InMemoryQueueBackend
from .replan import (
    FailureAnalysis,
    FailureClass,
    ReplanEngine,
    ReplanResult,
    ReplanStrategy,
)
from .store import GoalNotFoundError, GoalStore

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
