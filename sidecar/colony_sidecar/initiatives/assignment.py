"""Assignment Engine for multi-agent Colony.

Provides:
- Agent selection for initiatives
- Capability matching
- Load balancing
- Priority handling
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Initiative type → required capabilities
INITIATIVE_CAPABILITIES: Dict[str, List[str]] = {
    "follow_up": [],  # Any agent
    "relationship": ["messaging"],  # Needs messaging
    "scheduling": ["calendar"],  # Needs calendar
    "coding": ["coding"],  # Needs code execution
    "health": [],  # Any agent
}

# Types that should prefer primary agent
USER_FACING_TYPES = ["follow_up", "relationship"]

# Default for unknown types
UNKNOWN_TYPE_POLICY = "allow_any"  # "reject", "log_and_allow", "allow_any"


class AssignmentEngine:
    """Selects the best agent for an initiative."""

    def __init__(
        self,
        agent_store: Any,
        initiative_store: Any,
        unknown_type_policy: str = UNKNOWN_TYPE_POLICY,
    ):
        self._agent_store = agent_store
        self._initiative_store = initiative_store
        self._unknown_type_policy = unknown_type_policy

    def select_agent(
        self,
        initiative: Any,
        agents: Optional[List[Any]] = None,
    ) -> Optional[Any]:
        """Select the best agent for an initiative.

        Priority:
        1. Online status
        2. Preferred agent (if specified)
        3. Capability match
        4. Type restrictions
        5. Primary designation (for user-facing)
        6. Load balancing
        7. Capacity check

        Args:
            initiative: Initiative to assign (StoredInitiative or dict)
            agents: List of agents to choose from (if None, loads from store)

        Returns:
            Selected Agent or None if no suitable agent found
        """
        # Get initiative type
        init_type = self._get_initiative_type(initiative)

        # Check if type is known
        if init_type not in INITIATIVE_CAPABILITIES:
            if self._unknown_type_policy == "reject":
                logger.warning("Unknown initiative type: %s, rejecting", init_type)
                return None
            elif self._unknown_type_policy == "log_and_allow":
                logger.warning("Unknown initiative type: %s, allowing any agent", init_type)

        # Get agents if not provided
        if agents is None:
            agents = self._agent_store.list(status=["online"])

        # Step 1: Only online agents
        candidates = [a for a in agents if a.status == "online"]
        if not candidates:
            return None

        # Step 2: Preferred agent (if specified)
        preferred_id = self._get_preferred_agent_id(initiative)
        if preferred_id:
            preferred = next(
                (a for a in candidates if a.agent_id == preferred_id),
                None
            )
            if preferred and self._has_capacity(preferred):
                return preferred

        # Step 3: Capability filter
        required_caps = INITIATIVE_CAPABILITIES.get(init_type, [])
        if required_caps:
            candidates = [
                a for a in candidates
                if a.has_capabilities(required_caps)
            ]

        if not candidates:
            logger.debug(
                "No agents with capabilities %s for initiative type %s",
                required_caps,
                init_type,
            )
            return None

        # Step 4: Type restrictions (excluded/included types)
        filtered = []
        for agent in candidates:
            if not agent.can_handle_type(init_type):
                continue
            filtered.append(agent)

        candidates = filtered if filtered else candidates

        if not candidates:
            return None

        # Step 5: Primary preference for user-facing
        if init_type in USER_FACING_TYPES:
            primaries = [a for a in candidates if a.is_primary]
            if primaries:
                candidates = primaries

        # Step 6: Sort by load (ascending), then priority (descending)
        candidates.sort(key=lambda a: (
            a.load,
            -a.priority,
        ))

        # Step 7: Capacity check
        candidates = [a for a in candidates if a.has_capacity]

        return candidates[0] if candidates else None

    async def assign(
        self,
        initiative: Any,
        agent: Optional[Any] = None,
    ) -> Optional[Any]:
        """Assign initiative to agent.

        Args:
            initiative: Initiative to assign
            agent: Specific agent to assign to (if None, auto-select)

        Returns:
            Assigned agent or None if assignment failed
        """
        # Get initiative ID and type
        init_id = self._get_initiative_id(initiative)
        if not init_id:
            return None

        # Select agent if not provided
        if agent is None:
            agent = self.select_agent(initiative)

        if not agent:
            logger.debug("No agent available for initiative %s", init_id)
            return None

        # Assign via store
        assigned = self._initiative_store.assign(
            init_id,
            agent.agent_id,
            agent_name=agent.name,
        )

        if assigned:
            # Update agent assignment count
            self._agent_store.increment_assignments(agent.agent_id)
            logger.info(
                "Assigned initiative %s to agent %s (%s)",
                init_id,
                agent.agent_id,
                agent.name,
            )
            return agent

        return None

    async def auto_assign_pending(self, limit: int = 50) -> int:
        """Auto-assign all pending initiatives.

        Args:
            limit: Maximum initiatives to assign

        Returns:
            Number of initiatives assigned
        """
        pending = self._initiative_store.list(
            status=["pending"],
            limit=limit,
        )

        assigned = 0
        for initiative in pending:
            agent = await self.assign(initiative)
            if agent:
                assigned += 1

        return assigned

    def _get_initiative_type(self, initiative: Any) -> str:
        """Get initiative type from object or dict."""
        if hasattr(initiative, "type"):
            return initiative.type.value if hasattr(initiative.type, "value") else str(initiative.type)
        if isinstance(initiative, dict):
            return initiative.get("type", "unknown")
        return "unknown"

    def _get_initiative_id(self, initiative: Any) -> Optional[str]:
        """Get initiative ID from object or dict."""
        if hasattr(initiative, "id"):
            return initiative.id
        if isinstance(initiative, dict):
            return initiative.get("id")
        return None

    def _get_preferred_agent_id(self, initiative: Any) -> Optional[str]:
        """Get preferred agent ID from initiative."""
        if hasattr(initiative, "preferred_agent_id"):
            return initiative.preferred_agent_id
        if isinstance(initiative, dict):
            return initiative.get("preferred_agent_id")
        return None

    def _has_capacity(self, agent: Any) -> bool:
        """Check if agent has capacity for more assignments."""
        # Check max concurrent
        if agent.current_assignments >= agent.max_concurrent:
            return False

        # Check hourly rate limit. The agents table carries a
        # max_initiatives_per_hour column (default 10) that previously had no
        # enforcement — counting `assigned` rows from the assignment_history
        # within the last hour closes that gap. A value of 0 disables the
        # limit; <0 is treated as 0 (off).
        max_per_hour = getattr(agent, "max_initiatives_per_hour", 0) or 0
        if max_per_hour > 0:
            try:
                since = datetime.now(timezone.utc) - timedelta(hours=1)
                recent = self._initiative_store.count_agent_assignments_since(
                    agent.agent_id, since
                )
                if recent >= max_per_hour:
                    return False
            except (AttributeError, TypeError):
                # Store does not expose the counter (older deployments or
                # unit-test doubles) — fall back to the max_concurrent gate
                # rather than blocking all assignments.
                pass

        return True
