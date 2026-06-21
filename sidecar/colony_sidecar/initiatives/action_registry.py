"""Action registry — named, allow-listed capabilities with risk tiers (v0.16.0).

``action_hint`` on an agent-actionable initiative is a *named capability*
in this registry, never a raw command string. Colony builds initiatives
from graph data that can include untrusted content (contact messages,
repo READMEs, webhook payloads); a free-form ``{"tool": ..., "command":
<string>}`` payload would be a direct injection-to-execution path. The
registry is the allow-list: nothing executes that isn't registered.

Risk tiers (v0.18.0 graduated policy):

- ``read_only`` — auto-execute, no approval needed.
- ``mutating`` — reversible platform/system writes that touch no person
  (close an issue, comment on a PR, webhook to own infra).
- ``outbound`` — reaches a PERSON outside the system (email, message,
  reminder). Reserved strictly for human recipients; platform writes
  belong in ``mutating``.
- ``destructive`` — deletes, overwrites, force-pushes, installs, spends,
  or otherwise cannot be cheaply undone.

Approval policy (``COLONY_APPROVAL_POLICY``, default ``strict``):

- ``strict`` (v0.17 behavior): mutating/outbound/destructive all require
  HUMAN OWNER approval. The agent cannot approve its own mutations; the
  same actor on both sides of a gate is a log line, not a boundary.
- ``graduated`` (v0.18.0, opt-in): mutating auto-executes with an audit
  tag; destructive requires owner approval; outbound requires approval
  UNLESS the resolved target is an authorized contact
  (``interaction_allowed=True`` — see ``approval_policy``). This encodes
  the owner's policy: no manual gate unless the action is potentially
  destructive or reaches an unauthorized individual.

Standing approvals (``standing_approvals``) override the gate for an
exact action name in BOTH modes — the owner has said "always allow this".

``COLONY_AGENT_AUTO_APPROVE=true`` collapses the gate for trusted
deployments (default false).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class RiskTier(str, Enum):
    """How much damage an action can do if it runs at the wrong time."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    OUTBOUND = "outbound"
    DESTRUCTIVE = "destructive"


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
    # v0.18.0: for OUTBOUND actions, the param that holds the recipient.
    # The graduated policy resolves it against the contact store to decide
    # whether the target is an authorized individual.
    target_param: Optional[str] = None

    @property
    def auto_executable(self) -> bool:
        return self.risk == RiskTier.READ_ONLY


# The agent-as-sensor loop (v0.16.0): when a domain's observations go
# stale, the autonomy loop posts one of these read-only sync jobs. The
# agent observes through its own Hermes connections and POSTs results
# to /v1/host/observations. Colony never calls external APIs itself.
OBSERVATION_SYNC_ACTIONS: Dict[str, str] = {
    "coding": "agent_sync_coding",
    "task": "agent_sync_task",
    "calendar": "agent_sync_calendar",
    "research": "agent_sync_research",
    "project": "agent_sync_project",
    "system": "agent_sync_system",
}

