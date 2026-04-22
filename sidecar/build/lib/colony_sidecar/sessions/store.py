"""IsolatedSessionStore — session store protocol and in-memory implementation."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional, Protocol

from colony_sidecar.intelligence.relationships.trust_tiers import TrustTier
from colony_sidecar.sessions.isolated_session import IsolatedSession, SessionState


class IsolatedSessionStore(Protocol):
    """Interface for the session store. No method crosses session boundaries."""

    async def create(
        self,
        contact_id: str,
        gateway: str,
        trust_tier: TrustTier,
    ) -> IsolatedSession:
        """Create a new session for a contact."""
        ...

    async def get(self, session_id: str) -> Optional[IsolatedSession]:
        """Get session by session_id. Returns None if not found."""
        ...

    async def get_by_contact(self, contact_id: str) -> Optional[IsolatedSession]:
        """Get the active session for a contact. Returns None if not active."""
        ...

    async def get_recent_other_sessions(
        self,
        exclude_session_id: str,
        lookback_hours: int,
    ) -> dict:
        """Return {session_id: frozenset(mentioned_entities)} for all sessions
        EXCEPT the excluded one, active within lookback_hours.

        Used only by Layer 3. MUST NOT return session history or content."""
        ...

    async def get_contact_gateways(self, contact_id: str) -> set:
        """Return the set of gateways known for a contact."""
        ...

    async def get_display_name(self, contact_id: str) -> str:
        """Return display name for a contact."""
        ...

    async def save(self, session: IsolatedSession) -> None:
        """Persist session state."""
        ...

    async def archive(self, session_id: str) -> None:
        """Mark session as archived."""
        ...


class InMemorySessionStore:
    """In-memory session store for testing and development."""

    def __init__(self) -> None:
        self._sessions: dict[str, IsolatedSession] = {}
        # contact_id -> set of gateway labels
        self._contact_gateways: dict[str, set] = {}
        # contact_id -> display name
        self._contact_names: dict[str, str] = {}

    def register_contact(
        self,
        contact_id: str,
        display_name: str,
        gateways: set,
    ) -> None:
        """Register contact metadata (used in tests / setup)."""
        self._contact_gateways[contact_id] = gateways
        self._contact_names[contact_id] = display_name

    async def create(
        self,
        contact_id: str,
        gateway: str,
        trust_tier: TrustTier,
    ) -> IsolatedSession:
        session = IsolatedSession.create(contact_id, gateway, trust_tier)
        self._sessions[session.session_id] = session
        if contact_id not in self._contact_gateways:
            self._contact_gateways[contact_id] = {gateway}
        else:
            self._contact_gateways[contact_id].add(gateway)
        return session

    async def get(self, session_id: str) -> Optional[IsolatedSession]:
        session = self._sessions.get(session_id)
        if session and session.is_expired():
            await self.archive(session_id)
            return None
        return session

    async def get_by_contact(self, contact_id: str) -> Optional[IsolatedSession]:
        for session in list(self._sessions.values()):
            if (
                session.contact_id == contact_id
                and session.state == SessionState.ACTIVE
            ):
                if session.is_expired():
                    await self.archive(session.session_id)
                    continue
                return session
        return None

    async def sweep_expired(self) -> int:
        """Archive all expired sessions. Returns count of sessions archived."""
        count = 0
        for session in list(self._sessions.values()):
            if session.is_expired() and session.state == SessionState.ACTIVE:
                await self.archive(session.session_id)
                count += 1
        return count

    async def get_recent_other_sessions(
        self,
        exclude_session_id: str,
        lookback_hours: int,
    ) -> dict:
        """Return {session_id: frozenset(mentioned_entities)} for other recent sessions."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)
        result = {}
        for session_id, session in self._sessions.items():
            if session_id == exclude_session_id:
                continue
            if session.last_active >= cutoff:
                result[session_id] = session.get_mentioned_entities_snapshot()
        return result

    async def get_contact_gateways(self, contact_id: str) -> set:
        return self._contact_gateways.get(contact_id, set())

    async def get_display_name(self, contact_id: str) -> str:
        return self._contact_names.get(contact_id, contact_id)

    async def save(self, session: IsolatedSession) -> None:
        self._sessions[session.session_id] = session

    async def archive(self, session_id: str) -> None:
        if session_id in self._sessions:
            self._sessions[session_id].state = SessionState.ARCHIVED
