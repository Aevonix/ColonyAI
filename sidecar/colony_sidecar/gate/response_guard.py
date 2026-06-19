"""ResponseGuard — the rebuilt outbound response gate for the messaging path.

A focused replacement for the dormant 7-layer ``ResponseGate`` (which had a dead
cross-context layer, a recipient layer that blocks anything without a pre-registered
session, and an LLM review + send delay that don't belong in a live chat hot path).

ResponseGuard runs a small set of FAST, deterministic checks plus a provenance-based
cross-context leak check, in one of two modes:

  * ``shadow``  — evaluate and report findings, but never change the outcome (ALLOW).
                  Used to observe real traffic and tune before enforcing.
  * ``enforce`` — a blocking finding yields REVISE (caller regenerates once, then
                  suppresses on repeat).

Contract guarantees:
  * **Fail-open.** Any internal error returns ALLOW. A gate fault must never silence
    the agent.
  * **Configurable gateway exclusion.** A deployment can name gateways that must never
    be gated (e.g. a low-latency real-time / voice path) via ``excluded_gateways``; a
    reply on an excluded gateway returns ALLOW. The list of excluded gateways is supplied
    by the deployment, never baked into Colony.

The deterministic checks reuse the existing layer implementations (PII, trust tier,
injection); the cross-context check is an injected, provenance-backed dependency
(``cross_context``) so this module stays decoupled from the memory/provenance layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, List, Optional, Sequence

from colony_sidecar.gate.config import GateConfig
from colony_sidecar.gate.layers.l2_pii import PIIScanner
from colony_sidecar.gate.layers.l4_trust_tier import TrustTierChecker
from colony_sidecar.gate.layers.l5_injection import InjectionDetector
from colony_sidecar.gate.models import GatePayload
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

    @property
    def blocked(self) -> bool:
        return self.decision in (GuardDecision.REVISE.value, GuardDecision.BLOCK.value)

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "mode": self.mode,
            "findings": [
                {"check": f.check, "severity": f.severity, "reason": f.reason,
                 "excerpt": f.excerpt}
                for f in self.findings
            ],
        }


class CrossContextGuard:
    """Interface for the provenance-based cross-context leak check (filled in by the
    memory/provenance layer). A no-op default so ResponseGuard works before it lands."""

    async def check(self, *, response_text: str, conversation_key: Optional[str],
                    mentioned_entities: Sequence[str]) -> List[GuardFinding]:
        return []


class ResponseGuard:
    def __init__(self, config: Optional[GateConfig] = None,
                 cross_context: Optional[CrossContextGuard] = None,
                 default_mode: GuardMode = GuardMode.SHADOW,
                 excluded_gateways: Optional[Iterable[str]] = None) -> None:
        self._config = config or GateConfig()
        # Gateways the embedding deployment never wants gated (e.g. its voice path).
        # Empty by default — Colony ships no deployment-specific gateway names.
        self._excluded = frozenset((g or "").lower() for g in (excluded_gateways or ()))
        self._pii = PIIScanner(self._config)
        self._tier = TrustTierChecker(self._config)
        try:
            self._injection: Optional[InjectionDetector] = InjectionDetector(self._config)
        except Exception as exc:   # ruleset load failure must not break the guard
            logger.warning("ResponseGuard: injection detector unavailable: %s", exc)
            self._injection = None
        self._cross = cross_context
        self._default_mode = default_mode

    async def evaluate(
        self,
        *,
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
    ) -> GuardResult:
        mode = mode or self._default_mode
        # Deployment-configured gateway exclusion (e.g. a real-time / voice path).
        if self._excluded and (target_gateway or "").lower() in self._excluded:
            return GuardResult(decision=GuardDecision.ALLOW.value, mode=str(getattr(mode, "value", mode)))
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
            )
            findings: List[GuardFinding] = []

            findings += await self._run_check("secret_leak", "block", self._pii, payload)
            findings += await self._run_check("disclosure_tier", "block", self._tier, payload)
            if self._injection is not None:
                findings += await self._run_check("injection", "warn", self._injection, payload)
            if self._cross is not None:
                try:
                    findings += list(await self._cross.check(
                        response_text=response_text or "", conversation_key=conversation_key,
                        mentioned_entities=list(mentioned_entities or [])))
                except Exception as exc:
                    logger.warning("ResponseGuard: cross_context check failed (skipped): %s", exc)

            decision = self._decide(findings, mode)
            result = GuardResult(decision=decision, mode=str(getattr(mode, "value", mode)), findings=findings)
            if findings:
                _guard_audit.info(
                    "guard mode=%s decision=%s contact=%s turn=%s findings=%s",
                    result.mode, decision, target_contact_id, turn_id,
                    [f"{f.check}:{f.severity}" for f in findings],
                )
            return result
        except Exception as exc:
            logger.warning("ResponseGuard error (fail-open ALLOW): %s", exc)
            return GuardResult(decision=GuardDecision.ALLOW.value, mode=str(getattr(mode, "value", mode)))

    async def _run_check(self, name: str, severity: str, checker: Any,
                         payload: GatePayload) -> List[GuardFinding]:
        try:
            r = await checker.check(payload)
        except Exception as exc:   # a single broken check fails open (skipped), not the guard
            logger.warning("ResponseGuard: check %s failed (skipped): %s", name, exc)
            return []
        if getattr(r, "blocked", False):
            return [GuardFinding(check=name, severity=severity,
                                 reason=getattr(r, "reason", name),
                                 excerpt=getattr(r, "flagged_excerpt", None))]
        return []

    @staticmethod
    def _decide(findings: List[GuardFinding], mode: GuardMode) -> str:
        if getattr(mode, "value", mode) == GuardMode.SHADOW.value:
            return GuardDecision.ALLOW.value          # shadow never changes the outcome
        if any(f.severity == "block" for f in findings):
            return GuardDecision.REVISE.value         # caller regenerates once, then suppresses
        return GuardDecision.ALLOW.value
