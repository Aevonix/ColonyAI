"""Action registry — named, allow-listed capabilities with risk tiers (v0.16.0).

``action_hint`` on an agent-actionable initiative is a *named capability*
in this registry, never a raw command string. Colony builds initiatives
from graph data that can include untrusted content (contact messages,
repo READMEs, webhook payloads); a free-form ``{"tool": ..., "command":
<string>}`` payload would be a direct injection-to-execution path. The
registry is the allow-list: nothing executes that isn't registered.

Risk tiers and approval holders:

- ``read_only`` — auto-execute, no approval needed.
- ``mutating`` — requires HUMAN OWNER approval. The agent cannot approve
  its own mutations; the same actor on both sides of a gate is a log
  line, not a boundary.
- ``outbound`` — sends something outside the system; requires HUMAN
  OWNER approval.

``COLONY_AGENT_AUTO_APPROVE=true`` collapses the gate for trusted
deployments (default false).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class RiskTier(str, Enum):
    """How much damage an action can do if it runs at the wrong time."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    OUTBOUND = "outbound"


@dataclass(frozen=True)
class ActionSpec:
    """A registered capability the runtime knows how to run.

    ``command`` is a template the executing agent renders with values
    from the initiative context ($ID, $PR, etc.). It documents intent;
    the executing worker maps the action name to its own toolset and is
    free to implement it differently. What matters for safety is the
    name (allow-list membership) and the risk tier (approval gate).
    """

    name: str
    tool: str
    command: str
    risk: RiskTier
    description: str = ""
    initiative_type: Optional[str] = None
    required_params: List[str] = field(default_factory=list)

    @property
    def auto_executable(self) -> bool:
        return self.risk == RiskTier.READ_ONLY


