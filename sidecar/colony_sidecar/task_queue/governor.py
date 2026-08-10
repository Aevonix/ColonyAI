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
            regardless (would_refuse is recorded, not enforced); only
            explicitly verified completions become shadow competence events.
            Neutral skips/unknowns never graduate a job type. Non-blocking by
            design, so turning it on never disturbs the agent_action path.
  live   -> ENFORCING. A refused claim is blocked server-side; outcomes count
            as real evidence toward earned autonomy.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
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


def job_declares_effect(job: Any) -> bool:
    """Classify effectful work without treating its label as permission."""

    payload = getattr(job, "payload", {}) or {}
    risk = str(
        payload.get("risk_class") or payload.get("risk") or ""
    ).strip().lower()
    action = str(
        payload.get("action") or payload.get("command")
        or payload.get("action_hint") or ""
    ).strip().lower()
    registered_action_effect = False
    action_hint = str(payload.get("action_hint") or "").strip()
    if action_hint:
        try:
            from colony_sidecar.initiatives.action_registry import (
                RiskTier,
                get_action,
            )

            spec = get_action(action_hint)
            registered_action_effect = bool(
                spec is not None and spec.risk is not RiskTier.READ_ONLY
            )
        except Exception:
            # Registry availability is not needed to preserve the other
            # conservative effect signals below.
            registered_action_effect = False
    job_type = getattr(job, "job_type", "")
    job_type = (
        job_type.value if hasattr(job_type, "value") else str(job_type)
    ).strip().lower()
    required: set[str] = set()
    try:
        required.update(str(item) for item in job.required_capabilities())
    except Exception:
        pass
    extra = str((getattr(job, "tags", {}) or {}).get(
        "required_capability", ""
    )).strip()
    if extra:
        required.add(extra)
    effect_capability = any(
        capability in {
            "agent:tools", "code:execute", "filesystem:write",
            "git:write", "messaging:send", "shell", "terminal:execute",
        }
        or capability.endswith(":write")
        or capability.endswith(":send")
        for capability in required
    )
    return bool(
        payload.get("destructive") is True
        or registered_action_effect
        or effect_capability
        or risk in (_MUTATING_RISK | {"mutation", "disclosure"})
        or job_type == "system_maintenance"
        or action in (_MUTATE_OPS | {
            "disk_cleanup", "log_rotate", "db_vacuum",
            "agent_project_directed", "agent_project_deliver",
        })
    )


def workers_mode() -> str:
    from colony_sidecar.util.autonomy_preset import resolve
    explicit = os.environ.get("COLONY_WORKERS_MODE")
    if explicit is not None and explicit.strip():
        value = explicit.strip().lower()
        if value not in {"off", "shadow", "live"}:
            raise RuntimeError(
                "COLONY_WORKERS_MODE must be off, shadow, or live"
            )
    return resolve("COLONY_WORKERS_MODE", ("off", "shadow", "live"), "shadow")


