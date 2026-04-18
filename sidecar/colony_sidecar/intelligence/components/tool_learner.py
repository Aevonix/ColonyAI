"""Tool Learner — learn user's preferred tools for different tasks.

Tracks:
    - Which tools are used for which task types
    - Success rates per tool
    - User satisfaction signals (explicit + implicit)
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import logging

logger = logging.getLogger(__name__)


@dataclass
class ToolUsage:
    """Record of a tool being used for a task.

    Attributes:
        tool_name: Name of the tool used
        task_type: Category of task the tool was applied to
        success: Whether the tool usage succeeded
        user_feedback: Optional explicit feedback ("helpful", "not_helpful", None)
        timestamp: When the usage occurred
    """

    tool_name: str
    task_type: str
    success: bool
    user_feedback: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ToolPreference:
    """Learned preference for a tool on a task type.

    Attributes:
        tool_name: Name of the preferred tool
        task_type: Task category this preference applies to
        success_rate: Running success rate (0-1)
        usage_count: Total number of times this tool was used for this task type
        last_used: Most recent usage timestamp
        user_rating: Recency-weighted user satisfaction score (0-1)
    """

    tool_name: str
    task_type: str
    success_rate: float
    usage_count: int
    last_used: datetime
    user_rating: float


class ToolLearner:
    """Learn and apply tool preferences from usage patterns.

    Maintains in-memory preference records per task type, updated on
    each recorded usage. Preferred tool selection blends success rate
    (60%) with user rating (40%).

    Args:
        graph_client: Colony graph client for persistent storage
    """

    def __init__(self, graph_client: Any) -> None:
        self.graph = graph_client
        self._preferences: Dict[str, List[ToolPreference]] = {}

    async def record_usage(self, usage: ToolUsage) -> None:
        """Record a tool usage event and update preferences.

        Args:
            usage: The tool usage to record
        """
        key = usage.task_type
        if key not in self._preferences:
            self._preferences[key] = []

        # Find or create preference record
        pref = next(
            (p for p in self._preferences[key] if p.tool_name == usage.tool_name),
            None,
        )

        if pref:
            old_count = pref.usage_count
            pref.usage_count += 1
            pref.success_rate = (
                (pref.success_rate * old_count + (1.0 if usage.success else 0.0))
                / pref.usage_count
            )
            pref.last_used = usage.timestamp
        else:
            self._preferences[key].append(
                ToolPreference(
                    tool_name=usage.tool_name,
                    task_type=usage.task_type,
                    success_rate=1.0 if usage.success else 0.0,
                    usage_count=1,
                    last_used=usage.timestamp,
                    user_rating=0.5,
                )
            )

        logger.debug(
            "Recorded tool usage: %s for %s (success=%s)",
            usage.tool_name,
            usage.task_type,
            usage.success,
        )

    async def get_preferred_tool(self, task_type: str) -> Optional[str]:
        """Get the preferred tool for a task type.

        Selection blends success rate (60%) with user rating (40%).

        Args:
            task_type: The task category to find a tool for

        Returns:
            Tool name or None if no preferences exist
        """
        if task_type not in self._preferences:
            return None

        prefs = self._preferences[task_type]
        if not prefs:
            return None

        sorted_prefs = sorted(
            prefs,
            key=lambda p: (p.success_rate * 0.6 + p.user_rating * 0.4),
            reverse=True,
        )

        return sorted_prefs[0].tool_name

    async def get_all_preferences(self, task_type: str) -> List[ToolPreference]:
        """Get all tool preferences for a task type, sorted by score.

        Args:
            task_type: The task category to query

        Returns:
            Sorted list of preferences (best first)
        """
        prefs = self._preferences.get(task_type, [])
        return sorted(
            prefs,
            key=lambda p: (p.success_rate * 0.6 + p.user_rating * 0.4),
            reverse=True,
        )

    async def update_user_rating(
        self,
        tool_name: str,
        task_type: str,
        rating: float,
    ) -> None:
        """Update the user satisfaction rating for a tool on a task type.

        Uses an exponential moving average (alpha=0.3) so recent feedback
        is weighted more heavily without fully discarding history.

        Args:
            tool_name: Name of the tool to rate
            task_type: Task category this rating applies to
            rating: New rating value (0-1)
        """
        rating = max(0.0, min(1.0, rating))
        prefs = self._preferences.get(task_type, [])
        pref = next((p for p in prefs if p.tool_name == tool_name), None)
        if pref is None:
            logger.warning("No preference record for %s on %s", tool_name, task_type)
            return
        alpha = 0.3
        pref.user_rating = alpha * rating + (1 - alpha) * pref.user_rating
        logger.debug(
            "Updated user rating for %s on %s: %.2f",
            tool_name,
            task_type,
            pref.user_rating,
        )

    async def get_usage_stats(self, task_type: str) -> Dict[str, Any]:
        """Return summary statistics for all tools used on a task type.

        Args:
            task_type: The task category to summarise

        Returns:
            Dict with 'total_tools', 'total_usages', 'best_tool', and per-tool stats.
        """
        prefs = self._preferences.get(task_type, [])
        if not prefs:
            return {"total_tools": 0, "total_usages": 0, "best_tool": None, "tools": []}

        best = await self.get_preferred_tool(task_type)
        return {
            "total_tools": len(prefs),
            "total_usages": sum(p.usage_count for p in prefs),
            "best_tool": best,
            "tools": [
                {
                    "name": p.tool_name,
                    "usage_count": p.usage_count,
                    "success_rate": p.success_rate,
                    "user_rating": p.user_rating,
                    "score": p.success_rate * 0.6 + p.user_rating * 0.4,
                }
                for p in prefs
            ],
        }