_SPECS: List[ActionSpec] = [
    # --- AGENT_ACTION (v0.13.0 hints, now registered) ---
    ActionSpec(
        name="agent_check_repo_status",
        tool="terminal",
        command="git -C ~/colony-work status --short",
        risk=RiskTier.READ_ONLY,
        description="Check working repos for uncommitted changes",
        initiative_type="agent_action",
    ),
    ActionSpec(
        name="agent_investigate_subsystem",
        tool="terminal",
        command="colony doctor --subsystem $ID",
        risk=RiskTier.READ_ONLY,
        description="Investigate a degraded Colony subsystem",
        initiative_type="agent_action",
        required_params=["ID"],
    ),
    ActionSpec(
        name="agent_check_ci",
        tool="terminal",
        command="gh run list --limit 5 --json conclusion,headBranch",
        risk=RiskTier.READ_ONLY,
        description="Check recent CI runs",
        initiative_type="agent_action",
    ),
    ActionSpec(
        name="agent_cleanup_orphans",
        tool="terminal",
        command="colony graph cleanup-orphans $ID",
        risk=RiskTier.MUTATING,
        description="Delete orphan nodes from the knowledge graph",
        initiative_type="agent_action",
        required_params=["ID"],
    ),
    # Legacy destructive hints (loop.py v0.13.0 DESTRUCTIVE_HINTS) —
    # registered as mutating so the approval gate keeps holding.
    ActionSpec("agent_git_push", "terminal", "git push", RiskTier.MUTATING,
               "Push commits to a remote", "agent_action"),
    ActionSpec("agent_git_commit", "terminal", "git commit", RiskTier.MUTATING,
               "Create a commit", "agent_action"),
    ActionSpec("agent_service_restart", "terminal", "systemctl restart $SERVICE",
               RiskTier.MUTATING, "Restart a system service", "agent_action",
               ["SERVICE"]),
    ActionSpec("agent_file_delete", "terminal", "rm $PATH", RiskTier.MUTATING,
               "Delete a file", "agent_action", ["PATH"]),
    ActionSpec("agent_deploy", "terminal", "deploy $TARGET", RiskTier.MUTATING,
               "Deploy to an environment", "agent_action", ["TARGET"]),

    # --- TASK ---
    ActionSpec("task_list_open", "terminal",
               "gh issue list --state open --limit 20",
               RiskTier.READ_ONLY, "List open issues", "task"),
    ActionSpec("task_check_status", "terminal",
               "gh issue view $ID --json state",
               RiskTier.READ_ONLY, "Check one issue's state", "task", ["ID"]),
    ActionSpec("task_update_status", "terminal",
               "gh issue close $ID",
               RiskTier.MUTATING, "Close or reopen an issue", "task", ["ID"]),

    # --- CODING ---
    ActionSpec("coding_list_prs", "terminal",
               "gh pr list --state open --limit 20",
               RiskTier.READ_ONLY, "List open PRs", "coding"),
    ActionSpec("coding_check_ci", "terminal",
               "gh pr checks $PR --json conclusion",
               RiskTier.READ_ONLY, "Check CI status for a PR", "coding", ["PR"]),
    ActionSpec("coding_review_pr", "terminal",
               "gh pr review $PR --approve",
               RiskTier.MUTATING, "Submit a PR review", "coding", ["PR"]),
    ActionSpec("coding_merge_pr", "terminal",
               "gh pr merge $PR --squash",
               RiskTier.MUTATING, "Merge a PR", "coding", ["PR"]),
    ActionSpec("coding_comment_on_pr", "terminal",
               "gh pr comment $PR --body \"$MSG\"",
               RiskTier.OUTBOUND, "Comment on a PR", "coding", ["PR", "MSG"]),

    # --- PROJECT ---
    ActionSpec("project_list_milestones", "terminal",
               "gh api repos/{owner}/{repo}/milestones",
               RiskTier.READ_ONLY, "List milestones", "project"),
    ActionSpec("project_check_progress", "terminal",
               "gh api 'repos/{owner}/{repo}/issues?milestone=$ID'",
               RiskTier.READ_ONLY, "Check milestone progress", "project", ["ID"]),
    ActionSpec("project_update_milestone", "terminal",
               "gh api repos/{owner}/{repo}/milestones/$ID --method PATCH",
               RiskTier.MUTATING, "Update a milestone", "project", ["ID"]),

    # --- SYSTEM ---
    ActionSpec("system_check_health", "terminal",
               "curl http://localhost:7777/health",
               RiskTier.READ_ONLY, "Check sidecar health", "system"),
    ActionSpec("system_check_alerts", "terminal",
               "journalctl -p err -n 20 --no-pager",
               RiskTier.READ_ONLY, "Check recent error logs", "system"),
    ActionSpec("system_restart_service", "terminal",
               "systemctl restart $SERVICE",
               RiskTier.MUTATING, "Restart a service", "system", ["SERVICE"]),
    ActionSpec("system_send_alert", "terminal",
               "curl -X POST $WEBHOOK_URL -d \"$MSG\"",
               RiskTier.OUTBOUND, "Send an alert webhook", "system",
               ["WEBHOOK_URL", "MSG"]),

    # --- CALENDAR ---
    ActionSpec("calendar_list_events", "terminal",
               "gcal list --upcoming 24h",
               RiskTier.READ_ONLY, "List upcoming events", "calendar"),
    ActionSpec("calendar_prepare_meeting", "terminal",
               "gcal prepare $EVENT_ID",
               RiskTier.READ_ONLY, "Gather prep material for a meeting",
               "calendar", ["EVENT_ID"]),
    ActionSpec("calendar_send_reminder", "terminal",
               "curl -X POST $WEBHOOK_URL -d \"$MSG\"",
               RiskTier.OUTBOUND, "Send a meeting reminder", "calendar",
               ["WEBHOOK_URL", "MSG"]),

    # --- COMMITMENT ---
    ActionSpec("commitment_list_open", "terminal",
               "colony commitments list --status pending",
               RiskTier.READ_ONLY, "List open commitments", "commitment"),
    ActionSpec("commitment_check_deadline", "terminal",
               "colony commitments check $ID",
               RiskTier.READ_ONLY, "Check a commitment's deadline",
               "commitment", ["ID"]),
    ActionSpec("commitment_mark_complete", "terminal",
               "colony commitments complete $ID",
               RiskTier.MUTATING, "Mark a commitment fulfilled",
               "commitment", ["ID"]),

    # --- RESEARCH ---
    ActionSpec("research_check_paper", "terminal",
               "curl https://arxiv.org/abs/$ID",
               RiskTier.READ_ONLY, "Check a paper's status", "research", ["ID"]),
    ActionSpec("research_check_weights", "terminal",
               "curl https://huggingface.co/api/models/$MODEL",
               RiskTier.READ_ONLY, "Check if model weights are published",
               "research", ["MODEL"]),
    ActionSpec("research_download_weights", "terminal",
               "huggingface-cli download $MODEL",
               RiskTier.MUTATING, "Download model weights", "research",
               ["MODEL"]),
]

ACTION_REGISTRY: Dict[str, ActionSpec] = {spec.name: spec for spec in _SPECS}


def get_action(name: Optional[str]) -> Optional[ActionSpec]:
    """Look up a registered action. None for unregistered names."""
    if not name:
        return None
    return ACTION_REGISTRY.get(name)


def is_registered(name: Optional[str]) -> bool:
    return get_action(name) is not None


def requires_owner_approval(name: Optional[str]) -> bool:
    """True when the action must not auto-run.

    Unregistered actions also return True — fail closed — but callers
    should refuse to queue them at all (see ``classify_agent_action``).
    """
    spec = get_action(name)
    if spec is None:
        return True
    return spec.risk != RiskTier.READ_ONLY


def classify_agent_action(action_hint: Optional[str]) -> Dict[str, object]:
    """Single gating decision for the dispatch path.

    Returns a dict with:
    - ``registered``: the hint names a known capability
    - ``executable``: safe to post to the task queue at all
    - ``requires_approval``: must block on human-owner approval first
    - ``risk``: the tier string, or None when unregistered
    """
    spec = get_action(action_hint)
    if spec is None:
        return {
            "registered": False,
            "executable": False,
            "requires_approval": True,
            "risk": None,
        }
    return {
        "registered": True,
        "executable": True,
        "requires_approval": spec.risk != RiskTier.READ_ONLY,
        "risk": spec.risk.value,
    }


def actions_for_type(initiative_type: str) -> List[ActionSpec]:
    """All registered actions for one initiative type."""
    return [s for s in ACTION_REGISTRY.values() if s.initiative_type == initiative_type]
