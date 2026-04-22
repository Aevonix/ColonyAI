"""Layer 3 — Cross-session entity leak detection. Fully deterministic."""

from __future__ import annotations

import unicodedata

from colony_sidecar.gate.layers.base import LayerResult


def _normalize_entity(name: str) -> str:
    return unicodedata.normalize("NFKC", name).lower().strip()


class CrossContextDetector:
    """Layer 3 — Cross-session entity leak detection. Fully deterministic."""

    def __init__(self, session_store, config) -> None:
        self._sessions = session_store
        self._config = config

    async def check(self, payload) -> LayerResult:
        # Allow briefing mode to skip cross-context check (controlled cross-session read)
        if getattr(payload, "is_briefing", False):
            return LayerResult(blocked=False, code="pass")

        response_lower = _normalize_entity(payload.response_text)
        current_entities = {_normalize_entity(e) for e in payload.mentioned_entities}

        # Load entities from other recent sessions
        other_sessions = await self._sessions.get_recent_other_sessions(
            exclude_session_id=payload.session_id,
            lookback_hours=self._config.cross_context_lookback_hours,
        )

        for other_session_id, other_entities in other_sessions.items():
            for entity in other_entities:
                normalized = _normalize_entity(entity)
                if normalized in current_entities:
                    continue  # Entity is in this session's context — OK
                if len(normalized) < 3:
                    continue  # Skip very short tokens — too many false positives
                if normalized in response_lower:
                    return LayerResult(
                        blocked=True,
                        code="block_cross_context",
                        reason=f"entity from session {other_session_id!r} appeared in response",
                        flagged_excerpt="[entity from another session]",
                    )

        return LayerResult(blocked=False, code="pass")
