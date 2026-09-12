"""Goal persistence: Projects (item 1, Phase A centerpiece).

Turns one-shot initiatives into sustained multi-tick pursuit: a Project is a
durable objective decomposed (one LLM planning pass, deterministically
re-validated) into dependency-ordered Steps, pursued by the ProjectEngine
from a dedicated autonomy phase. Every step is boundary-checked before
dispatch and routed through the sub-path that already gates that kind of
action (reasoning turn for analyze/research/internal, DirectedActionService
for directed, the guarded proposal path for deliver). Milestones and
completions surface as Proposals.

Modes (PACOMIND_PROJECTS_MODE, default shadow): shadow plans for real and logs
the exact intended step actions (boundary checks included) while taking no
outward or mutating action; live dispatches steps through their own gates
(which keep their own shadow/dry-run flags).
"""

from pacomind.projects.models import (
    ACTION_KINDS, Project, Step, projects_mode,
)
from pacomind.projects.store import ProjectStore
from pacomind.projects.planner import plan_project, validate_steps
from pacomind.projects.engine import ProjectEngine

__all__ = [
    "ACTION_KINDS", "Project", "Step", "ProjectStore", "ProjectEngine",
    "plan_project", "validate_steps", "projects_mode",
]
