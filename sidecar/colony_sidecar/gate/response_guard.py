"""ResponseGuard — the rebuilt outbound response gate for the messaging path.

A focused replacement for the dormant 7-layer ``ResponseGate`` (which had a dead
cross-context layer, a recipient layer that blocks anything without a pre-registered
session, and an LLM review + send delay that don't belong in a live chat hot path).

ResponseGuard runs a small set of FAST, deterministic checks plus a provenance-based
cross-context leak check, in one of two modes on an explicit outbound surface:

  * ``shadow``  — evaluate and report findings, but never change the outcome (ALLOW).
                  Used to observe real traffic and tune before enforcing.
  * ``enforce`` — a blocking finding yields REVISE (caller regenerates once, then
                  suppresses on repeat).

Contract guarantees:
  * shadow evaluation remains fail-open and observational;
  * enforce evaluation on a guarded text/artifact surface fails closed when a
    configured check is unavailable;
  * real-time speech is excluded by exact surface type, never by a caller-chosen
    gateway label; and
  * a request may strengthen shadow to enforce but cannot weaken configured enforce.

The deterministic checks reuse the existing layer implementations (PII, trust tier,
injection); the cross-context check is an injected, provenance-backed dependency
(``cross_context``) so this module stays decoupled from the memory/provenance layer.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any, Dict, Iterable, List, Optional, Sequence

from colony_sidecar.gate.config import GateConfig
from colony_sidecar.gate.communication_policy import CommunicationPolicyContextV1
from colony_sidecar.gate.layers.l2_pii import PIIScanner
from colony_sidecar.gate.layers.l4_trust_tier import TrustTierChecker
from colony_sidecar.gate.layers.l5_injection import InjectionDetector
from colony_sidecar.gate.models import GatePayload
from colony_sidecar.gate.surface_policy import (
    POLICY_DIGEST,
    ResponseGuardSurfacePolicyV1,
    SurfacePolicyDecisionV1,
)
from colony_sidecar.intelligence.relationships.trust_tiers import TrustTier

logger = logging.getLogger(__name__)
_guard_audit = logging.getLogger("colony.gate.guard")

# Contact-layer tiers that the 5-value gate enum does not carry map to the nearest
# gate tier so disclosure gating still applies (acquaintance/unknown -> peripheral).
_CONTACT_TO_GATE_TIER = {
    "acquaintance": TrustTier.PERIPHERAL,
    "unknown": TrustTier.PERIPHERAL,
}


def to_gate_tier(value: Any) -> TrustTier:
    """Coerce a tier (TrustTier or contact-layer string) to a gate TrustTier."""
    if isinstance(value, TrustTier):
        return value
    try:
        return TrustTier(value)
    except (ValueError, TypeError):
        return _CONTACT_TO_GATE_TIER.get(str(value), TrustTier.REGULAR)


class GuardMode(str, Enum):
    SHADOW = "shadow"
    ENFORCE = "enforce"


def enforce_allowlist() -> Optional[frozenset]:
    """Checks allowed to BLOCK in enforce mode (per-check enforce ramp).

    COLONY_GUARD_ENFORCE_CHECKS, default ``secret_leak,tom2_epistemic`` —
    the lowest false-positive checks go first; everything else keeps shadow
    semantics (observed, audited, never suppressing) until explicitly
    added. ``tom2_epistemic`` ships allowlisted because it is INERT until a
    level-2 injection registers a taint (zero findings, zero false
    positives, on all other traffic). It is one level-2 prerequisite, but is
    not sufficient without a receipt-backed applied-output evidence probe.
    ``all`` / ``*`` restores full enforcement across every check (legacy).
    Returns None for "all checks", else a frozenset of check names.
    """
    raw = os.environ.get("COLONY_GUARD_ENFORCE_CHECKS",
                         "secret_leak,tom2_epistemic").strip()
    if raw.lower() in ("all", "*"):
        return None
    return frozenset(c.strip() for c in raw.split(",") if c.strip())


def breaker_enabled() -> bool:
    """COLONY_GUARD_BREAKER, default on. off/0/false disables the breaker."""
    return os.environ.get("COLONY_GUARD_BREAKER", "on").strip().lower() not in (
        "off", "0", "false", "no")


def breaker_trip_blocks() -> int:
    """Enforce blocks within 24h that trip the breaker (default 10).
    <=0 disables tripping."""
    try:
        return int(os.environ.get("COLONY_GUARD_TRIP_BLOCKS", "10"))
    except (TypeError, ValueError):
        return 10


class GuardDecision(str, Enum):
    ALLOW = "allow"
    REVISE = "revise"
    BLOCK = "block"


@dataclass
class GuardFinding:
    check: str           # secret_leak | disclosure_tier | injection | cross_context
    severity: str        # "block" (would suppress in enforce) | "warn" (advisory)
    reason: str
    excerpt: Optional[str] = None


@dataclass
class GuardResult:
    decision: str
    mode: str
    findings: List[GuardFinding] = field(default_factory=list)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    surface: str = "text_chat"
    surface_family: str = "text"
    applicability: str = "guarded"
    guard_status: str = "evaluated"
    policy_id: str = "response-guard-surface-policy-v1"
    policy_digest: str = POLICY_DIGEST
    candidate_digest: str = ""
    communication_policy_digest: Optional[str] = None
    communication_context_digest: Optional[str] = None

    @property
    def blocked(self) -> bool:
        return self.decision in (GuardDecision.REVISE.value, GuardDecision.BLOCK.value)

    def to_dict(self) -> dict:
        result = {
            "decision": self.decision,
            "mode": self.mode,
            "surface": self.surface,
            "surface_family": self.surface_family,
            "applicability": self.applicability,
            "guard_status": self.guard_status,
            "policy_id": self.policy_id,
            "policy_digest": self.policy_digest,
            "candidate_digest": self.candidate_digest,
            "findings": [
                {"check": f.check, "severity": f.severity, "reason": f.reason,
                 "excerpt": f.excerpt}
                for f in self.findings
            ],
        }
        # Preserve the legacy exact result shape when no communication policy
        # was supplied.  New clients opt into exactly two binding fields.
        if self.communication_policy_digest is not None:
            result["communication_policy_digest"] = self.communication_policy_digest
            result["communication_context_digest"] = self.communication_context_digest
        return result


def _result_for_surface(
    surface_decision: SurfacePolicyDecisionV1,
    *,
    decision: str,
    findings: List[GuardFinding],
    guard_status: str,
    candidate_digest: str,
    communication_policy: Optional[CommunicationPolicyContextV1] = None,
) -> GuardResult:
    if decision not in {item.value for item in GuardDecision}:
        raise ValueError("invalid guard result decision")
    return GuardResult(
        decision=decision,
        mode=surface_decision.effective_mode,
        findings=findings,
        surface=surface_decision.surface,
        surface_family=surface_decision.family,
        applicability=surface_decision.disposition,
        guard_status=guard_status,
        policy_id=surface_decision.policy_id,
        policy_digest=surface_decision.policy_digest,
        candidate_digest=candidate_digest,
        communication_policy_digest=(
            communication_policy.policy_digest
            if communication_policy is not None
            else None
        ),
        communication_context_digest=(
            communication_policy.context_digest
            if communication_policy is not None
            else None
        ),
    )


def response_text_digest(response_text: Any) -> str:
    """Stable identity of the exact Unicode candidate evaluated/released."""

    return sha256(str(response_text or "").encode("utf-8")).hexdigest()


def unavailable_guard_result(
    *,
    surface: str,
    configured_mode: Any,
    requested_mode: Optional[Any] = None,
    response_text: str = "",
    surface_policy: Optional[ResponseGuardSurfacePolicyV1] = None,
    communication_policy: Optional[CommunicationPolicyContextV1] = None,
) -> GuardResult:
    """Return the deterministic result for a configured guard outage.

    Shadow text/artifact checks remain observational and allow. Enforce
    text/artifact checks block. Speech remains excluded and is never routed
    through guard evaluation.
    """
    policy = surface_policy or ResponseGuardSurfacePolicyV1()
    surface_decision = policy.resolve(
        surface,
        configured_mode=configured_mode,
        requested_mode=requested_mode,
    )
    if not surface_decision.guarded:
        return _result_for_surface(
            surface_decision,
            decision=GuardDecision.ALLOW.value,
            findings=[],
            guard_status="bypassed",
            candidate_digest=response_text_digest(response_text),
            communication_policy=communication_policy,
        )
    enforce = surface_decision.effective_mode == GuardMode.ENFORCE.value
    return _result_for_surface(
        surface_decision,
        decision=(
            GuardDecision.BLOCK.value
            if enforce else GuardDecision.ALLOW.value
        ),
        findings=[GuardFinding(
            check="guard_unavailable",
            severity="block" if enforce else "warn",
            reason="response guard unavailable",
        )],
        guard_status="degraded",
        candidate_digest=response_text_digest(response_text),
        communication_policy=communication_policy,
    )


class CrossContextGuard:
    """Interface for the provenance-based cross-context leak check (filled in by the
    memory/provenance layer). A no-op default so ResponseGuard works before it lands."""

    async def check(self, *, response_text: str, conversation_key: Optional[str],
                    mentioned_entities: Sequence[str],
                    communication_policy: Optional[
                        CommunicationPolicyContextV1
                    ] = None) -> List[GuardFinding]:
        return []


class ResponseGuard:
    def __init__(self, config: Optional[GateConfig] = None,
                 cross_context: Optional[CrossContextGuard] = None,
                 default_mode: GuardMode = GuardMode.SHADOW,
                 excluded_gateways: Optional[Iterable[str]] = None,
                 audit_store: Optional[Any] = None,
                 tom2_epistemic: Optional[Any] = None,
                 surface_policy: Optional[ResponseGuardSurfacePolicyV1] = None) -> None:
        self._config = config or GateConfig()
        self._audit = audit_store
        # Retained only as migration telemetry. Gateway names are not authority
        # to bypass a content guard; exact SurfacePolicyV1 classification is.
        self._legacy_excluded_gateways = frozenset(
            (g or "").lower() for g in (excluded_gateways or ())
        )
        if self._legacy_excluded_gateways:
            logger.warning(
                "ResponseGuard ignored legacy excluded_gateways=%s; "
                "use an exact outbound surface",
                sorted(self._legacy_excluded_gateways),
            )
        self._surface_policy = surface_policy or ResponseGuardSurfacePolicyV1()
        self._pii = PIIScanner(self._config)
        self._tier = TrustTierChecker(self._config)
        try:
            self._injection: Optional[InjectionDetector] = InjectionDetector(self._config)
        except Exception as exc:   # ruleset load failure must not break the guard
            logger.warning("ResponseGuard: injection detector unavailable: %s", exc)
            self._injection = None
        self._cross = cross_context
        # tom2 epistemic egress net (L3.2): injected like cross_context so
        # this module stays decoupled from the taint/tom layer. Inert (zero
        # findings) whenever no level-2 injection taint is live.
        self._tom2_epistemic = tom2_epistemic
        self._default_mode = default_mode
        # Circuit breaker state: timestamps of enforce-mode blocks (24h
        # rolling window). The breaker only ever WEAKENS enforcement (open =
        # fall back to shadow semantics); it can never latch INTO enforce.
        self._block_times: deque = deque()

    async def evaluate(
        self,
        *,
        surface: str,
        response_text: str,
        incoming_message_text: str = "",
        trust_tier: Any = TrustTier.REGULAR,
        target_contact_id: str = "",
        target_gateway: str = "",
        session_id: str = "",
        turn_id: str = "",
        mentioned_entities: Optional[Sequence[str]] = None,
        conversation_key: Optional[str] = None,
        mode: Optional[GuardMode] = None,
        authorized: bool = False,
        communication_policy: Optional[CommunicationPolicyContextV1] = None,
    ) -> GuardResult:
        if communication_policy is not None:
            if not isinstance(communication_policy, CommunicationPolicyContextV1):
                raise TypeError(
                    "communication_policy must be CommunicationPolicyContextV1"
                )
            # Pydantic's trusted ``model_construct``/``model_copy(update=...)``
            # APIs can create an instance without field validation.  Re-parse
            # the canonical wire mapping at the effect boundary rather than
            # treating isinstance() as sufficient attestation.
            communication_policy = CommunicationPolicyContextV1.model_validate(
                communication_policy.canonical_dict()
            )
            if target_contact_id != communication_policy.target_contact_id:
                raise ValueError(
                    "communication policy target must match target_contact_id"
                )
        surface_decision = self._surface_policy.resolve(
            surface,
            configured_mode=self._default_mode,
            requested_mode=mode,
        )
        if not surface_decision.guarded:
            result = self._surface_result(
                surface_decision,
                decision=GuardDecision.ALLOW.value,
                findings=[],
                guard_status="bypassed",
                response_text=response_text,
                communication_policy=communication_policy,
            )
            self._audit_communication_policy(
                result,
                communication_policy=communication_policy,
                conversation_key=conversation_key,
                target_contact_id=target_contact_id,
                target_gateway=target_gateway,
                authorized=authorized,
                response_text=response_text,
            )
            return result
        effective_mode = GuardMode(surface_decision.effective_mode)
        try:
            tier = to_gate_tier(trust_tier)
            payload = GatePayload(
                response_text=response_text or "",
                target_contact_id=target_contact_id,
                target_gateway=target_gateway,
                session_id=session_id,
                trust_tier=tier,
                mentioned_entities=frozenset(mentioned_entities or []),
                turn_id=turn_id,
                incoming_message_text=incoming_message_text or "",
                communication_policy=communication_policy,
            )
            findings: List[GuardFinding] = []
            unavailable: List[str] = []

            observed, failed = await self._run_check(
                "secret_leak", "block", self._pii, payload,
            )
            findings += observed
            if failed:
                unavailable.append("secret_leak")
            observed, failed = await self._run_check(
                "disclosure_tier", "block", self._tier, payload,
            )
            findings += observed
            if failed:
                unavailable.append("disclosure_tier")
            if self._injection is not None:
                observed, failed = await self._run_check(
                    "injection", "warn", self._injection, payload,
                )
                findings += observed
                if failed:
                    unavailable.append("injection")
            else:
                # Configured-but-absent: the guard always configures the
                # injection detector in __init__; None means its ruleset
                # failed to load. Enforce must fail closed on that gap,
                # shadow observes it (warn finding), never a silent skip.
                unavailable.append("injection")
            if self._cross is not None:
                try:
                    cross_kwargs = {
                        "response_text": response_text or "",
                        "conversation_key": conversation_key,
                        "mentioned_entities": list(mentioned_entities or []),
                    }
                    # Legacy check implementations retain their exact call
                    # shape for legacy evaluations.  A policy-bound evaluation
                    # requires a checker to accept the immutable policy input;
                    # refusal becomes configured-check unavailability.
                    if communication_policy is not None:
                        cross_kwargs["communication_policy"] = communication_policy
                    findings += list(await self._cross.check(**cross_kwargs))
                except Exception as exc:
                    logger.warning("ResponseGuard: cross_context check failed: %s", exc)
                    unavailable.append("cross_context")
            if self._tom2_epistemic is not None:
                try:
                    findings += list(await self._tom2_epistemic.check(
                        response_text=response_text or "",
                        conversation_key=conversation_key))
                except Exception as exc:
                    logger.warning("ResponseGuard: tom2_epistemic check failed: %s", exc)
                    unavailable.append("tom2_epistemic")

            if unavailable:
                findings.append(GuardFinding(
                    check="guard_unavailable",
                    severity=(
                        "block"
                        if effective_mode == GuardMode.ENFORCE
                        else "warn"
                    ),
                    reason="configured checks unavailable: " + ",".join(unavailable),
                ))

            # Owner-directed (authorized) cross-context transfers are legitimate, not leaks:
            # downgrade them so enforce never blocks them, while still keeping the audit row.
            cross = [f for f in findings if f.check == "cross_context"]
            if authorized:
                for f in cross:
                    f.severity = "info"
                    f.reason = "authorized owner-directed transfer: " + f.reason

            fatal_unavailable = bool(
                unavailable and effective_mode == GuardMode.ENFORCE
            )
            decision = (
                GuardDecision.BLOCK.value
                if fatal_unavailable
                else self._decide(findings, effective_mode)
            )
            # Circuit breaker (fails open, never INTO enforce): repeated
            # enforce blocks in a 24h window suspend suppression — the guard
            # keeps evaluating and auditing, but allows, until the window
            # slides. A breaker fault leaves the decision unchanged.
            if decision != GuardDecision.ALLOW.value and not fatal_unavailable:
                try:
                    if self._breaker_open():
                        logger.warning(
                            "ResponseGuard breaker OPEN (%d blocks/24h >= %d)"
                            " — enforce suspended, allowing (would be %s)",
                            len(self._block_times), breaker_trip_blocks(),
                            decision)
                        decision = GuardDecision.ALLOW.value
                    else:
                        self._block_times.append(time.time())
                except Exception:
                    logger.debug("guard breaker check failed (decision "
                                 "unchanged)", exc_info=True)
            result = self._surface_result(
                surface_decision,
                decision=decision,
                findings=findings,
                guard_status="degraded" if unavailable else "evaluated",
                response_text=response_text,
                communication_policy=communication_policy,
            )
            # Audit trail: count EVERY evaluation (the rate denominator), and record a
            # row for any evaluation with findings or a non-allow decision — all checks,
            # not just cross_context — so per-check rates and the would-block rate can
            # be measured in shadow before enforcement is turned on.
            if self._audit is not None:
                try:
                    self._audit.count_evaluation()
                    if findings or decision != GuardDecision.ALLOW.value:
                        self._audit.record(
                            conversation_key=conversation_key, mode=result.mode, decision=decision,
                            authorized=authorized, checks=[f.check for f in findings],
                            entities=[f.excerpt or "" for f in findings],
                            response_text=response_text or "",
                            would_block=any(f.severity == "block" for f in findings),
                            gateway=target_gateway or None,
                            surface=surface_decision.surface,
                            policy_id=surface_decision.policy_id,
                            policy_digest=surface_decision.policy_digest,
                            guard_status=result.guard_status)
                except Exception:
                    logger.debug("guard audit record failed", exc_info=True)
            self._audit_communication_policy(
                result,
                communication_policy=communication_policy,
                conversation_key=conversation_key,
                target_contact_id=target_contact_id,
                target_gateway=target_gateway,
                authorized=authorized,
                response_text=response_text,
            )
            if findings:
                _guard_audit.info(
                    "guard mode=%s decision=%s contact=%s turn=%s findings=%s",
                    result.mode, decision, target_contact_id, turn_id,
                    [f"{f.check}:{f.severity}" for f in findings],
                )
            return result
        except Exception as exc:
            logger.warning("ResponseGuard evaluation error: %s", exc)
            enforce = effective_mode == GuardMode.ENFORCE
            result = self._surface_result(
                surface_decision,
                decision=(
                    GuardDecision.BLOCK.value
                    if enforce else GuardDecision.ALLOW.value
                ),
                findings=[GuardFinding(
                    check="guard_unavailable",
                    severity="block" if enforce else "warn",
                    reason="response guard evaluation unavailable",
                )],
                guard_status="degraded",
                response_text=response_text,
                communication_policy=communication_policy,
            )
            self._audit_communication_policy(
                result,
                communication_policy=communication_policy,
                conversation_key=conversation_key,
                target_contact_id=target_contact_id,
                target_gateway=target_gateway,
                authorized=authorized,
                response_text=response_text,
            )
            return result

    @staticmethod
    def _surface_result(
        surface_decision: SurfacePolicyDecisionV1,
        *,
        decision: str,
        findings: List[GuardFinding],
        guard_status: str,
        response_text: str,
        communication_policy: Optional[CommunicationPolicyContextV1] = None,
    ) -> GuardResult:
        return _result_for_surface(
            surface_decision,
            decision=decision,
            findings=findings,
            guard_status=guard_status,
            candidate_digest=response_text_digest(response_text),
            communication_policy=communication_policy,
        )

    def _audit_communication_policy(
        self,
        result: GuardResult,
        *,
        communication_policy: Optional[CommunicationPolicyContextV1],
        conversation_key: Optional[str],
        target_contact_id: str,
        target_gateway: str,
        authorized: bool,
        response_text: str,
    ) -> None:
        """Persist a digest-bound row for every policy-bound evaluation.

        This is separate from finding rows so clean constrained messages do
        not inflate ResponseGuard's historical false-positive denominator.
        """

        if communication_policy is None or self._audit is None:
            return
        recorder = getattr(self._audit, "record_communication_policy", None)
        if not callable(recorder):
            return
        try:
            recorder(
                conversation_key=conversation_key,
                mode=result.mode,
                decision=result.decision,
                authorized=authorized,
                gateway=target_gateway or None,
                surface=result.surface,
                surface_policy_id=result.policy_id,
                surface_policy_digest=result.policy_digest,
                response_text=response_text or "",
                guard_status=result.guard_status,
                target_contact_id=target_contact_id,
                communication_policy=communication_policy,
            )
        except Exception:
            logger.debug(
                "communication policy audit record failed", exc_info=True
            )

    async def _run_check(self, name: str, severity: str, checker: Any,
                         payload: GatePayload) -> tuple[List[GuardFinding], bool]:
        try:
            r = await checker.check(payload)
        except Exception as exc:
            logger.warning("ResponseGuard: check %s unavailable: %s", name, exc)
            return [], True
        if getattr(r, "blocked", False):
            return ([GuardFinding(check=name, severity=severity,
                                  reason=getattr(r, "reason", name),
                                  excerpt=getattr(r, "flagged_excerpt", None))], False)
        return [], False

    @staticmethod
    def _decide(findings: List[GuardFinding], mode: GuardMode) -> str:
        if getattr(mode, "value", mode) == GuardMode.SHADOW.value:
            return GuardDecision.ALLOW.value          # shadow never changes the outcome
        # Per-check enforce allowlist: only allowlisted checks may suppress;
        # everything else keeps shadow semantics inside enforce mode.
        allowed = enforce_allowlist()
        for f in findings:
            if f.severity != "block":
                continue
            if allowed is None or f.check in allowed:
                return GuardDecision.REVISE.value     # caller regenerates once, then suppresses
        return GuardDecision.ALLOW.value

    def _breaker_open(self) -> bool:
        """True when the enforce circuit breaker is tripped (24h window)."""
        if not breaker_enabled():
            return False
        threshold = breaker_trip_blocks()
        if threshold <= 0:
            return False
        cutoff = time.time() - 86400.0
        while self._block_times and self._block_times[0] < cutoff:
            self._block_times.popleft()
        return len(self._block_times) >= threshold

    @property
    def configured_mode(self) -> GuardMode:
        """The server-owned default mode used by direct in-process callers."""

        return self._default_mode

    def evidence_probe(self, audit_store: Optional[Any] = None,
                       check: str = "tom2_epistemic",
                       hours: float = 24.0,
                       surface: str = "text_chat"):
        """Return a conservative probe until applied egress receipts exist.

        A guard *evaluation* is not proof that a caller actually withheld or
        revised the evaluated bytes.  The current audit store records verdicts,
        not digest-bound application receipts, so it must never unlock Tom2
        level 2.  The compatibility arguments remain accepted for callers that
        construct this probe, but every result is False until a real egress
        mediator can attest the applied output.
        """

        del audit_store, check, hours, surface

        def probe(gateway: str) -> bool:
            del gateway
            return False

        return probe

    def breaker_status(self) -> Dict[str, Any]:
        """Observability surface for the enforce breaker + allowlist."""
        allowed = enforce_allowlist()
        cutoff = time.time() - 86400.0
        while self._block_times and self._block_times[0] < cutoff:
            self._block_times.popleft()
        return {
            "enabled": breaker_enabled(),
            "trip_blocks": breaker_trip_blocks(),
            "blocks_24h": len(self._block_times),
            "tripped": self._breaker_open(),
            "enforce_checks": (sorted(allowed) if allowed is not None
                               else "all"),
            "surface_policy": self._surface_policy.public(),
        }