_SPECS: List[ActionSpec] = [
    # --- Observation sync (v0.16.0, agent-as-sensor) ---
    ActionSpec(
        name="agent_sync_coding",
        tool="github",
        command="gh pr list --state open --json number,title,url,isDraft,reviewDecision,statusCheckRollup",
        risk=RiskTier.READ_ONLY,
        description="Observe open PRs + CI status; report to /v1/host/observations (domain=coding)",
        initiative_type="coding",
    ),
    ActionSpec(
        name="agent_sync_task",
        tool="github",
        command="gh issue list --state open --json number,title,url,assignees,updatedAt",
        risk=RiskTier.READ_ONLY,
        description="Observe open issues/tasks; report to /v1/host/observations (domain=task)",
        initiative_type="task",
    ),
    ActionSpec(
        name="agent_sync_calendar",
        tool="calendar",
        command="list upcoming events for the next 48h",
        risk=RiskTier.READ_ONLY,
        description="Observe upcoming events; report to /v1/host/observations (domain=calendar)",
        initiative_type="calendar",
    ),
    ActionSpec(
        name="agent_sync_research",
        tool="web",
        command="check tracked papers/models for status changes",
        risk=RiskTier.READ_ONLY,
        description="Observe tracked research items; report to /v1/host/observations (domain=research)",
        initiative_type="research",
    ),
    ActionSpec(
        name="agent_sync_project",
        tool="github",
        command="gh api repos/{owner}/{repo}/milestones",
        risk=RiskTier.READ_ONLY,
        description="Observe milestones/boards; report to /v1/host/observations (domain=project)",
        initiative_type="project",
    ),
    ActionSpec(
        name="agent_sync_system",
        tool="terminal",
        command="check service health/latency/error rates",
        risk=RiskTier.READ_ONLY,
        description="Observe infrastructure health; report to /v1/host/observations (domain=system)",
        initiative_type="system",
    ),

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
        risk=RiskTier.DESTRUCTIVE,  # deletes graph nodes
        description="Delete orphan nodes from the knowledge graph",
        initiative_type="agent_action",
        required_params=["ID"],
    ),
    # Legacy destructive hints (loop.py v0.13.0 DESTRUCTIVE_HINTS).
    # v0.18.0 audit: delete/overwrite/restart stay DESTRUCTIVE so the
    # graduated policy keeps blocking them; plain commit/push (non-force)
    # are reversible MUTATING writes.
    ActionSpec("agent_git_push", "terminal", "git push", RiskTier.MUTATING,
               "Push commits to a remote", "agent_action"),
    ActionSpec("agent_git_commit", "terminal", "git commit", RiskTier.MUTATING,
               "Create a commit", "agent_action"),
    ActionSpec("agent_service_restart", "terminal", "systemctl restart $SERVICE",
               RiskTier.DESTRUCTIVE, "Restart a system service", "agent_action",
               ["SERVICE"]),
    ActionSpec("agent_file_delete", "terminal", "rm $PATH", RiskTier.DESTRUCTIVE,
               "Delete a file", "agent_action", ["PATH"]),
    ActionSpec("agent_deploy", "terminal", "deploy $TARGET", RiskTier.DESTRUCTIVE,
               "Deploy to an environment (overwrites the running version)",
               "agent_action", ["TARGET"]),
    # Deliver an OWED message/result to a person — the immediate-deliverable half of the
    # per-turn introspection reflex (e.g. "text me the findings"). OUTBOUND because it reaches
    # a person, with the recipient named so the graduated policy auto-passes when it resolves
    # to an authorized contact (the person who asked for it). The executing agent renders this
    # with its own send tool on whatever channel the contact uses.
    ActionSpec("agent_deliver_message", "messaging", "send to $RECIPIENT: \"$MSG\"",
               RiskTier.OUTBOUND, "Deliver an owed message/result to a person",
               "agent_action", ["RECIPIENT", "MSG"], target_param="RECIPIENT"),

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
               RiskTier.DESTRUCTIVE, "Merge a PR (rewrites the target branch)",
               "coding", ["PR"]),
    # Platform write — a PR comment doesn't message an individual.
    ActionSpec("coding_comment_on_pr", "terminal",
               "gh pr comment $PR --body \"$MSG\"",
               RiskTier.MUTATING, "Comment on a PR", "coding", ["PR", "MSG"]),

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
               RiskTier.DESTRUCTIVE, "Restart a service", "system", ["SERVICE"]),
    # Webhook to own infrastructure — a platform write, not a person.
    ActionSpec("system_send_alert", "terminal",
               "curl -X POST $WEBHOOK_URL -d \"$MSG\"",
               RiskTier.MUTATING, "Send an alert webhook (own infra)", "system",
               ["WEBHOOK_URL", "MSG"]),

    # --- CALENDAR ---
    ActionSpec("calendar_list_events", "terminal",
               "gcal list --upcoming 24h",
               RiskTier.READ_ONLY, "List upcoming events", "calendar"),
    ActionSpec("calendar_prepare_meeting", "terminal",
               "gcal prepare $EVENT_ID",
               RiskTier.READ_ONLY, "Gather prep material for a meeting",
               "calendar", ["EVENT_ID"]),
    # Reaches a person (the attendee) — OUTBOUND, with the recipient
    # named so the graduated policy can resolve it against contacts.
    ActionSpec("calendar_send_reminder", "terminal",
               "curl -X POST $WEBHOOK_URL -d \"$MSG\"",
               RiskTier.OUTBOUND, "Send a meeting reminder", "calendar",
               ["WEBHOOK_URL", "MSG", "RECIPIENT"],
               target_param="RECIPIENT"),

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


