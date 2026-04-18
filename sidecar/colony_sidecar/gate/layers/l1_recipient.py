"""Layer 1 — Recipient verification. Fully deterministic."""

from __future__ import annotations

from colony_sidecar.gate.layers.base import LayerResult


class RecipientVerifier:
    """Layer 1 — Recipient verification. Fully deterministic."""

    def __init__(self, session_store) -> None:
        self._sessions = session_store

    async def check(self, payload) -> LayerResult:
        session = await self._sessions.get(payload.session_id)

        if session is None:
            return LayerResult(
                blocked=True,
                code="block_recipient",
                reason=f"session {payload.session_id!r} not found",
            )

        if not payload.target_contact_id:
            return LayerResult(
                blocked=True,
                code="block_recipient",
                reason="target_contact_id is empty or None",
            )

        if not payload.target_gateway:
            return LayerResult(
                blocked=True,
                code="block_recipient",
                reason="target_gateway is empty or None",
            )

        if payload.target_contact_id != session.contact_id:
            return LayerResult(
                blocked=True,
                code="block_recipient",
                reason=(
                    f"payload.target_contact_id={payload.target_contact_id!r} "
                    f"!= session.contact_id={session.contact_id!r}"
                ),
            )

        contact_gateways = await self._sessions.get_contact_gateways(
            payload.target_contact_id
        )
        if payload.target_gateway not in contact_gateways:
            return LayerResult(
                blocked=True,
                code="block_recipient",
                reason=(
                    f"gateway {payload.target_gateway!r} not in known gateways "
                    f"for contact {payload.target_contact_id!r}"
                ),
            )

        return LayerResult(blocked=False, code="pass")
