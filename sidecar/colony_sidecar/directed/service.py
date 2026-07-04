"""DirectedActionService -- owner directive -> gated, delegated, audited action.

Pipeline (option A: delegation only, Colony never mutates anything itself):

  intake  -> deterministic ScopedTask (intake.py)
  gate 1  -> DirectiveGuard boundary check FIRST (a standing "leave X alone"
             refuses intake with the citation)
  gate 2  -> approval tiering: read-only scopes auto-approve; any mutating
             scope requires owner approval (standing approvals honoured via
             the existing standing_approvals machinery)
  dispatch-> POST the ScopedTask contract to the EXISTING env-configured
             delegate endpoint (COLONY_DIRECTED_TASK_URL, falling back to the
             agent-bridge jobs webhook). Dry-run mode logs the exact would-be
             dispatch and sends nothing (COLONY_DIRECTED_MODE, default dry_run).
  report  -> the delegate POSTs a structured report back; audit_completion
             verifies it against the granted scope (mirror inspection when a
             read-only mirror of the target exists).
  outcome -> violations flagged loudly + recorded; success/violation/failure
             feeds the TypeFeedbackStore; an owner-facing report artifact is
             routed through the guarded reach-out path (held by delivery shadow).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from colony_sidecar.directed.models import ScopedTask, ScopedTaskStore
from colony_sidecar.directed.intake import scope_from_directive
from colony_sidecar.directed.audit import audit_completion

logger = logging.getLogger(__name__)


def directed_mode() -> str:
    m = os.environ.get("COLONY_DIRECTED_MODE", "dry_run").strip().lower()
    return m if m in ("off", "dry_run", "live") else "dry_run"


def _dispatch_url() -> str:
    return (os.environ.get("COLONY_DIRECTED_TASK_URL", "")
            or os.environ.get("COLONY_BRIDGE_JOBS_WEBHOOK_URL", "")
            or os.environ.get("COLONY_JOBS_WEBHOOK_URL", ""))


class DirectedActionService:
    def __init__(
        self,
        store: ScopedTaskStore,
        directive_manager: Any = None,
        mirrors: Any = None,           # RepoMirrorManager (audit + target resolution)
        feedback_store: Any = None,
        delivery_router: Any = None,   # awaitable (payload) -> bool, e.g. loop._route_reachout_delivery bound with delivery
    ) -> None:
        self.store = store
        self._directives = directive_manager
        self._mirrors = mirrors
        self._feedback = feedback_store
        self._deliver = delivery_router

    # -- known targets --------------------------------------------------
    def known_targets(self) -> List[Dict[str, str]]:
        """Owner-designated repos (mirrors) as intake-resolvable targets."""
        out: List[Dict[str, str]] = []
        if self._mirrors is not None:
            try:
                for name, info in self._mirrors.configured().items():
                    out.append({"kind": "repo", "name": name,
                                "aliases": info.get("aliases", ""),
                                "ref": info.get("url", "")})
            except Exception:
                pass
        return out

    # -- intake + gates --------------------------------------------------
    async def intake(self, directive_text: str,
                     source: str = "owner") -> ScopedTask:
        """Turn a directive into a gated ScopedTask (does NOT dispatch)."""
        task = scope_from_directive(directive_text, self.known_targets())

        # Gate 1: boundaries FIRST -- refuse with the citation.
        if self._directives is not None:
            try:
                from colony_sidecar.directives import Action
                verdict = self._directives.check(Action(
                    kind="directed_action",
                    text=task.searchable_text(),
                    target=",".join(t.get("name", "") for t in task.targets),
                    high_risk=True,
                ))
                if not verdict.allowed:
                    task.status = "refused"
                    task.refusal_reason = verdict.reason
                    self.store.save(task)
                    logger.warning("Directed intake REFUSED by boundary: %s", verdict.reason)
                    return task
            except Exception:
                logger.debug("directed boundary check failed (allowing)", exc_info=True)

        # Gate 2: approval tiering.
        if not task.mutating:
            task.approval = {"required": False, "reason": "read_only_auto"}
            task.status = "approved"
        else:
            standing = False
            try:
                from colony_sidecar.initiatives import standing_approvals
                standing = standing_approvals.is_approved(task.approval_key)
            except Exception:
                pass
            if standing:
                task.approval = {"required": True, "granted_by": "standing",
                                 "standing": True, "key": task.approval_key}
                task.status = "approved"
            else:
                task.approval = {"required": True, "standing": False,
                                 "key": task.approval_key}
                task.status = "awaiting_approval"
        self.store.save(task)
        return task

    def approve(self, task_id: str, approved_by: str = "owner",
                standing: bool = False) -> Optional[ScopedTask]:
        task = self.store.get(task_id)
        if task is None or task.status != "awaiting_approval":
            return task
        task.approval.update({"granted_by": approved_by, "standing": standing})
        task.status = "approved"
        if standing:
            try:
                from colony_sidecar.initiatives import standing_approvals
                standing_approvals.grant(task.approval_key, approved_by=approved_by)
            except Exception:
                pass
        self.store.save(task)
        return task

    # -- dispatch ---------------------------------------------------------
    async def dispatch(self, task_id: str) -> Dict[str, Any]:
        """Send an APPROVED ScopedTask to the delegate (or log it in dry-run)."""
        task = self.store.get(task_id)
        if task is None:
            return {"dispatched": False, "reason": "not_found"}
        if task.status != "approved":
            return {"dispatched": False, "reason": f"not_approved (status={task.status})"}
        if task.is_expired():
            task.status = "expired"; self.store.save(task)
            return {"dispatched": False, "reason": "expired"}

        mode = directed_mode()
        payload = {
            "type": "directed_task",
            "task": task.to_dict(),
            "contract": {
                "work_branch_prefix": task.limits.branch_prefix,
                "instructions": (
                    "Work ONLY within the attached scope. Create a dedicated "
                    f"branch under '{task.limits.branch_prefix}'. Never force-push, "
                    "never delete outside scope, respect the commit cap. On "
                    "completion POST a structured report (summary, operations, "
                    "files_touched, commits, branch) to report_url."
                ),
            },
            "report_url": f"/v1/host/directed/tasks/{task.id}/report",
        }
        if mode != "live":
            logger.info(
                "DRY-RUN directed dispatch %s -> %s | ops=%s targets=%s "
                "limits={branch_prefix:%s,max_commits:%d} mutating=%s",
                task.id, _dispatch_url() or "(no endpoint configured)",
                task.allowed_ops, [t.get("name") for t in task.targets],
                task.limits.branch_prefix, task.limits.max_commits, task.mutating,
            )
            task.status = "dispatched_dry"
            self.store.save(task)
            return {"dispatched": False, "dry_run": True, "payload": payload}

        url = _dispatch_url()
        if not url:
            return {"dispatched": False, "reason": "no_dispatch_endpoint"}
        try:
            import aiohttp
            headers = {"Content-Type": "application/json"}
            key = os.environ.get("COLONY_API_KEY", "")
            if key:
                headers["Authorization"] = f"Bearer {key}"
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=payload, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    ok = resp.status in (200, 202)
        except Exception as exc:
            logger.warning("directed dispatch failed: %s", exc)
            ok = False
        task.status = "dispatched" if ok else "failed"
        self.store.save(task)
        return {"dispatched": ok, "url": url}

    # -- report-back + audit ----------------------------------------------
    async def complete(self, task_id: str, report: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate report-back: audit vs scope, record outcome, notify owner."""
        task = self.store.get(task_id)
        if task is None:
            return {"ok": False, "reason": "not_found"}

        mirror_path = None
        if self._mirrors is not None and task.targets:
            try:
                mirror_path = self._mirrors.path_for(task.targets[0].get("name", ""))
            except Exception:
                mirror_path = None

        audit = audit_completion(task, report or {}, mirror_path=mirror_path)
        task.audit = audit
        verdict = audit["verdict"]
        task.status = "violated" if verdict == "violation" else "completed"
        self.store.save(task)

        # Outcome feedback.
        if self._feedback is not None:
            try:
                self._feedback.record(
                    "directed_action",
                    "actioned" if verdict == "clean" else "dismissed",
                )
            except Exception:
                pass

        # Owner-facing report through the guarded reach-out path (still held
        # by delivery shadow until go-live).
        await self._notify_owner(task, report or {}, audit)
        return {"ok": True, "verdict": verdict, "audit": audit}

    async def _notify_owner(self, task: ScopedTask, report: Dict[str, Any],
                            audit: Dict[str, Any]) -> None:
        if self._deliver is None:
            return
        try:
            from colony_sidecar.proposals import Proposal, proposal_to_payload
            verdict = audit.get("verdict", "unverified")
            if verdict == "violation":
                findings = "; ".join(
                    audit.get("report_audit", {}).get("findings", [])[:5])
                title = f"SCOPE VIOLATION on directed task: {task.objective[:60]}"
                finding = (f"The delegate exceeded the granted scope: {findings}. "
                           "I flagged and recorded it; nothing was accepted.")
                urgency = 0.95
            else:
                title = f"Directed task {verdict}: {task.objective[:60]}"
                finding = str(report.get("summary", ""))[:600] or "Task completed."
                urgency = 0.7
            prop = Proposal(
                title=title, finding=finding,
                why_it_helps="closes the loop on the work you directed",
                suggested_action=("Review the flagged changes" if verdict == "violation"
                                  else "Review the result; tell me any follow-ups"),
                source=task.id, initiative_type="proposal", confidence=urgency,
            )
            await self._deliver(proposal_to_payload(prop))
        except Exception:
            logger.debug("directed owner-notify failed", exc_info=True)
