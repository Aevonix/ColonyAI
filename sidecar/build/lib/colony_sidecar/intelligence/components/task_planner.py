"""Task Planner — break complex requests into subtasks.

Capabilities:
    - Dependency detection
    - Parallel task identification
    - Resource estimation
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set

import logging

logger = logging.getLogger(__name__)

# Patterns for identifying independently-executable task types
_INDEPENDENT_TASK_PATTERNS = [
    re.compile(r'\b(research|look up|find out|search for)\b'),
    re.compile(r'\b(write|draft|compose|create)\b'),
    re.compile(r'\b(schedule|book|reserve|arrange)\b'),
    re.compile(r'\b(notify|email|message|send)\b'),
    re.compile(r'\b(download|fetch|retrieve)\b'),
]

# Patterns indicating an explicit sequential dependency between tasks
_DEPENDENCY_PATTERNS = [
    re.compile(r'\b(then|after|once|following|based on|using the|with the)\b'),
]

# Keywords indicating sequential ordering (creates dependencies)
_SEQUENTIAL_PATTERN = re.compile(
    r"\s+(?:then|next|finally|after that|subsequently|afterward|before)\s+",
    re.IGNORECASE,
)

# Keywords indicating parallel tasks (no dependencies)
_PARALLEL_PATTERN = re.compile(
    r"\s+(?:and also|also|simultaneously|at the same time|in parallel)\s+",
    re.IGNORECASE,
)

# Keywords that suggest higher effort
_HIGH_EFFORT_WORDS = frozenset(
    {"research", "analyze", "analyse", "comprehensive", "detailed",
     "thorough", "complex", "deep", "investigate", "audit"}
)

# Keywords that suggest lower effort
_LOW_EFFORT_WORDS = frozenset(
    {"quick", "simple", "brief", "short", "fast", "small", "minor",
     "trivial", "basic", "easy"}
)

# Keywords that elevate priority
_CRITICAL_WORDS = frozenset(
    {"critical", "urgent", "immediately", "asap", "emergency", "blocker"}
)

_HIGH_PRIO_WORDS = frozenset(
    {"important", "priority", "soon", "required", "must"}
)


class TaskPriority(str, Enum):
    """Priority levels for subtasks."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class SubTask:
    """A subtask in a decomposition.

    Attributes:
        id: Unique subtask identifier
        description: Human-readable description
        priority: Execution priority
        dependencies: IDs of subtasks that must complete first
        estimated_effort: Relative effort units (1.0 = baseline)
        assigned_to: Optional node ID for mesh distribution
    """

    id: str
    description: str
    priority: TaskPriority = TaskPriority.MEDIUM
    dependencies: List[str] = field(default_factory=list)
    estimated_effort: float = 1.0
    assigned_to: Optional[str] = None


@dataclass
class TaskPlan:
    """A complete task decomposition.

    Attributes:
        id: Unique plan identifier
        description: The original request
        subtasks: Ordered list of subtasks
        parallel_groups: Groups of subtask IDs that can execute concurrently
        total_effort: Sum of all subtask efforts
    """

    id: str
    description: str
    subtasks: List[SubTask] = field(default_factory=list)
    parallel_groups: List[List[str]] = field(default_factory=list)
    total_effort: float = 0.0


