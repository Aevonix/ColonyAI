"""Colony intelligence learning sub-package.

Provides:
- FeedbackStore   — persist/retrieve user corrections
- ContinuousLearner — near-real-time signal ingestion and weight updates
"""

from .feedback_store import FeedbackStore, UserCorrection
from .continuous_learner import ContinuousLearner, BriefingEngagement, GoalOutcome

__all__ = [
    "FeedbackStore",
    "UserCorrection",
    "ContinuousLearner",
    "BriefingEngagement",
    "GoalOutcome",
]
