"""Session Continuity — maintain context across conversation sessions.

Manages:
    - Session handoff
    - Context preservation
    - Long-term memory bridging
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import logging

logger = logging.getLogger(__name__)


@dataclass
class SessionContext:
    """Context carried across sessions.

    Attributes:
        session_id: Unique session identifier
        user_id: User this session belongs to
        started_at: When the session began
        last_activity: Most recent activity timestamp
        topics: Topics discussed in this session
        entities: Referenced entity IDs (people, projects, etc.)
        pending_tasks: Tasks still in progress
        metadata: Additional session-specific data
    """

    session_id: str
    user_id: str
    started_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    topics: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    pending_tasks: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class SessionContinuity:
    """Maintain context across sessions.

    Tracks active sessions per user, resumes existing sessions within
    the timeout window, and preserves topics, entities, and pending
    tasks across session boundaries.

    Args:
        graph_client: Colony graph client for persistent session storage
        event_bus: Colony event bus for session lifecycle events
    """

    def __init__(self, graph_client: Any, event_bus: Any) -> None:
        self.graph = graph_client
        self.events = event_bus
        self._active_sessions: Dict[str, SessionContext] = {}
        self._session_timeout = timedelta(hours=24)

    async def start_session(self, user_id: str) -> SessionContext:
        """Start a new session, potentially continuing from a previous one.

        If an active session exists for the user within the timeout window,
        it is resumed instead of creating a new one.

        Args:
            user_id: The user starting or resuming a session

        Returns:
            Active or newly created SessionContext
        """
        existing = await self._find_existing_session(user_id)

        if existing:
            existing.last_activity = datetime.now()
            logger.debug("Resumed session %s for user %s", existing.session_id, user_id)
            return existing

        session_id = f"session-{user_id}-{datetime.now().isoformat()}"
        context = SessionContext(
            session_id=session_id,
            user_id=user_id,
        )
        self._active_sessions[session_id] = context

        logger.debug("Started new session %s for user %s", session_id, user_id)
        return context

    async def update_context(
        self,
        session_id: str,
        topics: Optional[List[str]] = None,
        entities: Optional[List[str]] = None,
        pending_tasks: Optional[List[str]] = None,
    ) -> None:
        """Update session context with new information.

        Deduplicates topics and entities. Appends pending tasks.

        Args:
            session_id: Session to update
            topics: New topics to add
            entities: New entity IDs to add
            pending_tasks: New pending tasks to add
        """
        if session_id not in self._active_sessions:
            logger.warning("Attempted to update unknown session %s", session_id)
            return

        ctx = self._active_sessions[session_id]
        ctx.last_activity = datetime.now()

        if topics:
            ctx.topics.extend(t for t in topics if t not in ctx.topics)
        if entities:
            ctx.entities.extend(e for e in entities if e not in ctx.entities)
        if pending_tasks:
            ctx.pending_tasks.extend(pending_tasks)

    async def end_session(self, session_id: str) -> Optional[SessionContext]:
        """Explicitly end a session.

        Args:
            session_id: Session to end

        Returns:
            The ended session context, or None if not found
        """
        ctx = self._active_sessions.pop(session_id, None)
        if ctx:
            logger.debug("Ended session %s", session_id)
        return ctx

    async def get_active_session(self, user_id: str) -> Optional[SessionContext]:
        """Get the active session for a user, if any.

        Args:
            user_id: User to look up

        Returns:
            Active session or None
        """
        return await self._find_existing_session(user_id)

    async def _find_existing_session(self, user_id: str) -> Optional[SessionContext]:
        """Find an existing active session within the timeout window."""
        cutoff = datetime.now() - self._session_timeout

        for ctx in self._active_sessions.values():
            if ctx.user_id == user_id and ctx.last_activity > cutoff:
                return ctx

        return None
