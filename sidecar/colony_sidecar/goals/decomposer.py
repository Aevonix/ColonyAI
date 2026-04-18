"""GoalDecomposer — transform a Goal into an executable GoalDAG.

Strategies:
  TemplateDecomposition  — goal matches a known template; no LLM required
  FreeFormDecomposition  — novel goal; generates a sensible default DAG
  DelegationDecomposition — remote plan provided; wrap into GoalDAG

Critical path is computed using EFT (Earliest Finish Time) algorithm.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from colony_sidecar.goals.models import Goal, GoalDAG, GoalPriority, Subtask, SubtaskStatus

logger = logging.getLogger(__name__)

MAX_DEPTH = 5
MAX_SUBTASKS = 50

# Standard capability names
CAPABILITY_MAP: Dict[str, List[str]] = {
    "llm_inference":   ["inference"],
    "web_search":      ["network"],
    "document_write":  ["filesystem"],
    "code_execution":  ["sandbox"],
    "gpu_inference":   ["gpu", "cuda"],
    "apple_silicon":   ["apple_silicon"],
    "email_send":      ["email_gateway"],
    "calendar_access": ["calendar"],
}


class DecompositionStrategy(str, Enum):
    TEMPLATE    = "template"
    HYBRID      = "hybrid"
    FREE_FORM   = "free_form"
    DELEGATION  = "delegation"


class DecompositionError(ValueError):
    """Raised when decomposition produces an invalid DAG."""


@dataclass
class SubtaskSpec:
    """Specification for a subtask in a template."""
    title: str
    job_type: str = "custom"
    capabilities: List[str] = field(default_factory=list)
    depends_on_indices: List[int] = field(default_factory=list)   # 0-based index in template list
    estimated_hours: Optional[float] = None
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DecompositionTemplate:
    """A reusable decomposition pattern for known goal types."""
    name: str
    keywords: List[str]
    subtasks: List[SubtaskSpec]
    description: str = ""


# ── Built-in templates ─────────────────────────────────────────────────────────

_BUILTIN_TEMPLATES: List[DecompositionTemplate] = [
    DecompositionTemplate(
        name="research_topic",
        keywords=["research", "learn about", "find out", "investigate", "explore"],
        description="Research and summarize a topic",
        subtasks=[
            SubtaskSpec(title="Search for relevant sources", job_type="research",
                        capabilities=["network"], estimated_hours=0.5),
            SubtaskSpec(title="Read and extract key information", job_type="inference",
                        capabilities=["inference"], depends_on_indices=[0], estimated_hours=1.0),
            SubtaskSpec(title="Synthesize and summarize findings", job_type="synthesis",
                        capabilities=["inference"], depends_on_indices=[1], estimated_hours=0.5),
        ],
    ),
    DecompositionTemplate(
        name="write_document",
        keywords=["write", "draft", "create document", "prepare report", "compose"],
        description="Write or draft a document",
        subtasks=[
            SubtaskSpec(title="Outline document structure", job_type="inference",
                        capabilities=["inference"], estimated_hours=0.25),
            SubtaskSpec(title="Draft content sections", job_type="inference",
                        capabilities=["inference"], depends_on_indices=[0], estimated_hours=1.0),
            SubtaskSpec(title="Review and refine draft", job_type="inference",
                        capabilities=["inference"], depends_on_indices=[1], estimated_hours=0.5),
            SubtaskSpec(title="Save final document", job_type="data_processing",
                        capabilities=["filesystem"], depends_on_indices=[2], estimated_hours=0.1),
        ],
    ),
    DecompositionTemplate(
        name="send_email",
        keywords=["send email", "email to", "write email", "compose email"],
        description="Compose and send an email",
        subtasks=[
            SubtaskSpec(title="Draft email content", job_type="inference",
                        capabilities=["inference"], estimated_hours=0.25),
            SubtaskSpec(title="Review and address email", job_type="inference",
                        capabilities=["inference"], depends_on_indices=[0], estimated_hours=0.1),
            SubtaskSpec(title="Send email", job_type="custom",
                        capabilities=["email_gateway"], depends_on_indices=[1], estimated_hours=0.05),
        ],
    ),
    DecompositionTemplate(
        name="schedule_meeting",
        keywords=["schedule meeting", "book meeting", "set up call", "arrange call", "calendar"],
        description="Schedule a meeting or call",
        subtasks=[
            SubtaskSpec(title="Check calendar availability", job_type="custom",
                        capabilities=["calendar"], estimated_hours=0.1),
            SubtaskSpec(title="Propose meeting time", job_type="inference",
                        capabilities=["inference"], depends_on_indices=[0], estimated_hours=0.1),
            SubtaskSpec(title="Create calendar event", job_type="custom",
                        capabilities=["calendar"], depends_on_indices=[1], estimated_hours=0.1),
        ],
    ),
    DecompositionTemplate(
        name="code_task",
        keywords=["implement", "code", "build", "develop", "fix bug", "write code", "program"],
        description="Implement or fix a code task",
        subtasks=[
            SubtaskSpec(title="Analyse requirements and design approach", job_type="inference",
                        capabilities=["inference"], estimated_hours=0.5),
            SubtaskSpec(title="Implement solution", job_type="custom",
                        capabilities=["sandbox"], depends_on_indices=[0], estimated_hours=2.0),
            SubtaskSpec(title="Write tests", job_type="custom",
                        capabilities=["sandbox"], depends_on_indices=[1], estimated_hours=0.5),
            SubtaskSpec(title="Run tests and fix issues", job_type="custom",
                        capabilities=["sandbox"], depends_on_indices=[2], estimated_hours=0.5),
        ],
    ),
]


class GoalDecomposer:
    """Transforms a Goal into an executable GoalDAG."""

    MAX_DEPTH = MAX_DEPTH
    MAX_SUBTASKS = MAX_SUBTASKS

    def __init__(self) -> None:
        self._templates: List[DecompositionTemplate] = list(_BUILTIN_TEMPLATES)

    def register_template(self, template: DecompositionTemplate) -> None:
        self._templates.append(template)

    def get_template(self, goal_type: str) -> Optional[DecompositionTemplate]:
        """Find a template by name."""
        for t in self._templates:
            if t.name == goal_type:
                return t
        return None

    def decompose(self, goal: Goal) -> GoalDAG:
        """Decompose a goal into a subtask DAG.

        Steps:
        1. Select decomposition strategy
        2. Generate subtask list with dependencies
        3. Build adjacency structure
        4. Compute critical path
        5. Validate for cycles and orphaned nodes
        6. Return the GoalDAG

        Raises:
            DecompositionError: If the resulting DAG is invalid or exceeds limits.
        """
        strategy = self._select_strategy(goal)
        dag = self._build_dag(goal, strategy)

        # Validate
        errors = dag.validate()
        if errors:
            raise DecompositionError(f"Invalid DAG for goal {goal.goal_id}: {errors}")

        if len(dag.subtasks) > self.MAX_SUBTASKS:
            raise DecompositionError(
                f"Goal {goal.goal_id} decomposed into {len(dag.subtasks)} subtasks "
                f"(max {self.MAX_SUBTASKS})"
            )

        self._assign_depths(dag)

        if dag.max_depth > self.MAX_DEPTH:
            raise DecompositionError(
                f"Goal {goal.goal_id} DAG depth {dag.max_depth} exceeds max {self.MAX_DEPTH}"
            )

        critical = self._compute_critical_path(dag)
        dag.critical_path = critical
        self._mark_critical_path(dag)

        # Compute root/leaf sets
        all_ids = set(dag.subtasks.keys())
        has_deps = {dep for s in dag.subtasks.values() for dep in s.depends_on}
        dag.root_ids = [sid for sid in all_ids if not dag.subtasks[sid].depends_on]
        dag.leaf_ids = [sid for sid in all_ids if sid not in has_deps]

        return dag

    def _select_strategy(self, goal: Goal) -> DecompositionStrategy:
        """Select the decomposition strategy for a goal."""
        text = (goal.title + " " + goal.description).lower()
        for template in self._templates:
            if any(kw in text for kw in template.keywords):
                return DecompositionStrategy.TEMPLATE
        return DecompositionStrategy.FREE_FORM

    def _build_dag(self, goal: Goal, strategy: DecompositionStrategy) -> GoalDAG:
        """Build the GoalDAG using the selected strategy."""
        dag = GoalDAG(goal_id=goal.goal_id)

        if strategy == DecompositionStrategy.TEMPLATE:
            template = self._find_matching_template(goal)
            if template:
                return self._build_from_template(goal, dag, template)

        # Free-form: build a minimal 3-step DAG
        return self._build_free_form(goal, dag)

    def _find_matching_template(self, goal: Goal) -> Optional[DecompositionTemplate]:
        text = (goal.title + " " + goal.description).lower()
        best: Optional[DecompositionTemplate] = None
        best_count = 0
        for template in self._templates:
            count = sum(1 for kw in template.keywords if kw in text)
            if count > best_count:
                best_count = count
                best = template
        return best if best_count > 0 else None

    def _build_from_template(
        self, goal: Goal, dag: GoalDAG, template: DecompositionTemplate
    ) -> GoalDAG:
        """Instantiate subtasks from a template."""
        import uuid as _uuid
        subtask_ids: List[str] = []

        for spec in template.subtasks:
            sid = str(_uuid.uuid4())
            subtask_ids.append(sid)
            deps = [subtask_ids[i] for i in spec.depends_on_indices if i < len(subtask_ids)]
            st = Subtask(
                subtask_id=sid,
                goal_id=goal.goal_id,
                title=spec.title,
                job_type=spec.job_type,
                capabilities=list(spec.capabilities),
                depends_on=deps,
                estimated_hours=spec.estimated_hours,
                payload=dict(spec.payload),
            )
            dag.add_subtask(st)
        return dag

    def _build_free_form(self, goal: Goal, dag: GoalDAG) -> GoalDAG:
        """Build a generic 3-step DAG: analyse → execute → verify."""
        import uuid as _uuid

        s1_id = str(_uuid.uuid4())
        s2_id = str(_uuid.uuid4())
        s3_id = str(_uuid.uuid4())

        dag.add_subtask(Subtask(
            subtask_id=s1_id,
            goal_id=goal.goal_id,
            title=f"Analyse requirements: {goal.title}",
            job_type="inference",
            capabilities=["inference"],
            depends_on=[],
            estimated_hours=0.25,
        ))
        dag.add_subtask(Subtask(
            subtask_id=s2_id,
            goal_id=goal.goal_id,
            title=f"Execute: {goal.title}",
            job_type="custom",
            capabilities=[],
            depends_on=[s1_id],
            estimated_hours=1.0,
        ))
        dag.add_subtask(Subtask(
            subtask_id=s3_id,
            goal_id=goal.goal_id,
            title=f"Verify completion: {goal.title}",
            job_type="inference",
            capabilities=["inference"],
            depends_on=[s2_id],
            estimated_hours=0.25,
        ))
        return dag

    def _assign_depths(self, dag: GoalDAG) -> None:
        """Assign DAG depth to each subtask via BFS from root nodes."""
        # Root nodes have depth 0
        depth: Dict[str, int] = {}
        in_degree: Dict[str, int] = {sid: 0 for sid in dag.subtasks}

        # Build parent → children map
        children: Dict[str, List[str]] = {sid: [] for sid in dag.subtasks}
        for s in dag.subtasks.values():
            for dep in s.depends_on:
                if dep in dag.subtasks:
                    in_degree[s.subtask_id] = in_degree.get(s.subtask_id, 0) + 1
                    children[dep].append(s.subtask_id)

        queue = [sid for sid, d in in_degree.items() if d == 0]
        for sid in queue:
            depth[sid] = 0

        while queue:
            next_queue = []
            for sid in queue:
                for child in children[sid]:
                    new_depth = depth[sid] + 1
                    if child not in depth or depth[child] < new_depth:
                        depth[child] = new_depth
                    next_queue.append(child)
            queue = next_queue

        for sid, st in dag.subtasks.items():
            st.depth = depth.get(sid, 0)

        dag.max_depth = max(depth.values()) if depth else 0

    def _compute_critical_path(self, dag: GoalDAG) -> List[str]:
        """Compute the critical path using EFT (Earliest Finish Time) algorithm.

        Returns ordered list of subtask_ids from root to leaf along the
        longest estimated execution path.
        """
        if not dag.subtasks:
            return []

        # Build children map
        children: Dict[str, List[str]] = {sid: [] for sid in dag.subtasks}
        for s in dag.subtasks.values():
            for dep in s.depends_on:
                if dep in dag.subtasks:
                    children[dep].append(s.subtask_id)

        # EFT: earliest_finish[sid] = max(EFT of deps) + duration
        eft: Dict[str, float] = {}
        parent: Dict[str, Optional[str]] = {sid: None for sid in dag.subtasks}

        # Topological order (Kahn's algorithm)
        in_degree: Dict[str, int] = {sid: len(s.depends_on) for sid, s in dag.subtasks.items()}
        queue = [sid for sid, d in in_degree.items() if d == 0]
        topo: List[str] = []
        tmp_queue = list(queue)
        while tmp_queue:
            node = tmp_queue.pop(0)
            topo.append(node)
            for child in children[node]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    tmp_queue.append(child)

        for sid in topo:
            s = dag.subtasks[sid]
            dur = s.estimated_hours or 1.0
            if not s.depends_on:
                eft[sid] = dur
            else:
                max_parent_eft = max(eft.get(dep, 0.0) for dep in s.depends_on)
                eft[sid] = max_parent_eft + dur
                # Track which parent gives the critical path
                critical_dep = max(
                    (dep for dep in s.depends_on if dep in eft),
                    key=lambda d: eft.get(d, 0.0),
                    default=None,
                )
                parent[sid] = critical_dep

        if not eft:
            return []

        # Find the leaf with maximum EFT
        leaf_ids = set(dag.subtasks.keys()) - {dep for s in dag.subtasks.values() for dep in s.depends_on}
        if not leaf_ids:
            leaf_ids = set(dag.subtasks.keys())

        end_node = max(leaf_ids, key=lambda sid: eft.get(sid, 0.0))

        # Trace back from end_node to root
        path = []
        current: Optional[str] = end_node
        while current is not None:
            path.append(current)
            current = parent.get(current)

        return list(reversed(path))

    def _mark_critical_path(self, dag: GoalDAG) -> None:
        """Mark subtasks on the critical path with is_critical_path = True."""
        cp_set = set(dag.critical_path)
        for sid, st in dag.subtasks.items():
            st.is_critical_path = sid in cp_set

    def estimate_subtask_duration(self, subtask: Subtask) -> float:
        """Return estimated hours for a subtask (default if not set)."""
        if subtask.estimated_hours is not None:
            return subtask.estimated_hours
        # Default by job type
        defaults = {
            "inference": 0.5,
            "synthesis": 0.5,
            "research": 1.0,
            "data_processing": 0.5,
            "custom": 1.0,
        }
        return defaults.get(subtask.job_type, 1.0)
