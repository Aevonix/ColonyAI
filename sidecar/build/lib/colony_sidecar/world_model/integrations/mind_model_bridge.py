"""Mind Model bridge: provides world model context to the mind model pipeline."""

from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..store import WorldModelStore


class MindModelBridge:
    """Provides world model context to the mind model pipeline."""

    def __init__(self, store: "WorldModelStore") -> None:
        self._store = store

    async def get_context_for_person(
        self,
        person_id: str,
        as_of: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch world model context relevant to a person's mental state.

        Returns employer, active projects, upcoming events, and shared
        concepts that may inform state estimation.

        Args:
            person_id: The world model entity ID of the person.
            as_of: Optional ISO8601 datetime for temporal queries.

        Returns:
            Dict with 'employer', 'projects', 'events', 'concepts' keys.
        """
        context: Dict[str, Any] = {
            "employer": None,
            "projects": [],
            "events": [],
            "concepts": [],
        }

        # Current employer
        employer_rels = await self._store.query_relationships(
            source_id=person_id,
            relationship_type="WM_WORKS_AT",
            active_only=(as_of is None),
            min_confidence=0.30,
            limit=1,
        )
        if employer_rels:
            employer = await self._store.get_entity(employer_rels[0].target_id)
            context["employer"] = employer

        # Active project memberships
        project_rels = await self._store.query_relationships(
            source_id=person_id,
            relationship_type="WM_MEMBER_OF",
            target_types=["project"],
            active_only=(as_of is None),
            min_confidence=0.30,
            limit=10,
        )
        for rel in project_rels:
            project = await self._store.get_entity(rel.target_id)
            if project:
                context["projects"].append(project)

        # Upcoming events (attended)
        event_rels = await self._store.query_relationships(
            source_id=person_id,
            relationship_type="WM_ATTENDED",
            target_types=["event"],
            min_confidence=0.30,
            limit=10,
        )
        for rel in event_rels:
            event = await self._store.get_entity(rel.target_id)
            if event:
                context["events"].append(event)

        # Tagged concepts
        concept_rels = await self._store.query_relationships(
            source_id=person_id,
            relationship_type="WM_TAGGED_WITH",
            target_types=["concept"],
            min_confidence=0.30,
            limit=10,
        )
        for rel in concept_rels:
            concept = await self._store.get_entity(rel.target_id)
            if concept:
                context["concepts"].append(concept)

        return context

    async def enrich_mind_signal(
        self,
        person_id: str,
        signal_text: str,
    ) -> Optional[str]:
        """Cross-reference a text signal with world model entities.

        Returns the entity ID if a matching EventEntity or ConceptEntity
        is found, otherwise None.
        """
        if not signal_text:
            return None

        # Search events and concepts
        candidates = await self._store.find_entities(
            query=signal_text,
            min_confidence=0.30,
            limit=5,
        )
        for candidate in candidates:
            if candidate.entity_type in ("event", "concept"):
                return candidate.id
        return None
