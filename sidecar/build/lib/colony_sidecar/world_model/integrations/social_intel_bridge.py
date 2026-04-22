"""Social Intelligence bridge: provides world model context to the relationship scorer."""

from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..store import WorldModelStore
from ..entities import BaseEntity


class SocialIntelBridge:
    """Provides world model context to the social intelligence scorer."""

    def __init__(self, store: "WorldModelStore") -> None:
        self._store = store

    async def get_shared_entities(
        self,
        person_a_id: str,
        person_b_id: str,
    ) -> Dict[str, List[BaseEntity]]:
        """Return entities shared between two people.

        Checks for shared companies (via WM_WORKS_AT), projects (via
        WM_MEMBER_OF), events (via WM_ATTENDED), and concepts (via
        WM_TAGGED_WITH).

        Returns:
            {
                "companies": [...],
                "projects": [...],
                "events": [...],
                "concepts": [...],
            }
        """
        shared: Dict[str, List[BaseEntity]] = {
            "companies": [],
            "projects": [],
            "events": [],
            "concepts": [],
        }

        # Shared employers
        a_employers = await self._get_related_entity_ids(
            person_a_id, "WM_WORKS_AT"
        )
        b_employers = await self._get_related_entity_ids(
            person_b_id, "WM_WORKS_AT"
        )
        for eid in a_employers & b_employers:
            entity = await self._store.get_entity(eid)
            if entity:
                shared["companies"].append(entity)

        # Shared projects
        a_projects = await self._get_related_entity_ids(
            person_a_id, "WM_MEMBER_OF", target_type="project"
        )
        b_projects = await self._get_related_entity_ids(
            person_b_id, "WM_MEMBER_OF", target_type="project"
        )
        for eid in a_projects & b_projects:
            entity = await self._store.get_entity(eid)
            if entity:
                shared["projects"].append(entity)

        # Shared events
        a_events = await self._get_related_entity_ids(
            person_a_id, "WM_ATTENDED", target_type="event"
        )
        b_events = await self._get_related_entity_ids(
            person_b_id, "WM_ATTENDED", target_type="event"
        )
        for eid in a_events & b_events:
            entity = await self._store.get_entity(eid)
            if entity:
                shared["events"].append(entity)

        # Shared concepts
        a_concepts = await self._get_related_entity_ids(
            person_a_id, "WM_TAGGED_WITH", target_type="concept"
        )
        b_concepts = await self._get_related_entity_ids(
            person_b_id, "WM_TAGGED_WITH", target_type="concept"
        )
        for eid in a_concepts & b_concepts:
            entity = await self._store.get_entity(eid)
            if entity:
                shared["concepts"].append(entity)

        return shared

    async def _get_related_entity_ids(
        self,
        person_id: str,
        relationship_type: str,
        target_type: Optional[str] = None,
    ) -> set:
        """Helper: get set of target entity IDs for a person/relationship type."""
        rels = await self._store.query_relationships(
            source_id=person_id,
            relationship_type=relationship_type,
            target_types=[target_type] if target_type else None,
            min_confidence=0.30,
            limit=100,
        )
        return {r.target_id for r in rels}

    async def get_co_attendance_signal(
        self,
        person_a_id: str,
        person_b_id: str,
    ) -> float:
        """Compute meeting co-attendance signal from shared events.

        Returns a normalized score 0.0–1.0 based on number of shared events.
        """
        shared = await self.get_shared_entities(person_a_id, person_b_id)
        event_count = len(shared.get("events", []))
        # Normalize: 5+ shared events = max signal
        return min(event_count / 5.0, 1.0)
