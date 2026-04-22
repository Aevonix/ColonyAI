"""SessionContextLoader — loads all context for a session turn, respecting isolation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from colony_sidecar.intelligence.relationships.trust_tiers import TrustTier
from colony_sidecar.sessions.isolated_session import IsolatedSession


@dataclass
class SessionContext:
    """The complete context package loaded for a session turn.

    This object has no method to access other sessions.
    All fields are scoped to the single contact.
    """
    session: IsolatedSession
    person_node: dict                    # Person node from Neo4j
    accessible_memories: list           # memories tagged for this tier or lower
    initiative_history: list            # last N outreach events with this contact
    relationship_trend: Optional[list]  # 30-day score trend (SHOULD load)
    anomaly_flags: list                 # current anomaly flags (SHOULD load)
    style_model: Optional[dict]         # communication style model (SHOULD load)


class SessionContextLoader:
    """Loads all context for a session turn, respecting isolation boundaries."""

    def __init__(
        self,
        session_store,
        graph_client,
        config,
    ) -> None:
        self._sessions = session_store
        self._graph = graph_client
        self._config = config

    async def load(self, session_id: str) -> SessionContext:
        """Load full context for a session. Raises if session not found."""
        session = await self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id!r} not found")

        # MUST load
        person_node = await self._graph.get_person_node(session.contact_id)
        accessible_memories = await self._graph.get_memories_for_tier(
            contact_id=session.contact_id,
            tier=session.trust_tier,
            exclude_visibility=["COLONY_ONLY"],  # NEVER load private memories
        )
        initiative_history = await self._graph.get_initiative_history(
            contact_id=session.contact_id,
            limit=10,
        )

        # SHOULD load (best-effort)
        relationship_trend = None
        anomaly_flags = []
        style_model = None
        remaining_tokens = (
            self._config.context_token_limit - session.context_token_count
        )
        if remaining_tokens > 5_000:
            try:
                relationship_trend = await self._graph.get_score_trend(
                    contact_id=session.contact_id, days=30
                )
                anomaly_flags = await self._graph.get_anomaly_flags(
                    contact_id=session.contact_id
                )
                style_model = await self._graph.get_style_model(
                    contact_id=session.contact_id
                )
            except Exception:
                pass  # SHOULD load — failures are non-fatal

        return SessionContext(
            session=session,
            person_node=person_node,
            accessible_memories=accessible_memories,
            initiative_history=initiative_history,
            relationship_trend=relationship_trend,
            anomaly_flags=anomaly_flags,
            style_model=style_model,
        )