APPROVAL_POLICY_STRICT = "strict"
APPROVAL_POLICY_GRADUATED = "graduated"


def get_approval_policy() -> str:
    """Resolve the approval policy mode from the environment.

    ``COLONY_APPROVAL_POLICY=graduated`` opts a deployment into the
    graduated gate; anything else (including unset/unknown values) is
    ``strict`` — fail closed on typos.
    """
    mode = os.environ.get("COLONY_APPROVAL_POLICY", APPROVAL_POLICY_STRICT)
    mode = (mode or "").strip().lower()
    if mode == APPROVAL_POLICY_GRADUATED:
        return APPROVAL_POLICY_GRADUATED
    return APPROVAL_POLICY_STRICT


def classify_agent_action(
    action_hint: Optional[str],
    params: Optional[Dict[str, object]] = None,
    policy: Optional[str] = None,
    target_authorized: Optional[bool] = None,
) -> Dict[str, object]:
    """Single gating decision for the dispatch path.

    Args:
        action_hint: the named capability to classify.
        params: the job params/context (carried for callers; target
            resolution itself is async — see
            ``approval_policy.is_authorized_target``).
        policy: ``strict`` or ``graduated``; defaults to
            ``get_approval_policy()`` (env, default strict).
        target_authorized: for OUTBOUND actions under the graduated
            policy, the verdict of ``is_authorized_target`` — True means
            the recipient resolved to a contact with
            ``interaction_allowed=True``. None/False keep the gate.

    Returns a dict with:
    - ``registered``: the hint names a known capability
    - ``executable``: safe to post to the task queue at all
    - ``requires_approval``: must block on human-owner approval first
    - ``risk``: the tier string, or None when unregistered
    - ``reason``: why the gate decision was made
    """
    spec = get_action(action_hint)
    if spec is None:
        return {
            "registered": False,
            "executable": False,
            "requires_approval": True,
            "risk": None,
            "reason": "unregistered_action",
        }

    mode = policy if policy in (APPROVAL_POLICY_STRICT, APPROVAL_POLICY_GRADUATED) \
        else get_approval_policy()

    # Standing approvals — the owner has said "always allow this exact
    # action". Overrides the gate in BOTH modes. Best-effort: a broken
    # approvals file must not break classification (gate stays closed).
    standing = False
    try:
        from colony_sidecar.initiatives import standing_approvals
        standing = standing_approvals.is_approved(spec.name)
    except Exception:
        standing = False

    if standing:
        requires_approval, reason = False, "standing_approval"
    elif spec.risk == RiskTier.READ_ONLY:
        requires_approval, reason = False, "read_only_auto"
    elif mode == APPROVAL_POLICY_GRADUATED:
        if spec.risk == RiskTier.MUTATING:
            requires_approval, reason = False, "graduated_auto_mutating"
        elif spec.risk == RiskTier.DESTRUCTIVE:
            requires_approval, reason = True, "destructive_requires_owner"
        else:  # OUTBOUND — gated unless the target is an authorized contact
            if target_authorized is True:
                requires_approval, reason = False, "outbound_authorized_contact"
            else:
                requires_approval, reason = True, "outbound_target_unverified"
    else:  # strict — v0.17 behavior: everything non-read-only is gated
        requires_approval, reason = True, "strict_policy_gate"

    return {
        "registered": True,
        "executable": True,
        "requires_approval": requires_approval,
        "risk": spec.risk.value,
        "reason": reason,
    }


def actions_for_type(initiative_type: str) -> List[ActionSpec]:
    """All registered actions for one initiative type."""
    return [s for s in ACTION_REGISTRY.values() if s.initiative_type == initiative_type]