@dataclass(frozen=True)
class ClaimVerdict:
    """Immutable authority result consumed by the central queue claim path.

    Compatibility mapping helpers keep the existing read-only tests and API
    serialization ergonomic, but callers cannot fabricate or mutate a partial
    dictionary. Construction rejects inconsistent mode/boolean combinations.
    """

    mode: str
    allowed: bool
    enforced: bool
    would_refuse: bool
    reason: str
    capability_ok: Optional[bool]
    boundary_ok: Optional[bool]
    boundary_reason: str
    missing_capabilities: tuple[str, ...] = ()
    shadow: bool = False
    trust_ok: Optional[bool] = True
    trust_reason: str = "trust_not_required"

    def __post_init__(self) -> None:
        if self.mode not in {"off", "shadow", "live"}:
            raise ValueError("invalid worker-governor mode")
        for field_name in ("allowed", "enforced", "would_refuse", "shadow"):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be an exact boolean")
        for field_name in ("capability_ok", "boundary_ok", "trust_ok"):
            value = getattr(self, field_name)
            if self.mode == "off":
                # Mode off checks NOTHING: the verdict must say "unchecked"
                # (None), never fabricate a pass — and never a bool at all,
                # so an off verdict cannot be mistaken for an evaluated one.
                if value is not None:
                    raise TypeError(
                        f"{field_name} must be None (unchecked) in mode 'off'"
                    )
            elif type(value) is not bool:
                raise TypeError(f"{field_name} must be an exact boolean")
        if not isinstance(self.reason, str) or not isinstance(
            self.boundary_reason, str
        ):
            raise TypeError("claim verdict reasons must be strings")
        if not isinstance(self.missing_capabilities, tuple) or any(
            not isinstance(item, str) for item in self.missing_capabilities
        ):
            raise TypeError("missing_capabilities must be a tuple of strings")

        expected_refusal = not (
            self.capability_ok and self.boundary_ok and self.trust_ok
        )
        if self.mode == "off":
            # capability/boundary/trust are already required to be None
            # ("unchecked") above.
            valid = (
                self.allowed and not self.enforced and not self.would_refuse
                and not self.shadow
            )
        elif self.mode == "shadow":
            valid = (
                self.shadow
                and self.enforced == (not self.allowed)
                and self.would_refuse == expected_refusal
            )
        else:
            valid = (
                self.enforced and not self.shadow
                and self.would_refuse == expected_refusal
                and self.allowed == (not expected_refusal)
            )
        if not valid:
            raise ValueError("inconsistent worker claim verdict")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "allowed": self.allowed,
            "enforced": self.enforced,
            "would_refuse": self.would_refuse,
            "reason": self.reason,
            "capability_ok": self.capability_ok,
            "boundary_ok": self.boundary_ok,
            "boundary_reason": self.boundary_reason,
            "missing_capabilities": list(self.missing_capabilities),
            "shadow": self.shadow,
            "trust_ok": self.trust_ok,
            "trust_reason": self.trust_reason,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)


