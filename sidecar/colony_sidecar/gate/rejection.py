"""Rejection feedback loop — retry with escalation when gate blocks."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class RejectionNotice:
    """Structured rejection fed back to the LLM for regeneration."""
    notice_id: str
    turn_id: str
    session_id: str
    contact_display_name: str
    block_reason_type: str
    blocking_layer: int
    redacted_excerpt: Optional[str]
    trust_tier: str
    retry_number: int
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    def to_llm_prompt_fragment(self) -> str:
        excerpt_line = ""
        if self.redacted_excerpt:
            excerpt_line = f'\nFlagged content: "{self.redacted_excerpt}"'
        return (
            f"Your response to {self.contact_display_name} was blocked by the "
            f"response gate.\n\n"
            f"Reason: {self.block_reason_type}\n"
            f"Layer: {self.blocking_layer}{excerpt_line}\n\n"
            f"Please generate a new response that does not contain this content. "
            f"The recipient's trust tier is {self.trust_tier}. Adjust accordingly."
        )


@dataclass
class GateRejectionEvent:
    """MetaLearner learning signal for a gate rejection."""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    contact_id: str = ""
    trust_tier: str = ""
    blocking_layer: int = 0
    block_reason: str = ""
    retry_number: int = 0
    eventually_succeeded: bool = False
    turn_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@dataclass
class FeedbackLoopResult:
    passed: bool
    payload: Optional[object]
    attempts: int


def _build_payload(
    response_text: str,
    session,
    prior,
) -> object:
    """Build a new GatePayload for a regenerated response."""
    from colony_sidecar.gate.models import GatePayload
    return GatePayload(
        response_text=response_text,
        target_contact_id=prior.target_contact_id,
        target_gateway=prior.target_gateway,
        session_id=prior.session_id,
        trust_tier=prior.trust_tier,
        mentioned_entities=prior.mentioned_entities,
        turn_id=str(uuid.uuid4()),
        incoming_message_text=prior.incoming_message_text,
    )


class RejectionFeedbackLoop:
    """Manages the retry cycle after a gate block.

    Usage::

        loop = RejectionFeedbackLoop(config, gate, llm, workspace,
                                     metalearner, escalation_manager)
        result = await loop.run(initial_payload, session)
    """

    def __init__(
        self,
        config,
        gate,
        llm,
        workspace,
        metalearner,
        escalation_manager,
        session_store,
    ) -> None:
        self._config = config
        self._gate = gate
        self._llm = llm
        self._ws = workspace
        self._meta = metalearner
        self._escalation = escalation_manager
        self._sessions = session_store

    async def run(self, initial_payload, session) -> FeedbackLoopResult:
        payload = initial_payload
        rejection_event: Optional[GateRejectionEvent] = None
        decision = None

        for attempt in range(self._config.max_retries + 1):
            decision = await self._gate.evaluate(payload)

            if not decision.blocked:
                # Gate passed — re-record last rejection event with eventually_succeeded=True
                if rejection_event:
                    rejection_event.eventually_succeeded = True
                    await self._meta.record_gate_rejection(rejection_event)
                return FeedbackLoopResult(passed=True, payload=payload, attempts=attempt + 1)

            # Gate blocked — create and record rejection event
            rejection_event = GateRejectionEvent(
                session_id=session.session_id,
                contact_id=session.contact_id,
                trust_tier=session.trust_tier.value,
                blocking_layer=decision.blocking_layer,
                block_reason=decision.result_code.value,
                retry_number=attempt,
                turn_id=payload.turn_id,
            )
            await self._meta.record_gate_rejection(rejection_event)

            if attempt >= self._config.max_retries:
                break

            # Build rejection notice and inject into workspace
            contact_name = await self._sessions.get_display_name(session.contact_id)
            notice = RejectionNotice(
                notice_id=str(uuid.uuid4()),
                turn_id=payload.turn_id,
                session_id=session.session_id,
                contact_display_name=contact_name,
                block_reason_type=decision.result_code.value,
                blocking_layer=decision.blocking_layer,
                redacted_excerpt=decision.flagged_excerpt,
                trust_tier=session.trust_tier.value,
                retry_number=attempt + 1,
            )
            self._ws.append_rejection_context(
                reason=notice.block_reason_type,
                redacted_excerpt=notice.redacted_excerpt or "",
            )

            # Regenerate with rejection context
            rejection_prompt = notice.to_llm_prompt_fragment()
            new_response_text = await self._llm.regenerate(
                session=session,
                workspace=self._ws,
                rejection_prompt=rejection_prompt,
            )
            payload = _build_payload(new_response_text, session, payload)

        # Exhausted retries — escalate
        await self._escalation.escalate_response_failure(
            session_id=session.session_id,
            contact_id=session.contact_id,
            contact_display_name=await self._sessions.get_display_name(session.contact_id),
            last_block_reason=decision.result_code.value if decision else "unknown",
            last_blocking_layer=decision.blocking_layer if decision else 0,
            retry_count=self._config.max_retries,
        )
        return FeedbackLoopResult(
            passed=False,
            payload=None,
            attempts=self._config.max_retries + 1,
        )