class TaskPlanner:
    """Decompose complex requests into executable plans.

    Takes a natural-language request, breaks it into subtasks,
    identifies dependencies, and groups parallelizable work.

    Decomposition rules:
    - Sequential keywords ("then", "next", "finally"…) create ordered dependencies.
    - Parallel keywords ("and also", "simultaneously"…) create independent tasks.
    - Plain "and" splits into parallel tasks when no sequential markers are present.
    - A single-sentence request creates one subtask.

    Args:
        graph_client: Colony graph client for context retrieval
    """

    def __init__(self, graph_client: Any) -> None:
        self.graph = graph_client

    async def plan(self, request: str, context: Optional[Dict[str, Any]] = None) -> TaskPlan:
        """Create execution plan for a request.

        Args:
            request: Natural-language task description
            context: Optional context for better decomposition

        Returns:
            TaskPlan with subtasks and parallel groups
        """
        subtasks = await self._decompose(request)

        await self._identify_dependencies(subtasks)

        parallel_groups = self._find_parallel_groups(subtasks)

        total_effort = sum(s.estimated_effort for s in subtasks)

        plan = TaskPlan(
            id=f"plan-{hash(request) % 10000:04d}",
            description=request,
            subtasks=subtasks,
            parallel_groups=parallel_groups,
            total_effort=total_effort,
        )

        logger.debug(
            "Created plan %s: %d subtasks, %d parallel groups, effort=%.1f",
            plan.id,
            len(subtasks),
            len(parallel_groups),
            total_effort,
        )
        return plan

    async def _decompose(self, request: str) -> List[SubTask]:
        """Decompose request into subtasks using keyword rules.

        Sequential markers create ordered dependencies; parallel markers
        and plain conjunctions create independent groups.
        """
        # Check for sequential ordering markers first
        seq_parts = _SEQUENTIAL_PATTERN.split(request)
        if len(seq_parts) > 1:
            return self._build_subtasks(seq_parts, sequential=True)

        # Check for explicit parallel markers
        par_parts = _PARALLEL_PATTERN.split(request)
        if len(par_parts) > 1:
            return self._build_subtasks(par_parts, sequential=False)

        # Fall back to splitting on plain "and" (parallel by default)
        and_parts = re.split(r"\s+and\s+", request, flags=re.IGNORECASE)
        if len(and_parts) > 1:
            return self._build_subtasks(and_parts, sequential=False)

        # Single task
        return [self._make_subtask("subtask-1", request.strip() or request)]

    def _build_subtasks(self, parts: List[str], *, sequential: bool) -> List[SubTask]:
        """Build subtask list from text parts.

        Args:
            parts: Text fragments representing individual tasks
            sequential: If True, each task depends on the previous one
        """
        subtasks: List[SubTask] = []
        for i, part in enumerate(parts):
            part = part.strip()
            if not part:
                continue
            st = self._make_subtask(f"subtask-{len(subtasks) + 1}", part)
            if sequential and subtasks:
                st.dependencies.append(subtasks[-1].id)
            subtasks.append(st)
        return subtasks or [self._make_subtask("subtask-1", "Analyse request")]

    def _make_subtask(self, task_id: str, description: str) -> SubTask:
        """Construct a SubTask, inferring priority and effort from keywords."""
        lower = description.lower()

        # Priority
        if any(w in lower for w in _CRITICAL_WORDS):
            priority = TaskPriority.CRITICAL
        elif any(w in lower for w in _HIGH_PRIO_WORDS):
            priority = TaskPriority.HIGH
        else:
            priority = TaskPriority.MEDIUM

        # Effort
        if any(w in lower for w in _HIGH_EFFORT_WORDS):
            effort = 2.0
        elif any(w in lower for w in _LOW_EFFORT_WORDS):
            effort = 0.5
        else:
            effort = 1.0

        return SubTask(
            id=task_id,
            description=description,
            priority=priority,
            estimated_effort=effort,
        )

    async def _identify_dependencies(self, subtasks: List[SubTask]) -> None:
        """Heuristic dependency analysis between subtasks.

        ``_decompose`` creates a linear dependency chain by default.
        This method clears dependencies between pairs of tasks whose
        descriptions suggest they are genuinely independent — i.e. no
        explicit ordering language and both match independence patterns.

        Sequential tasks already have dependencies set by ``_decompose``.
        An LLM-assisted Phase 2 can be wired in future when a router is
        available.
        """
        if len(subtasks) < 2:
            return

        for i, task in enumerate(subtasks):
            if i == 0:
                continue

            desc_lower = task.description.lower()
            has_explicit_dep = any(p.search(desc_lower) for p in _DEPENDENCY_PATTERNS)
            if has_explicit_dep:
                continue  # Keep the sequential dependency

            is_independent_type = any(p.search(desc_lower) for p in _INDEPENDENT_TASK_PATTERNS)
            if not is_independent_type:
                continue

            prev = subtasks[i - 1]
            prev_lower = prev.description.lower()
            prev_is_independent = any(p.search(prev_lower) for p in _INDEPENDENT_TASK_PATTERNS)
            if prev_is_independent:
                # Both tasks are independently typed — allow parallel execution
                task.dependencies = [
                    d for d in task.dependencies if d != prev.id
                ]
                logger.debug(
                    "TaskPlanner: cleared dependency %s → %s "
                    "(both tasks appear independent)",
                    prev.id,
                    task.id,
                )

    def _find_parallel_groups(self, subtasks: List[SubTask]) -> List[List[str]]:
        """Find subtasks that can run in parallel using topological levels.

        Performs a level-order topological sort: tasks at level 0 have no
        dependencies, tasks at level 1 depend only on level-0 tasks, etc.
        Tasks within the same level can execute concurrently.

        Args:
            subtasks: All subtasks in the plan

        Returns:
            List of groups (each group is a list of subtask IDs that can
            run concurrently).
        """
        if not subtasks:
            return []

        remaining: Dict[str, SubTask] = {s.id: s for s in subtasks}
        completed: Set[str] = set()
        groups: List[List[str]] = []

        while remaining:
            ready = [
                sid
                for sid, s in remaining.items()
                if all(dep in completed for dep in s.dependencies)
            ]

            if not ready:
                # Cycle or unresolvable — dump remainder into one group
                groups.append(list(remaining.keys()))
                break

            groups.append(ready)
            for sid in ready:
                completed.add(sid)
                del remaining[sid]

        return groups