class WorkerGovernor:
    """Server-authoritative claim gate + completion audit for queue workers."""

    def __init__(
        self,
        *,
        directive_manager: Any = None,
        feedback_store: Any = None,
        self_model: Any = None,
        delivery_router: Any = None,      # awaitable(payload)->bool, guarded reach-out
        proposal_store: Any = None,
        skill_store: Any = None,
        llm_router: Any = None,
        boundary_required: bool = False,
    ) -> None:
        self._directives = directive_manager
        self._feedback = feedback_store
        self._self_model = self_model
        self._deliver = delivery_router
        self._proposal_store = proposal_store
        self._skills = skill_store
        self._llm = llm_router
        self._boundary_required = bool(boundary_required)

    def ready_for_live_claims(self) -> bool:
        """Whether this instance is valid live claim authority."""

        trust = getattr(self._self_model, "trust", None)
        return bool(
            self._boundary_required
            and self._directives is not None
            and self._self_model is not None
            and trust is not None
        )

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
        # Risk/action fields are untrusted classification input.  They may
        # increase gating but can never grant mutation authority.  Permission
        # must be provenance stamped by the server-owned approval/policy
        # plane and bound to the queued action.
        return bool(
            job_declares_effect(job)
            and (
                tags.get("approved_by")
                or tags.get("auto_approved_by_policy")
            )
        )

    def _job_action(self, job: Any):
        """Build the DirectiveGuard Action for this job's subject.

        Worker jobs are active autonomous work -> ACT capability (an
        ACT-level "leave X alone" boundary must stop a worker touching X;
        awareness/reads survive). high_risk on any mutating/outbound job so
        the guard fails closed on ambiguity.
        """
        from colony_sidecar.directives import Action
        payload = getattr(job, "payload", {}) or {}
        subject_keys = (
            "description", "action_hint", "domain", "action", "command",
            "script", "query", "endpoint", "url", "target_path", "path",
            "target", "entity_id", "purpose",
        )
        # Only fields that name the proposed work belong in the boundary
        # subject.  Thought jobs also carry prompts, injected directives, and
        # other control context.  Passing the whole payload through ``args``
        # made DirectiveGuard index that context as if the worker intended to
        # act on it, so an injected boundary could match its own text and
        # permanently hold an otherwise unrelated job.
        subject_args = {
            key: payload[key]
            for key in subject_keys
            if type(payload.get(key)) in (str, int, float)
            and payload.get(key) not in (None, "")
        }
        text = " ".join(
            str(subject_args[key]) for key in subject_keys
            if key in subject_args
        ).strip()
        target = str(
            subject_args.get("target_path") or subject_args.get("path")
            or subject_args.get("endpoint") or subject_args.get("url")
            or subject_args.get("target") or subject_args.get("entity_id")
            or subject_args.get("domain") or ""
        )
        return Action(
            kind="execute_tool",
            text=text or str(getattr(job, "job_type", "")),
            target=target,
            entity_id=str(subject_args.get("entity_id") or ""),
            tool_name=str(
                subject_args.get("action") or subject_args.get("command")
                or getattr(getattr(job, "job_type", None), "value", "")
                or getattr(job, "job_type", "")
            ),
            args=subject_args,
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

    def _read_only_live_trial_status(
        self,
        domain: str,
    ) -> tuple[bool, str]:
        """Authorize one bounded, evidence-backed read-only live trial.

        ``ask_first`` normally requires an owner decision.  That creates a
        bootstrap deadlock for no-effect worker domains: calibration can
        prove they are safe enough to leave shadow, but they can never
        produce the real, independently attested outcomes required to earn
        ``act_first``.  This bridge is deliberately narrower than approval:

        * it is considered only for jobs already classified as no-effect;
        * the domain needs clean, server-attested no-effect shadow evidence;
        * any real failure, timeout, violation, or evidence gap closes it;
        * every real attempt consumes one of a small, bounded trial budget.

        Capability, boundary, route, owner, and global kill-switch checks are
        separate claim gates and remain mandatory.
        """

        store = getattr(self._self_model, "store", None)
        if store is None or not hasattr(store, "events"):
            return False, "read_only_live_trial_store_unavailable"
        try:
            if (
                hasattr(store, "active_evidence_gaps")
                and store.active_evidence_gaps(domain)
            ):
                return False, "read_only_live_trial_evidence_gap"

            try:
                ask_min = int(os.environ.get("COLONY_TRUST_ASK_MIN_N", "3"))
            except (TypeError, ValueError):
                ask_min = 3
            ask_min = max(1, min(ask_min, 100))
            try:
                trial_max = int(os.environ.get(
                    "COLONY_TRUST_READ_ONLY_TRIAL_MAX",
                    os.environ.get("COLONY_TRUST_ACT_MIN_N", "5"),
                ))
            except (TypeError, ValueError):
                trial_max = 5
            trial_max = max(1, min(trial_max, 20))

            history = store.events(
                domain,
                include_shadow=True,
                include_invalid=True,
                include_unavailable=True,
            )
            shadow_history = [
                event for event in history if bool(event.get("shadow"))
            ]
            clean_calibration = []
            for event in shadow_history:
                evidence = event.get("evidence")
                if not isinstance(evidence, dict):
                    continue
                if (
                    event.get("source") == "task_queue.governor"
                    and event.get("outcome") == "success"
                    and not bool(event.get("violation"))
                    and event.get("evidence_status") == "verified"
                    and event.get("outcome_contract")
                    == "colony.worker-outcome/v1"
                    and evidence.get("schema") == "colony.worker-outcome/v1"
                    and evidence.get("success_attested") is True
                    and evidence.get("effectful") is False
                    and event.get("valid") is not False
                    and event.get("evidence_available") is not False
                ):
                    clean_calibration.append(event)
            if len(clean_calibration) < ask_min:
                return (
                    False,
                    "read_only_live_trial_calibration_insufficient_"
                    f"{len(clean_calibration)}_of_{ask_min}",
                )

            real_history = [
                event for event in history if not bool(event.get("shadow"))
            ]
            if any(
                bool(event.get("violation"))
                or event.get("outcome") in {"failure", "timeout"}
                or event.get("valid") is False
                or event.get("evidence_available") is False
                for event in real_history
            ):
                return False, "read_only_live_trial_negative_evidence"
            trial_count = len(real_history)
            if trial_count >= trial_max:
                return (
                    False,
                    f"read_only_live_trial_budget_exhausted_{trial_max}",
                )
            return (
                True,
                f"bounded_read_only_live_trial_{trial_count + 1}_of_"
                f"{trial_max}",
            )
        except Exception:
            logger.warning(
                "worker read-only live-trial evidence check failed closed",
                exc_info=True,
            )
            return False, "read_only_live_trial_check_failed_closed"

    # -- claim gate -------------------------------------------------------
    def evaluate_claim(self, job: Any, worker_capabilities: Iterable[str],
                       worker_node_id: str = "") -> ClaimVerdict:
        """Re-decide, server-side, whether this worker may run this job.

        Returns an immutable :class:`ClaimVerdict`. In shadow, ``allowed`` is
        always true (calibration), while ``would_refuse`` records what live
        mode would do.
        """
        mode = workers_mode()
        if mode == "off":
            # Nothing is checked in mode "off": say so (None = unchecked)
            # rather than fabricating capability/boundary/trust passes.
            return ClaimVerdict(
                mode="off", allowed=True, enforced=False,
                would_refuse=False, reason="governor_off",
                capability_ok=None, boundary_ok=None,
                boundary_reason="governor_off_unchecked", shadow=False,
                trust_ok=None, trust_reason="governor_off_unchecked",
            )

        caps = set(worker_capabilities or [])
        required = self._required_caps(job)
        missing = [c for c in required if c not in caps]
        capability_ok = not missing

        boundary_ok = not self._boundary_required
        boundary_reason = (
            "boundary_checker_not_required"
            if boundary_ok else "boundary_checker_unavailable"
        )
        job_action = self._job_action(job)
        if self._directives is not None:
            try:
                verdict = self._directives.check(job_action)
                if type(getattr(verdict, "allowed", None)) is not bool:
                    raise TypeError("directive verdict allowed must be boolean")
                boundary_ok = verdict.allowed
                boundary_reason = str(getattr(verdict, "reason", "") or "ok")
            except Exception:
                # A configured boundary dependency is part of the live claim
                # authority. Its failure cannot be interpreted as permission.
                # Shadow remains observational, but records the exact claim it
                # would refuse after graduation.
                boundary_ok = False
                boundary_reason = "boundary_check_failed_closed"
                logger.warning(
                    "worker claim boundary check failed closed",
                    exc_info=True,
                )

        jt = self._job_type_value(job)
        domain = self.trust_domain(jt)
        tags = getattr(job, "tags", {}) or {}
        approved = bool(
            tags.get("approved_by") or tags.get("auto_approved_by_policy")
        )
        effectful = job_declares_effect(job)
        trust = getattr(self._self_model, "trust", None)
        if trust is None:
            trust_ok = False
            trust_reason = "worker_trust_unavailable"
        else:
            try:
                from colony_sidecar.self_model.trust import floor_class

                stage = str(trust.stage(domain) or "shadow")
                confidence = float(trust.confidence(domain))
                try:
                    threshold = float(os.environ.get(
                        "COLONY_TRUST_ACT_THRESHOLD", "0.8"
                    ))
                except ValueError:
                    threshold = 0.8
                floor = floor_class(
                    f"{job_action.text} {job_action.target} "
                    f"{job_action.tool_name}"
                )
                if mode == "shadow" and effectful:
                    trust_ok = False
                    trust_reason = "shadow_effect_execution_disabled"
                elif effectful and not approved:
                    trust_ok = False
                    trust_reason = "action_approval_required"
                elif floor and not approved:
                    trust_ok = False
                    trust_reason = f"immutable_floor_{floor}"
                elif (
                    mode == "live"
                    and stage == "ask_first"
                    and not effectful
                    and not approved
                ):
                    trust_ok, trust_reason = (
                        self._read_only_live_trial_status(domain)
                    )
                else:
                    trust_ok = approved or (
                        stage == "act_first" and confidence >= threshold
                    )
                    trust_reason = (
                        f"worker_trust_{stage}_confidence_{confidence:.3f}"
                        + ("_approved" if approved else "")
                    )
            except Exception:
                trust_ok = False
                trust_reason = "worker_trust_check_failed_closed"

        would_refuse = not (capability_ok and boundary_ok and trust_ok)
        if would_refuse and not capability_ok:
            reason = f"worker lacks required capabilities: {missing}"
        elif would_refuse and boundary_reason in {
            "boundary_checker_unavailable", "boundary_check_failed_closed",
        }:
            reason = f"job boundary unavailable: {boundary_reason}"
        elif would_refuse:
            if not boundary_ok:
                reason = f"job subject under boundary: {boundary_reason}"
            else:
                reason = f"worker autonomy not earned: {trust_reason}"
        else:
            reason = "ok"

        shadow = mode == "shadow"
        allowed = (
            not would_refuse
            if mode == "live"
            else (not effectful if mode == "shadow" else True)
        )

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
        return ClaimVerdict(
            mode=mode,
            allowed=allowed,
            enforced=(mode == "live" or (mode == "shadow" and not allowed)),
            would_refuse=would_refuse,
            reason=reason,
            capability_ok=capability_ok,
            boundary_ok=boundary_ok,
            boundary_reason=boundary_reason,
            missing_capabilities=tuple(missing),
            shadow=shadow,
            trust_ok=trust_ok,
            trust_reason=trust_reason,
        )

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
            k in report for k in (
                "summary", "operations", "result", "output",
                "execution_result",
            )
        )
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
    @staticmethod
    def classify_completion_outcome(
        report: Dict[str, Any],
        verdict: str,
    ) -> tuple[str, str]:
        """Classify semantic work outcome independently of HTTP completion.

        ``POST .../complete`` only says the worker stopped and returned a
        payload. It is not evidence that useful work succeeded. Success needs
        a clean audit plus an explicit verified completion marker. Policy
        skips, cancellations, unknown/malformed terminal states, and ordinary
        unverified reports are neutral: journalable, but never competence or
        earned-autonomy evidence.
        """

        payload = report if isinstance(report, dict) else {}
        execution_result = (
            payload.get("execution_result")
            if isinstance(payload.get("execution_result"), dict)
            else {}
        )
        semantic = str(
            payload.get("status") or payload.get("outcome")
            or execution_result.get("terminal_outcome") or ""
        ).strip().lower()
        if semantic == "succeeded":
            semantic = "completed"
        action_plane = (
            payload.get("action_plane")
            if isinstance(payload.get("action_plane"), dict)
            else {}
        )
        action_state = str(action_plane.get("state") or "").strip().lower()
        verification = str(
            payload.get("verification_status") or ""
        ).strip().lower()

        if verdict == "violation":
            return "failure", "scope_violation"
        failure_markers = {"failed", "failure", "error"}
        if semantic in failure_markers or action_state in failure_markers:
            return "failure", "reported_failure"
        neutral_markers = {
            "skipped", "skip", "cancelled", "canceled", "unknown",
            "pending", "proposed", "gated", "accepted",
        }
        if semantic in neutral_markers or action_state in neutral_markers:
            return "neutral", f"terminal_{semantic or action_state}"
        if verdict != "clean":
            return "neutral", f"audit_{verdict or 'unverified'}"

        verified_completion = (
            (
                execution_result.get("schema") == "ExecutionResultV1"
                and str(
                    execution_result.get("terminal_outcome") or ""
                ).lower() == "succeeded"
            )
            or semantic == "verified"
            or (
                semantic == "completed"
                and (
                    action_state in {"verified", "completed"}
                    or verification == "verified"
                )
            )
        )
        if verified_completion:
            return "success", "verified_completion"
        if semantic == "completed":
            return "neutral", "completed_without_verification"
        return "neutral", "unknown_semantic_outcome"

    async def record_outcome(self, job: Any, report: Dict[str, Any],
                             verdict: str, *, outcome: Optional[str] = None,
                             latency: Optional[float] = None,
                             attempts: int = 0,
                             event_id: Optional[str] = None,
                             event_mode: Optional[str] = None,
                             success_attested: bool = False) -> Dict[str, Any]:
        """Fold an audited completion into the trust/accountability layer."""
        report = report or {}
        mode = (
            str(event_mode).strip().lower()
            if str(event_mode or "").strip().lower() in {"off", "shadow", "live"}
            else workers_mode()
        )
        if mode == "off":
            return {"recorded": False, "reason": "governor_off"}
        jt = self._job_type_value(job)
        domain = self.trust_domain(jt)
        shadow = mode != "live"

        if outcome is None:
            outcome, outcome_reason = self.classify_completion_outcome(
                report, verdict
            )
        else:
            outcome = str(outcome).strip().lower()
            if outcome not in {"success", "failure", "neutral"}:
                outcome = "neutral"
            outcome_reason = "explicit_outcome"
            if outcome == "success":
                derived, derived_reason = self.classify_completion_outcome(
                    report, verdict
                )
                if derived != "success":
                    outcome = derived
                    outcome_reason = f"rejected_explicit_success:{derived_reason}"

        stated = report.get("confidence")
        try:
            stated = float(stated) if stated is not None else None
        except (TypeError, ValueError):
            stated = None

        # Self-model / trust engine: the earned-autonomy evidence. Competence
        # and its deterministic trust transition are separate durable stages:
        # a replayed event re-runs trust reconciliation without duplicating
        # the immutable competence row.
        duplicate = False
        if outcome != "neutral" and self._self_model is None and event_id:
            raise RuntimeError("worker competence store is unavailable")
        if outcome != "neutral" and self._self_model is not None:
            try:
                recorded = self._self_model.record(
                    domain, outcome, latency_secs=latency,
                    shadow=shadow, violation=(verdict == "violation"),
                    stated_confidence=stated,
                    source="task_queue.governor",
                    source_ref=self._job_id(job),
                    event_key=event_id,
                    # A worker report is observed evidence. It is never
                    # promoted to externally verified merely because the
                    # worker wrote status="verified" in its own payload.
                    evidence_status=(
                        "verified"
                        if outcome == "success" and success_attested
                        else "observed"
                    ),
                    outcome_contract="colony.worker-outcome/v1",
                    evidence={
                        "schema": "colony.worker-outcome/v1",
                        "audit_verdict": verdict,
                        "classification": outcome_reason,
                        "attempts": max(0, int(attempts or 0)),
                        "event_id": event_id,
                        "success_attested": bool(success_attested),
                        "effectful": bool(job_declares_effect(job)),
                    },
                    defer_trust=True,
                )
                if event_id and recorded is False:
                    store = getattr(self._self_model, "store", None)
                    if (
                        store is not None
                        and hasattr(store, "has_event_key")
                        and store.has_event_key(
                            "task_queue.governor", event_id,
                        )
                    ):
                        duplicate = True
                    else:
                        raise RuntimeError(
                            "worker competence event was not durably recorded"
                        )
                if hasattr(self._self_model, "reconcile_trust"):
                    self._self_model.reconcile_trust(domain)
                else:
                    trust = getattr(self._self_model, "trust", None)
                    if trust is not None:
                        trust.after_outcome(domain)
            except Exception:
                logger.warning(
                    "worker self-model record failed",
                    exc_info=True,
                )
                raise

        # Worker self-reports are never owner feedback. TypeFeedbackStore is
        # reserved for actual owner reactions and must not amplify a worker's
        # own success claim into the trust confidence multiplier.

        journal_id = self._journal(
            domain,
            f"job {self._job_id(job)} ({jt}) completed",
            reasoning=(
                f"audit verdict: {verdict}; semantic outcome: {outcome} "
                f"({outcome_reason})"
            ),
            confidence=stated,
            reversibility="recoverable" if self._job_authorized_to_mutate(job)
                          else "reversible",
            decision="noted", outcome=outcome, ref=self._job_id(job),
            event_key=event_id,
        )
        if event_id and journal_id < 0:
            raise RuntimeError("worker action journal is unavailable")

        # Owner-facing safety notice precedes optional LLM distillation and
        # carries a stable proposal ID so crash-window retries are deduplicable
        # downstream.  Its own durable receipt lets a failed first delivery be
        # retried while suppressing routine outbox replays after success.
        if verdict == "violation" and self._deliver is not None:
            receipt_key = (
                f"{event_id}:violation-notice-delivered"
                if event_id else None
            )
            journal = getattr(self._self_model, "journal", None)
            already_delivered = bool(
                receipt_key
                and journal is not None
                and hasattr(journal, "has_event_key")
                and journal.has_event_key(receipt_key)
            )
            if not already_delivered:
                await self._notify_violation(job, report, event_id=event_id)
                if receipt_key:
                    receipt_id = self._journal(
                        domain,
                        f"violation notice delivered for worker event "
                        f"{event_id}",
                        reasoning="durable worker-notice delivery receipt",
                        decision="noted",
                        outcome="delivered",
                        ref=self._job_id(job),
                        event_key=receipt_key,
                    )
                    if receipt_id < 0:
                        raise RuntimeError(
                            "worker violation delivery receipt unavailable"
                        )

        # The durable queue outbox may replay an event after a timeout or
        # restart.  Trust reconciliation and the violation receipt above are
        # intentionally repeatable; LLM skill distillation is not.
        if duplicate:
            return {
                "recorded": False,
                "duplicate": True,
                "event_id": event_id,
            }

        # Skill distillation on genuine successes (retry-success / novel).
        if outcome == "success" and verdict != "violation":
            await self._maybe_distill(job, report, attempts)
        return {
            "recorded": not duplicate,
            "duplicate": duplicate,
            "event_id": event_id,
        }

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

    async def _notify_violation(
        self,
        job: Any,
        report: Dict[str, Any],
        *,
        event_id: Optional[str] = None,
    ) -> None:
        try:
            from colony_sidecar.proposals import Proposal, proposal_to_payload
            findings = "; ".join(
                self.audit_report(job, report).get("findings", [])[:5])
            notice_key = str(
                event_id or f"worker-violation:{self._job_id(job)}"
            )
            prop = Proposal(
                id=("prop-worker-" + hashlib.sha256(
                    notice_key.encode()
                ).hexdigest()[:16]),
                title=f"Worker scope violation on job {self._job_id(job)}",
                finding=(f"A queue worker exceeded its authorized scope: "
                         f"{findings}. I flagged and recorded it; the job "
                         "type has been demoted from autonomous execution."),
                why_it_helps="protects you from an over-reaching worker",
                suggested_action="Review the flagged job and worker",
                source=self._job_id(job), initiative_type="proposal",
                confidence=0.95)
            if self._proposal_store is not None:
                insert_once = getattr(
                    self._proposal_store, "add_if_absent", None,
                )
                if callable(insert_once):
                    insert_once(prop)
                else:
                    self._proposal_store.add(prop)
            delivered = await self._deliver(proposal_to_payload(prop))
            if delivered is not True:
                raise RuntimeError("worker violation notice was not delivered")
        except Exception:
            logger.debug("worker violation notify failed", exc_info=True)
            raise

    # -- observability ----------------------------------------------------
    def status(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "mode": workers_mode(),
            "boundary_required": self._boundary_required,
            "directive_dependency_available": self._directives is not None,
            "ready_for_live_claims": self.ready_for_live_claims(),
        }
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
    def _journal(self, domain: str, description: str, **kw: Any) -> int:
        journal = getattr(self._self_model, "journal", None)
        if journal is None:
            return -1
        try:
            return int(journal.record(domain, description, **kw))
        except Exception:
            logger.debug("worker journal failed", exc_info=True)
            return -1

    @staticmethod
    def _job_id(job: Any) -> str:
        return str(getattr(job, "job_id", "") or getattr(job, "id", "") or "?")

    @staticmethod
    def _job_type_value(job: Any) -> str:
        jt = getattr(job, "job_type", "custom")
        return jt.value if hasattr(jt, "value") else str(jt)
