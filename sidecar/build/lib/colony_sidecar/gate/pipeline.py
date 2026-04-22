"""ResponseGate — seven-layer response gate pipeline orchestrator."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Optional

# Dedicated audit logger for security-relevant gate events (SEC-13-L-03)
_audit_log = logging.getLogger("colony.gate.audit")

from colony_sidecar.gate.models import GateDecision, GatePayload, GateResultCode, DispatchResult
from colony_sidecar.gate.layers.l1_recipient import RecipientVerifier
from colony_sidecar.gate.layers.l2_pii import PIIScanner
from colony_sidecar.gate.layers.l3_cross_context import CrossContextDetector
from colony_sidecar.gate.layers.l4_trust_tier import TrustTierChecker
from colony_sidecar.gate.layers.l5_injection import InjectionDetector
from colony_sidecar.gate.layers.l6_review import SecondaryReviewer
from colony_sidecar.gate.layers.l7_delay import SendDelayGate, PendingDispatchStore

logger = logging.getLogger(__name__)


class ResponseGate:
    """Seven-layer response gate pipeline.

    Usage::

        gate = ResponseGate(config, session_store, audit_log)
        result = await gate.evaluate(payload)
        if not result.blocked:
            await dispatch(payload)
    """

    def __init__(
        self,
        config: Any,
        session_store: Any,
        audit_log: logging.Logger,
        secondary_reviewer: Optional[SecondaryReviewer] = None,
        dispatch_store: Optional[PendingDispatchStore] = None,
    ) -> None:
        """Initialize the ResponseGate pipeline.

        Args:
            config:              Gate configuration object (GateConfig or compatible).
            session_store:       Session store used by L1 and L3 layers.
            audit_log:           Logger to record gate decisions for the audit trail.
            secondary_reviewer:  Optional override for L6; defaults to SecondaryReviewer(config).
            dispatch_store:      Optional persistent store for L7 delayed dispatches.
        """
        self._config = config
        self._l1 = RecipientVerifier(session_store)
        self._l2 = PIIScanner(config)
        self._l3 = CrossContextDetector(session_store, config)
        self._l4 = TrustTierChecker(config)
        self._l5 = InjectionDetector(config)
        self._l6 = secondary_reviewer or SecondaryReviewer(config)
        self._l7 = SendDelayGate(config, dispatch_store)
        self._audit = audit_log

    async def evaluate(self, payload: GatePayload) -> GateDecision:
        """Run all gate layers. Returns decision; does NOT dispatch."""
        response_hash = hashlib.sha256(payload.response_text.encode()).hexdigest()

        layer_results: dict = {
            "_session_id": payload.session_id,
            "_contact_id": payload.target_contact_id,
            "_sensitivity": self._config.sensitivity,
            "_overrides_applied": [],
            "_response_hash": response_hash,
        }

        # Check per-contact overrides
        override = self._config.get_contact_override(payload.target_contact_id)
        bypass_layers: list[int] = []
        if override:
            raw_bypass = override.get("bypass_layers", [])
            layer_results["_overrides_applied"] = [f"contact:{payload.target_contact_id}"]
            # Layer 1 can NEVER be bypassed
            bypass_layers = [layer_id for layer_id in raw_bypass if layer_id != 1]

            # Emit structured audit entry for each active bypass (SEC-13-L-03)
            for layer_id in bypass_layers:
                _audit_log.warning(
                    "gate_layer_bypassed",
                    extra={
                        "contact_id": payload.target_contact_id,
                        "layer_id": layer_id,
                        "override_source": override.get("source", "unknown"),
                        "message_id": payload.turn_id,
                        "timestamp": time.time(),
                    },
                )

        # Layers 1–5: deterministic, short-circuit on first block
        deterministic_layers = [
            (1, self._l1),
            (2, self._l2),
            (3, self._l3),
            (4, self._l4),
            (5, self._l5),
        ]
        for layer_num, checker in deterministic_layers:
            if layer_num in bypass_layers:
                # GATE-01: audit log every operator bypass exercise
                logger.warning(
                    "GATE_OPERATOR_BYPASS: layer=%d session_id=%s contact_id=%s",
                    layer_num,
                    payload.session_id,
                    payload.target_contact_id,
                )
                layer_results[f"layer_{layer_num}"] = {"bypassed": True}
                continue
            result = await checker.check(payload)
            layer_results[f"layer_{layer_num}"] = {
                "blocked": result.blocked,
                "code": result.code,
                "reason": result.reason,
                "suspicious": result.suspicious,
            }
            if result.blocked:
                decision = GateDecision(
                    payload_turn_id=payload.turn_id,
                    result_code=GateResultCode(result.code),
                    blocked=True,
                    blocking_layer=layer_num,
                    block_reason=result.reason,
                    flagged_excerpt=result.flagged_excerpt,
                    layer_results=layer_results,
                )
                await self._audit.record(decision)
                return decision

        # Layer 6: secondary LLM review (soft flag only)
        l5_result = layer_results.get("layer_5", {})
        injection_suspicious = l5_result.get("suspicious", False) if isinstance(l5_result, dict) else False

        if self._config.enable_secondary_review and 6 not in bypass_layers:
            l6_result = await self._l6.review(
                payload, injection_suspicious=injection_suspicious
            )
            layer_results["layer_6"] = {
                "flagged": l6_result.flagged,
                "category": l6_result.category,
            }
            if l6_result.flagged:
                decision = GateDecision(
                    payload_turn_id=payload.turn_id,
                    result_code=GateResultCode.BLOCK_REVIEW,
                    blocked=True,
                    blocking_layer=6,
                    block_reason="secondary_review_flagged",
                    flagged_excerpt=None,
                    layer_results=layer_results,
                )
                await self._audit.record(decision)
                return decision
        else:
            layer_results["layer_6"] = {"skipped": True}

        # Layer 7: send delay
        if 7 not in bypass_layers:
            l7_result = await self._l7.hold(payload)
            layer_results["layer_7"] = {
                "cancelled": l7_result.cancelled,
                "pending_id": l7_result.pending_id,
            }
            if l7_result.cancelled:
                decision = GateDecision(
                    payload_turn_id=payload.turn_id,
                    result_code=GateResultCode.PENDING_DELAY,
                    blocked=True,
                    blocking_layer=7,
                    block_reason=l7_result.cancel_reason or "async_cancellation_during_delay",
                    flagged_excerpt=None,
                    layer_results=layer_results,
                )
                await self._audit.record(decision)
                return decision
        else:
            layer_results["layer_7"] = {"bypassed": True}

        # All layers passed
        decision = GateDecision(
            payload_turn_id=payload.turn_id,
            result_code=GateResultCode.PASS,
            blocked=False,
            blocking_layer=None,
            block_reason=None,
            flagged_excerpt=None,
            layer_results=layer_results,
        )
        await self._audit.record(decision)
        return decision
