"""ProjectEngine -- sustained multi-tick pursuit of durable objectives.

Pursued from the autonomy loop's ``_phase_projects`` (after
``_phase_execute``). Each tick: adopt qualifying project-type initiatives,
plan any project still in ``planning`` (one LLM pass, deterministically
validated), then advance due active projects by one ready step each.

Safety posture:
- Every step is boundary-checked (DirectiveGuard) before dispatch; a blocked
  step blocks the whole project (visible, never silent).
- Step dispatch routes through the sub-path that already gates that action
  kind: reasoning turn (internal tools) for analyze/research/internal,
  DirectedActionService (approval tiering + dry_run) for directed, the
  guarded proposal path for deliver. This engine adds NO new outbound or
  mutating primitive of its own; it is orchestration over existing gates.
- COLONY_PROJECTS_MODE=shadow (default): plans for real, logs the exact
  intended step action with its boundary verdict, simulates advancement, and
  stores milestone proposals with status "shadow" WITHOUT routing them to
  delivery. Nothing leaves the machine.
- Uses the self-model (item 4) to defer pursuit under load, and skills memory
  (item 3) to inform planning and distill procedures from completions.

For step EXECUTION beyond the sidecar, deployments point the directed
pipeline's env-configured delegate endpoint at their host framework's job
surface (e.g. a kanban/runs API); this engine deliberately reuses that seam
instead of growing a private execution runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from colony_sidecar.projects.models import (
    Project, Step, projects_max_replans, projects_mode, projects_review_secs,
)
from colony_sidecar.projects.store import ProjectStore

logger = logging.getLogger(__name__)

# Step execution composes through the shared cognition charter (role
# "executor"); this block scopes the turn to ONE project step.
_STEP_SCOPE_CONTEXT = """\
You are executing ONE STEP of a long-running project. Complete THIS STEP
ONLY, then summarize the outcome in 2-4 sentences; later steps are separate
work sessions. If the step cannot be completed, say precisely what is
missing."""

_MAX_TOOL_ROUNDS = 4


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


class ProjectEngine:
    def __init__(
        self,
        store: ProjectStore,
        *,
        directive_manager: Any = None,
        llm_router: Any = None,          # planning + distillation passes
        reasoning_loop: Any = None,      # analyze/research/internal steps
        tool_executor: Any = None,
        directed_service: Any = None,    # directed steps
        proposal_store: Any = None,
        feedback_store: Any = None,
        self_model: Any = None,
        skill_store: Any = None,
        delivery_router: Any = None,     # async callable(payload) -> bool
        initiative_store: Any = None,    # adoption of project-type initiatives
    ) -> None:
        self.store = store
        self._directives = directive_manager
        self._router = llm_router
        self._reasoning = reasoning_loop
        self._tools = tool_executor
        self._directed = directed_service
        self._proposals = proposal_store
        self._feedback = feedback_store
        self._self_model = self_model
        self._skills = skill_store
        self._deliver = delivery_router
        self._initiatives = initiative_store

    # ------------------------------------------------------------------
    # Creation / adoption / abandonment
    # ------------------------------------------------------------------

    def create_project(self, objective: str, *, title: str = "",
                       source: str = "owner",
                       entity_ids: Optional[List[str]] = None,
                       ) -> Tuple[Optional[Project], str]:
        """Boundary-gated project creation (planning only; steps gate at
        dispatch). Returns (project, reason)."""
        objective = (objective or "").strip()
        if not objective:
            return None, "objective_required"
        if self._directives is not None:
            try:
                from colony_sidecar.directives import Action
                verdict = self._directives.check(Action(
                    kind="project", text=objective, target=title or objective,
                    high_risk=True))
                if not verdict.allowed:
                    logger.warning("Project creation REFUSED by boundary: %s",
                                   verdict.reason)
                    return None, verdict.reason
            except Exception:
                logger.debug("project boundary check failed (allowing)",
                             exc_info=True)
        title = (title or objective.split(".")[0]).strip()[:120]
        project = Project(title=title, objective=objective, source=source,
                          entity_ids=list(entity_ids or []))
        self.store.save_project(project)
        logger.info("Project created: %s %r (source=%s, mode=%s)",
                    project.id, title, source, projects_mode())
        return project, "ok"

    def abandon(self, project_id: str, reason: str = "owner_request",
                ) -> Optional[Project]:
        project = self.store.get_project(project_id)
        if project is None or project.status in ("completed", "abandoned"):
            return project
        project.status = "abandoned"
        project.reason = reason
        self.store.save_project(project)
        self._record_outcome("failure")
        if self._feedback is not None:
            try:
                self._feedback.record("project", "dismissed")
            except Exception:
                pass
        logger.info("Project %s abandoned: %s", project_id, reason)
        return project

    def project_status(self, project_id: str) -> Optional[Dict[str, Any]]:
        project = self.store.get_project(project_id)
        if project is None:
            return None
        steps = self.store.steps_for(project_id)
        return {
            "project": project.to_row(),
            "steps": [s.to_row() for s in steps],
            "done": sum(1 for s in steps if s.status == "done"),
            "total": len(steps),
        }

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _effective_mode(self) -> str:
        """Env mode, graduated by the trust engine (Amendment 1.2).

        "off" and "live" are owner overrides. "shadow" is the calibration
        stage: once the "project" trust domain graduates (clean calibration
        runs), pursuit goes live THROUGH the sub-gates, which carry their own
        ask/approval semantics for anything outbound or mutating.
        """
        mode = projects_mode()
        if mode in ("off", "live"):
            return mode
        trust = getattr(self._self_model, "trust", None)
        if trust is None:
            return mode
        try:
            stage = trust.stage("project", default="shadow")
        except Exception:
            return mode
        return "shadow" if stage == "shadow" else "live"

    async def tick(self) -> Dict[str, Any]:
        mode = self._effective_mode()
        report: Dict[str, Any] = {"mode": mode, "adopted": 0, "planned": 0,
                                  "steps_dispatched": 0, "deferred": False}
        if mode == "off":
            return report

        # Pursue-vs-defer via the self-model: under heavy load, hold off.
        if self._self_model is not None:
            try:
                load = self._self_model.load()
                if int(load.get("total") or 0) >= _int_env(
                        "COLONY_PROJECTS_DEFER_LOAD", 10):
                    logger.info("Project pursuit deferred (load=%s)", load)
                    report["deferred"] = True
                    return report
            except Exception:
                pass

        try:
            report["adopted"] = await self._adopt_initiatives()
        except Exception:
            logger.debug("project adoption failed", exc_info=True)
        try:
            report["planned"] = await self._plan_pending(mode)
        except Exception:
            logger.debug("project planning failed", exc_info=True)
        try:
            report["steps_dispatched"] = await self._pursue_active(mode)
        except Exception:
            logger.debug("project pursuit failed", exc_info=True)
        return report

    # ------------------------------------------------------------------
    # Adoption: project-type initiatives become durable projects
    # ------------------------------------------------------------------

    async def _adopt_initiatives(self) -> int:
        if self._initiatives is None:
            return 0
        open_projects = (self.store.count(status="active")
                         + self.store.count(status="planning"))
        if open_projects >= _int_env("COLONY_PROJECTS_MAX_CONCURRENT", 3):
            return 0
        try:
            loop = asyncio.get_event_loop()
            pending = await loop.run_in_executor(
                None, lambda: self._initiatives.list(
                    status=["pending"], type="project", limit=5))
        except Exception:
            return 0
        existing_titles = {p.title.strip().lower()
                           for p in self.store.list_projects(limit=200)}
        for init in pending or []:
            desc = (getattr(init, "description", "") or "").strip()
            if not desc or desc.split(".")[0].strip()[:120].lower() in existing_titles:
                continue
            rationale = (getattr(init, "rationale", "") or "").strip()
            objective = desc if not rationale else f"{desc}\n\nWhy: {rationale}"
            project, reason = self.create_project(
                objective, title=desc.split(".")[0][:120], source="thinker")
            if project is None:
                continue
            try:
                iid = getattr(init, "id", "")
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._initiatives.complete(
                        iid, agent_id="project-engine",
                        result=f"adopted as project {project.id}"))
            except Exception:
                logger.debug("initiative adoption closure failed", exc_info=True)
            logger.info("Adopted initiative as project %s: %r",
                        project.id, project.title)
            return 1
        return 0

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    async def _plan_pending(self, mode: str) -> int:
        from colony_sidecar.projects.planner import plan_project
        planned = 0
        now = time.time()
        for project in self.store.list_projects(status="planning", limit=10):
            if project.next_review_at and project.next_review_at > now:
                continue
            skills_block = self._skills_block(project.objective)
            brief = self._self_brief()
            steps = await plan_project(
                self._router, project.objective, project_id=project.id,
                skills_block=skills_block, self_brief=brief,
                boundaries=self._boundaries_brief())
            if not steps:
                project.replans += 1
                if project.replans > projects_max_replans():
                    project.status = "abandoned"
                    project.reason = "planning_failed"
                    self.store.save_project(project)
                    self._record_outcome("failure")
                    logger.warning("Project %s abandoned: planning failed %d times",
                                   project.id, project.replans)
                else:
                    project.next_review_at = now + projects_review_secs()
                    self.store.save_project(project)
                continue
            for s in steps:
                self.store.save_step(s)
            project.status = "active"
            project.next_review_at = 0.0
            self.store.save_project(project)
            planned += 1
            logger.info(
                "Project %s planned[%s]: %d step(s): %s",
                project.id, mode, len(steps),
                " | ".join(f"{s.ordinal}.{s.action_kind}:{s.description[:60]}"
                           for s in steps))
        return planned

    # ------------------------------------------------------------------
    # Pursuit
    # ------------------------------------------------------------------

    async def _pursue_active(self, mode: str) -> int:
        dispatched = 0
        for project in self.store.due_for_review(limit=3):
            try:
                advanced = await self._advance_project(project, mode)
                dispatched += 1 if advanced else 0
            except Exception:
                logger.error("project %s advance failed", project.id,
                             exc_info=True)
        return dispatched

    async def _advance_project(self, project: Project, mode: str) -> bool:
        steps = self.store.steps_for(project.id)
        if not steps:
            # active project with no steps: send back to planning
            project.status = "planning"
            self.store.save_project(project)
            return False

        # Terminal check: everything done/skipped -> complete.
        if all(s.status in ("done", "skipped") for s in steps):
            await self._complete_project(project, steps, mode)
            return False

        # A failed step triggers a bounded replan of the remaining work.
        if any(s.status == "failed" for s in steps):
            await self._replan_remaining(project, steps, mode)
            return False

        done_ordinals = {s.ordinal for s in steps if s.status in ("done", "skipped")}
        ready = [s for s in steps if s.status == "pending"
                 and all(d in done_ordinals for d in s.depends_on)]
        if not ready:
            # nothing ready (steps active or deps unmet) -- check again later
            project.next_review_at = time.time() + projects_review_secs()
            self.store.save_project(project)
            return False
        step = ready[0]

        # Boundary gate on the concrete step.
        if self._directives is not None:
            try:
                from colony_sidecar.directives import Action
                verdict = self._directives.check(Action(
                    kind=step.action_kind,
                    text=f"{step.description} {step.boundary_subject}",
                    target=project.subject_text(), high_risk=True))
                if not verdict.allowed:
                    project.status = "blocked"
                    project.reason = verdict.reason
                    self.store.save_project(project)
                    logger.warning(
                        "Project %s BLOCKED at step %d by boundary: %s",
                        project.id, step.ordinal, verdict.reason)
                    await self._milestone(
                        project, "blocked",
                        f"Step {step.ordinal} ({step.description[:120]}) hit a "
                        f"standing boundary: {verdict.reason}", mode)
                    return False
            except Exception:
                logger.debug("step boundary check failed (allowing)",
                             exc_info=True)

        if mode == "shadow":
            logger.info(
                "SHADOW-PROJECT %s step %d/%d [%s]: would dispatch %r "
                "(boundary=allowed)",
                project.id, step.ordinal, len(steps), step.action_kind,
                step.description[:160])
            step.status = "done"
            step.result = "SHADOW: simulated (no action taken)"
            self.store.save_step(step)
            project.next_review_at = time.time() + projects_review_secs()
            self.store.save_project(project)
            # re-check terminal state so a finished shadow run completes
            steps = self.store.steps_for(project.id)
            if all(s.status in ("done", "skipped") for s in steps):
                await self._complete_project(project, steps, mode)
            return True

        # LIVE dispatch through the kind's own gated sub-path.
        step.status = "active"
        step.attempts += 1
        self.store.save_step(step)
        started = time.monotonic()
        ok, result = await self._dispatch_step(project, step)
        latency = time.monotonic() - started

        if ok is None:
            # waiting on an external gate (e.g. directed approval): not an
            # attempt, re-check at next review.
            step.status = "pending"
            step.attempts = max(0, step.attempts - 1)
            step.result = result[:1000]
            self.store.save_step(step)
        elif ok:
            step.status = "done"
            step.result = result[:2000]
            self.store.save_step(step)
            self._record_outcome("success", latency,
                                 stated_confidence=step.confidence)
        else:
            self._record_outcome(
                "timeout" if "timeout" in (result or "").lower() else "failure",
                latency, stated_confidence=step.confidence)
            if step.attempts >= 2:
                step.status = "failed"
                step.result = result[:1000]
                self.store.save_step(step)
                if self._skills is not None:
                    try:
                        self._skills.record_failure_note(
                            "project",
                            f"step '{step.description[:80]}' failed: {result[:120]}")
                    except Exception:
                        pass
            else:
                step.status = "pending"
                step.result = f"attempt {step.attempts} failed: {result[:500]}"
                self.store.save_step(step)

        project.next_review_at = time.time() + projects_review_secs()
        self.store.save_project(project)

        steps = self.store.steps_for(project.id)
        if all(s.status in ("done", "skipped") for s in steps):
            await self._complete_project(project, steps, mode)
        return True

    # ------------------------------------------------------------------
    # Step dispatch by kind (LIVE mode only)
    # ------------------------------------------------------------------

    async def _dispatch_step(self, project: Project, step: Step,
                             ) -> Tuple[Optional[bool], str]:
        kind = step.action_kind
        try:
            if kind in ("analyze", "research", "internal"):
                return await self._run_reasoning_step(project, step)
            if kind == "directed":
                return await self._run_directed_step(project, step)
            if kind == "deliver":
                return await self._run_deliver_step(project, step)
        except Exception as exc:
            return False, f"dispatch error: {exc}"
        return False, f"unknown action_kind {kind}"

    async def _run_reasoning_step(self, project: Project, step: Step,
                                  ) -> Tuple[Optional[bool], str]:
        if self._reasoning is None:
            return False, "no reasoning loop wired"
        done = [s for s in self.store.steps_for(project.id)
                if s.status == "done" and s.result]
        context = "\n".join(
            f"- step {s.ordinal}: {s.result[:200]}" for s in done[-5:])
        prompt = (f"## Project: {project.title}\n"
                  f"Objective: {project.objective[:800]}\n\n"
                  + (f"## Completed so far\n{context}\n\n" if context else "")
                  + f"## Current step ({step.action_kind})\n{step.description}")
        try:
            from colony_sidecar.cognition.charter import build_system_prompt
            system = build_system_prompt(
                "executor",
                self_brief=self._self_brief() or None,
                boundaries=self._boundaries_brief() or None,
                skills=self._skills_block(step.description) or None,
                extra=_STEP_SCOPE_CONTEXT)
        except Exception:
            logger.debug("charter compose failed; minimal fallback",
                         exc_info=True)
            system = _STEP_SCOPE_CONTEXT
            db = self._boundaries_brief()
            if db:
                system += ("\n## Standing boundaries from the owner "
                           "(obey without exception)\n" + db)

        working: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]
        tier = os.environ.get("COLONY_PROJECTS_MODEL_TIER", "small")
        for _round in range(_MAX_TOOL_ROUNDS):
            result = await self._reasoning.run_turn(
                session_id=f"project-{project.id}-s{step.ordinal}",
                messages=working, system_prompt=system, model_override=tier)
            if result.status == "completed":
                text = (result.message or {}).get("content", "") or ""
                return True, text
            if result.status == "error":
                return False, result.error or "reasoning error"
            if result.status == "needs_tool":
                pending = list(result.tool_calls or [])
                if not pending or self._tools is None:
                    return False, "needs_tool with no executable tool path"
                pending = self._boundary_filter_tools(pending)
                if not pending:
                    working.append({
                        "role": "user",
                        "content": "Those actions violate a standing boundary "
                                   "and were refused. Summarise what you can "
                                   "and stop."})
                    continue
                results = await self._tools.execute_batch(
                    pending, session_id=f"project-{project.id}-s{step.ordinal}")
                working.append(_assistant_msg(
                    (result.message or {}).get("content", ""), pending))
                for tr in results:
                    working.append({"role": "tool",
                                    "tool_call_id": tr.get("tool_call_id", ""),
                                    "content": tr.get("content", "")})
                continue
            return False, f"unexpected reasoning status {result.status}"
        return False, f"tool-loop cap reached ({_MAX_TOOL_ROUNDS} rounds)"

    def _boundary_filter_tools(self, pending: List[dict]) -> List[dict]:
        if self._directives is None:
            return pending
        try:
            from colony_sidecar.directives import Action
        except Exception:
            return pending
        survivors = []
        for tc in pending:
            name = tc.get("name", "")
            args = tc.get("arguments", {}) if isinstance(
                tc.get("arguments"), dict) else {}
            try:
                verdict = self._directives.check(Action(
                    kind="execute_tool", tool_name=name, args=args, text=name,
                    high_risk=True))
            except Exception:
                verdict = None
            if verdict is not None and not verdict.allowed:
                logger.warning("Project tool call %s REFUSED by boundary: %s",
                               name, verdict.reason)
                continue
            survivors.append(tc)
        return survivors

    async def _run_directed_step(self, project: Project, step: Step,
                                 ) -> Tuple[Optional[bool], str]:
        if self._directed is None:
            return False, "no directed service wired"
        # An existing awaiting-approval task for this step: keep waiting.
        prior = (step.result or "")
        if prior.startswith("awaiting_approval:"):
            task_id = prior.split(":", 1)[1].strip()
            task = self._directed.store.get(task_id)
            if task is not None:
                if task.status == "awaiting_approval":
                    return None, prior
                if task.status in ("approved",):
                    out = await self._directed.dispatch(task.id)
                    return True, f"directed task {task.id} dispatched: {json.dumps(out, default=str)[:300]}"
                if task.status in ("dispatched", "dispatched_dry", "completed"):
                    return True, f"directed task {task.id} {task.status}"
                if task.status in ("refused", "violated", "failed", "expired"):
                    return False, f"directed task {task.id} {task.status}: {task.refusal_reason}"
        task = await self._directed.intake(step.description)
        if task.status == "refused":
            return False, f"directed intake refused: {task.refusal_reason}"
        if task.status == "awaiting_approval":
            return None, f"awaiting_approval:{task.id}"
        out = await self._directed.dispatch(task.id)
        status = "dry_run" if out.get("dry_run") else (
            "dispatched" if out.get("dispatched") else
            f"not dispatched ({out.get('reason', '?')})")
        ok = bool(out.get("dispatched") or out.get("dry_run"))
        return (True, f"directed task {task.id}: {status}") if ok else (
            False, f"directed task {task.id}: {status}")

    async def _run_deliver_step(self, project: Project, step: Step,
                                ) -> Tuple[Optional[bool], str]:
        done = [s for s in self.store.steps_for(project.id)
                if s.status == "done" and s.result
                and not s.result.startswith("SHADOW")]
        finding = "\n".join(
            f"- {s.result[:300]}" for s in done[-4:]) or project.objective[:300]
        try:
            from colony_sidecar.proposals import Proposal, proposal_to_payload
            prop = Proposal(
                title=f"Project update: {project.title[:70]}",
                finding=f"{step.description[:200]}\n{finding}"[:1200],
                why_it_helps=f"progress on your project: {project.title[:100]}",
                suggested_action="Tell me to continue, adjust, or stop this project.",
                source=project.id, initiative_type="proposal", confidence=0.7)
            if self._proposals is not None:
                self._proposals.add(prop)
            if self._deliver is not None:
                ok = bool(await self._deliver(proposal_to_payload(prop)))
                return True, ("delivered" if ok else
                              "routed to guarded delivery (held/gated)")
            return True, "proposal stored (no delivery router)"
        except Exception as exc:
            return False, f"deliver failed: {exc}"

    # ------------------------------------------------------------------
    # Replan / complete / milestones
    # ------------------------------------------------------------------

    async def _replan_remaining(self, project: Project, steps: List[Step],
                                mode: str) -> None:
        from colony_sidecar.projects.planner import plan_project
        project.replans += 1
        if project.replans > projects_max_replans():
            project.status = "abandoned"
            project.reason = "replan_limit"
            self.store.save_project(project)
            self._record_outcome("failure")
            if self._feedback is not None:
                try:
                    self._feedback.record("project", "dismissed")
                except Exception:
                    pass
            await self._milestone(
                project, "abandoned",
                f"replan limit reached after {project.replans - 1} replans", mode)
            return

        done = [s for s in steps if s.status == "done"]
        failed = [s for s in steps if s.status == "failed"]
        context = ""
        if done:
            context += "Completed steps:\n" + "\n".join(
                f"- {s.description[:120]}: {s.result[:150]}" for s in done[-5:])
        if failed:
            context += "\nFailed steps (plan around these failures):\n" + "\n".join(
                f"- {s.description[:120]}: {s.result[:150]}" for s in failed[-3:])

        new_steps = await plan_project(
            self._router,
            f"{project.objective}\n\nRe-plan ONLY the remaining work.",
            project_id=project.id, context=context,
            skills_block=self._skills_block(project.objective),
            self_brief=self._self_brief(),
            boundaries=self._boundaries_brief())
        self.store.delete_steps(project.id, statuses=["pending", "failed", "active"])
        base = max((s.ordinal for s in done), default=0)
        for s in new_steps:
            s.ordinal += base
            s.depends_on = [d + base for d in s.depends_on]
            self.store.save_step(s)
        project.next_review_at = time.time() + projects_review_secs()
        if not new_steps and not done:
            project.status = "abandoned"
            project.reason = "replan_produced_no_steps"
            self._record_outcome("failure")
        elif not new_steps:
            # nothing left to do beyond what completed
            self.store.save_project(project)
            await self._complete_project(
                project, self.store.steps_for(project.id), mode)
            return
        self.store.save_project(project)
        logger.info("Project %s replanned (replan %d/%d): %d new step(s)",
                    project.id, project.replans, projects_max_replans(),
                    len(new_steps))

    async def _complete_project(self, project: Project, steps: List[Step],
                                mode: str) -> None:
        if project.status == "completed":
            return
        project.status = "completed"
        project.reason = "all_steps_done"
        self.store.save_project(project)
        # Shadow completions are CALIBRATION evidence (graduate out of
        # shadow), never act-first evidence.
        self._record_outcome("success", shadow=(mode == "shadow"))
        if self._feedback is not None:
            try:
                self._feedback.record("project", "actioned")
            except Exception:
                pass
        summary = "; ".join(
            s.result[:100] for s in steps if s.status == "done" and s.result)[:800]
        await self._milestone(
            project, "completed",
            summary or f"{len(steps)} step(s) completed", mode)
        # Skill hook (item 3): non-trivial completion -> distill a procedure.
        if len(steps) >= 3 and mode == "live":
            try:
                from colony_sidecar.skills_memory import distill_from_completion
                transcript = "\n".join(
                    f"step {s.ordinal} ({s.action_kind}): {s.description[:150]} "
                    f"-> {s.result[:200]}" for s in steps)
                await distill_from_completion(
                    self._router, self._skills, domain="project",
                    task_text=project.objective, result_text=transcript,
                    source_ref=project.id)
            except Exception:
                logger.debug("project skill distillation failed", exc_info=True)
        logger.info("Project %s COMPLETED[%s]: %s", project.id, mode,
                    project.title)

    async def _milestone(self, project: Project, event: str, detail: str,
                         mode: str) -> None:
        """Status-change report as a Proposal. Shadow: stored + logged only."""
        try:
            from colony_sidecar.proposals import Proposal, proposal_to_payload
            prop = Proposal(
                title=f"Project {event}: {project.title[:60]}",
                finding=detail[:1000],
                why_it_helps=("keeps you in control of work I am pursuing "
                              "on your behalf"),
                suggested_action=(
                    "Review and tell me whether to proceed differently"
                    if event in ("blocked", "abandoned")
                    else "Review the outcome; tell me any follow-ups"),
                source=project.id, initiative_type="proposal",
                confidence=0.85 if event == "blocked" else 0.7)
            if self._proposals is not None:
                self._proposals.add(prop)
            if mode == "shadow" or self._deliver is None:
                logger.info(
                    "SHADOW-PROJECT-MILESTONE %s [%s]: %s -- %s",
                    project.id, event, prop.title, detail[:200])
                return
            await self._deliver(proposal_to_payload(prop))
        except Exception:
            logger.debug("project milestone failed", exc_info=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _skills_block(self, situation: str) -> str:
        if self._skills is None:
            return ""
        try:
            from colony_sidecar.skills_memory import (
                format_block, relevant_skills, skills_enabled,
            )
            if not skills_enabled():
                return ""
            return format_block(
                relevant_skills(self._skills, situation, k=3, domain="project"),
                strategy_note=self._skills.get_note("project"))
        except Exception:
            return ""

    def _boundaries_brief(self) -> str:
        if self._directives is None:
            return ""
        try:
            return self._directives.context_brief() or ""
        except Exception:
            return ""

    def _self_brief(self) -> str:
        if self._self_model is None:
            return ""
        try:
            return self._self_model.brief()
        except Exception:
            return ""

    def _record_outcome(self, outcome: str,
                        latency: Optional[float] = None,
                        shadow: bool = False,
                        stated_confidence: Optional[float] = None) -> None:
        if self._self_model is None:
            return
        try:
            self._self_model.record("project", outcome, latency_secs=latency,
                                    shadow=shadow,
                                    stated_confidence=stated_confidence)
        except Exception:
            pass


def _assistant_msg(content: str, tool_calls: List[dict]) -> Dict[str, Any]:
    """OpenAI-shaped assistant message carrying tool calls (mirrors the
    initiative executor's continuation shape)."""
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {"id": tc.get("id", ""), "type": "function",
             "function": {"name": tc.get("name", ""),
                          "arguments": (tc["arguments"]
                                        if isinstance(tc.get("arguments"), str)
                                        else json.dumps(tc.get("arguments", {})))}}
            for tc in tool_calls
        ],
    }
