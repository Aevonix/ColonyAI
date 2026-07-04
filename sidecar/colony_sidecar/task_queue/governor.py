"""WorkerGovernor -- server-side enforcement for the distributed job queue
(cognition program, Phase B item 5).

The worker daemon is UNTRUSTED. Capability coverage, boundary compliance, and
the scope of what a worker actually did are all re-decided HERE, server-side,
never taken on the worker's word:

  claim       -> evaluate_claim(): re-verify (a) the worker's advertised
                 capabilities really cover the job's requirement and (b) the
                 job's subject is not under a standing owner boundary
                 (DirectiveGuard, capability-aware). A boundaried or
                 uncovered claim is refused server-side.
  completion  -> audit_report(): cross-check the worker's structured report
                 against what the job was authorized to do. A worker that
                 reports a mutation on a read-only job (or a force-push, or
                 out-of-scope deletes) is a VIOLATION, flagged loudly.
  outcome     -> record_outcome(): the audited verdict is the earned-autonomy
                 evidence. Each worker JOB TYPE is its own trust domain
                 ("worker:<job_type>") feeding the self-model/trust engine
                 (item 4): clean real completions graduate it, a violation
                 trips its circuit breaker. Feedback + journal + skill
                 distillation ride the same chokepoint.

Mode (COLONY_WORKERS_MODE, default shadow):
  off    -> governor disabled; the queue behaves exactly as before.
  shadow -> CALIBRATION. Every claim is evaluated and journaled but ALLOWED
            regardless (would_refuse is recorded, not enforced); outcomes are
            recorded as shadow events so the trust engine can graduate the
            job type out of calibration. Non-blocking by design, so turning it
            on never disturbs the already-live agent_action path.
  live   -> ENFORCING. A refused claim is blocked server-side; outcomes count
            as real evidence toward earned autonomy.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Reported operations that imply the worker CHANGED something. A worker that
# reports any of these on a read-only job exceeded its grant.
_MUTATE_OPS = frozenset({
    "modify_files", "write", "commit", "push", "push_branch", "open_pr",
    "delete", "rm", "execute", "deploy", "send", "post", "create", "update",
})

# Risk tiers (from the action registry) that authorize a job to change state.
# A job at read_only risk that reports a mutation is a violation.
_MUTATING_RISK = frozenset({"low", "medium", "high", "outbound", "destructive"})


def workers_mode() -> str:
    m = os.environ.get("COLONY_WORKERS_MODE", "shadow").strip().lower()
    return m if m in ("off", "shadow", "live") else "shadow"


class WorkerGovernor:
    """Server-authoritative claim gate + completion audit for queue workers."""

    def __init__(
        self,
        *,
        directive_manager: Any = None,
        feedback_store: Any = None,
        self_model: Any = None,
        delivery_router: Any = None,      # awaitable(payload)->bool, guarded reach-out
        skill_store: Any = None,
        llm_router: Any = None,
    ) -> None:
        self._directives = directive_manager
        self._feedback = feedback_store
        self._self_model = self_model
        self._deliver = delivery_router
        self._skills = skill_store
        self._llm = llm_router

    # -- trust domain per job type ---------------------------------------
    @staticmethod
    def trust_domain(job_type: str) -> str:
        return f"worker:{(job_type or 'custom').strip().lower()}"

    # -- job classification (never trust the worker's own claim) ----------
    @staticmethod
    def _job_authorized_to_mutate(job: Any) -> bool:
        """Was THIS job authorized to change state? Read-only jobs were not.

        A job carries its risk tier and approval provenance in payload/tags
        (set at post time by the approval policy). read_only risk => not
        authorized; an approved or mutating-risk job => authorized.
        """
        payload = getattr(job, "payload", {}) or {}
        tags = getattr(job, "tags", {}) or {}
        risk = str(payload.get("risk", "")).strip().lower()
        if risk == "read_only":
            return False
        if payload.get("destructive"):
            return True
        if risk in _MUTATING_RISK:
            return True
        if tags.get("approved_by") or tags.get("auto_approved_by_policy"):
            return True
        # Unknown provenance: assume read-only (the safer audit posture --
        # a surprising mutation should surface, not slip through).
        return False

    def _job_action(self, job: Any):
        """Build the DirectiveGuard Action for this job's subject.

        Worker jobs are active autonomous work -> ACT capability (an
        ACT-level "leave X alone" boundary must stop a worker touching X;
        awareness/reads survive). high_risk on any mutating/outbound job so
        the guard fails closed on ambiguity.
        """
        from colony_sidecar.directives import Action
        payload = getattr(job, "payload", {}) or {}
        text = " ".join(str(payload.get(k, "")) for k in
                        ("description", "action_hint", "domain")).strip()
        target = str(payload.get("entity_id") or payload.get("domain")
                     or payload.get("target") or "")
        return Action(
            kind="execute_tool",
            text=text or str(getattr(job, "job_type", "")),
            target=target,
            entity_id=str(payload.get("entity_id") or ""),
            high_risk=self._job_authorized_to_mutate(job),
        )

    @staticmethod
    def _required_caps(job: Any) -> List[str]:
        req: List[str] = []
        try:
            req.extend(job.required_capabilities())
        except Exception:
            pass
        tags = getattr(job, "tags", {}) or {}
        rc = tags.get("required_capability")
        if rc:
            req.append(str(rc))
        return [c for c in req if c]

    # -- claim gate -------------------------------------------------------
    def evaluate_claim(self, job: Any, worker_capabilities: Iterable[str],
                       worker_node_id: str = "") -> Dict[str, Any]:
        """Re-decide, server-side, whether this worker may run this job.

        Returns {allowed, enforced, would_refuse, reason, capability_ok,
        boundary_ok, shadow}. In shadow, allowed is always True (calibration)
        but would_refuse records what live mode WOULD do.
        """
        mode = workers_mode()
        if mode == "off":
            return {"allowed": True, "enforced": False, "would_refuse": False,
                    "reason": "governor_off", "capability_ok": True,
                    "boundary_ok": True, "shadow": False}

        caps = set(worker_capabilities or [])
        required = self._required_caps(job)
        missing = [c for c in required if c not in caps]
        capability_ok = not missing

        boundary_ok = True
        boundary_reason = "ok"
        if self._directives is not None:
            try:
                verdict = self._directives.check(self._job_action(job))
                boundary_ok = bool(verdict.allowed)
                boundary_reason = verdict.reason
            except Exception:
                logger.debug("worker claim boundary check failed (allowing)",
                             exc_info=True)

        would_refuse = not (capability_ok and boundary_ok)
        if would_refuse and not capability_ok:
            reason = f"worker lacks required capabilities: {missing}"
        elif would_refuse:
            reason = f"job subject under boundary: {boundary_reason}"
        else:
            reason = "ok"

        shadow = mode != "live"
        allowed = True if shadow else not would_refuse

        # A refused (live) or would-be-refused (shadow) claim is journaled;
        # a clean claim is not (it becomes an outcome at completion).
        if would_refuse:
            self._journal(
                self.trust_domain(getattr(job, "job_type", None)
                                  and job.job_type.value if hasattr(
                                      getattr(job, "job_type", None), "value")
                                  else getattr(job, "job_type", "custom")),
                f"worker {worker_node_id} claim of {self._job_id(job)}",
                reasoning=reason,
                decision="blocked" if not allowed else "noted",
                ref=self._job_id(job))
        return {"allowed": allowed, "enforced": mode == "live",
                "would_refuse": would_refuse, "reason": reason,
                "capability_ok": capability_ok, "boundary_ok": boundary_ok,
                "missing_capabilities": missing, "shadow": shadow}

    # -- completion audit (never trust the report) ------------------------
    def audit_report(self, job: Any, report: Dict[str, Any]) -> Dict[str, Any]:
        """Cross-check the worker's structured report against its authority.

        Verdict: 'clean' | 'violation' | 'unverified'. A violation from any
        check wins (fail loud).
        """
        report = report or {}
        findings: List[str] = []
        ok = True

        authorized = self._job_authorized_to_mutate(job)
        ops = [str(o).strip().lower() for o in (report.get("operations") or [])]
        reported_mutation = (
            bool(set(ops) & _MUTATE_OPS)
            or int(report.get("commits") or 0) > 0
            or bool(report.get("files_written"))
            or bool(report.get("deletions"))
        )
        if reported_mutation and not authorized:
            ok = False
            findings.append(
                "worker reported a mutation on a job not authorized to change "
                f"state (ops={sorted(set(ops) & _MUTATE_OPS) or 'commits/files'})")
        if report.get("force_push"):
            ok = False
            findings.append("force push reported (never allowed)")

        # Capability escalation: a worker reporting it used a capability the
        # job never required and the worker never advertised is suspicious.
        # (Kept advisory unless it co-occurs with a mutation.)
        if reported_mutation and not authorized and report.get("escalated"):
            findings.append("worker reported escalating its own scope")

        has_report = bool(report) and any(
            k in report for k in ("summary", "operations", "result", "output"))
        if not ok:
            verdict = "violation"
        elif has_report:
            verdict = "clean"
        else:
            verdict = "unverified"

        result = {"verdict": verdict, "findings": findings,
                  "authorized_to_mutate": authorized,
                  "reported_mutation": reported_mutation}
        if verdict == "violation":
            logger.warning("WORKER SCOPE VIOLATION on %s: %s",
                           self._job_id(job), "; ".join(findings)[:400])
        return result

    # -- outcome recording (feedback + self-model + journal + skills) -----
    async def record_outcome(self, job: Any, report: Dict[str, Any],
                             verdict: str, *, outcome: Optional[str] = None,
                             latency: Optional[float] = None,
                             attempts: int = 0) -> None:
        """Fold an audited completion into the trust/accountability layer."""
        report = report or {}
        mode = workers_mode()
        if mode == "off":
            return
        jt = self._job_type_value(job)
        domain = self.trust_domain(jt)
        shadow = mode != "live"

        if outcome is None:
            outcome = "failure" if verdict == "violation" else "success"

        stated = report.get("confidence")
        try:
            stated = float(stated) if stated is not None else None
        except (TypeError, ValueError):
            stated = None

        # Feedback (owner-reaction multiplier lives per type).
        if self._feedback is not None:
            try:
                self._feedback.record(
                    f"worker_{jt}",
                    "actioned" if verdict == "clean" else "dismissed")
            except Exception:
                logger.debug("worker feedback record failed", exc_info=True)

        # Self-model / trust engine: the earned-autonomy evidence.
        if self._self_model is not None:
            try:
                self._self_model.record(
                    domain, outcome, latency_secs=latency,
                    shadow=shadow, violation=(verdict == "violation"),
                    stated_confidence=stated)
            except Exception:
                logger.debug("worker self-model record failed", exc_info=True)

        self._journal(
            domain,
            f"job {self._job_id(job)} ({jt}) completed",
            reasoning=f"audit verdict: {verdict}",
            confidence=stated,
            reversibility="recoverable" if self._job_authorized_to_mutate(job)
                          else "reversible",
            decision="noted", outcome=verdict, ref=self._job_id(job))

        # Skill distillation on genuine successes (retry-success / novel).
        if outcome == "success" and verdict != "violation":
            await self._maybe_distill(job, report, attempts)

        # Owner-facing violation notice through the guarded reach-out path.
        if verdict == "violation" and self._deliver is not None:
            await self._notify_violation(job, report)

    async def _maybe_distill(self, job: Any, report: Dict[str, Any],
                             attempts: int) -> None:
        if self._skills is None or self._llm is None:
            return
        try:
            from colony_sidecar.skills_memory import (
                should_distill, distill_from_completion,
            )
            result_text = str(report.get("summary")
                              or report.get("result") or "")
            if not should_distill(attempts, result_text, self._skills):
                return
            payload = getattr(job, "payload", {}) or {}
            task_text = str(payload.get("description")
                           or payload.get("action_hint") or "")
            await distill_from_completion(
                self._llm, self._skills,
                domain=self.trust_domain(self._job_type_value(job)),
                task_text=task_text, result_text=result_text,
                source_ref=self._job_id(job))
        except Exception:
            logger.debug("worker skill distill failed", exc_info=True)

    async def _notify_violation(self, job: Any, report: Dict[str, Any]) -> None:
        try:
            from colony_sidecar.proposals import Proposal, proposal_to_payload
            findings = "; ".join(
                self.audit_report(job, report).get("findings", [])[:5])
            prop = Proposal(
                title=f"Worker scope violation on job {self._job_id(job)}",
                finding=(f"A queue worker exceeded its authorized scope: "
                         f"{findings}. I flagged and recorded it; the job "
                         "type has been demoted from autonomous execution."),
                why_it_helps="protects you from an over-reaching worker",
                suggested_action="Review the flagged job and worker",
                source=self._job_id(job), initiative_type="proposal",
                confidence=0.95)
            await self._deliver(proposal_to_payload(prop))
        except Exception:
            logger.debug("worker violation notify failed", exc_info=True)

    # -- observability ----------------------------------------------------
    def status(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"mode": workers_mode()}
        trust = getattr(self._self_model, "trust", None)
        if trust is not None:
            try:
                out["worker_domains"] = [
                    s for s in trust.snapshot()
                    if str(s.get("domain", "")).startswith("worker:")]
            except Exception:
                pass
        return out

    # -- helpers ----------------------------------------------------------
    def _journal(self, domain: str, description: str, **kw: Any) -> None:
        journal = getattr(self._self_model, "journal", None)
        if journal is None:
            return
        try:
            journal.record(domain, description, **kw)
        except Exception:
            logger.debug("worker journal failed", exc_info=True)

    @staticmethod
    def _job_id(job: Any) -> str:
        return str(getattr(job, "job_id", "") or getattr(job, "id", "") or "?")

    @staticmethod
    def _job_type_value(job: Any) -> str:
        jt = getattr(job, "job_type", "custom")
        return jt.value if hasattr(jt, "value") else str(jt)
