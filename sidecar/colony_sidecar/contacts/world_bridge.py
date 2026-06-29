"""Colony Contacts — World Model Bridge.

Synchronises Neo4j :Person nodes with the SQLite ContactStore so that
discovered people become visible to the contacts API while preserving
the architectural separation between curated and discovered contacts.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .models import Contact
from .store import SQLiteContactStore, _name_similarity, _normalize_email, _normalize_phone

logger = logging.getLogger("colony.contacts.world_bridge")


def _person_handle_gaps(person: Dict[str, Any]) -> List[tuple[str, str]]:
    """Extract (gateway, address) pairs from a Person node's properties."""
    handles: List[tuple[str, str]] = []
    phone = person.get("phone")
    if phone:
        norm = _normalize_phone(str(phone))
        if norm:
            handles.append(("imessage", norm))
    email = person.get("email")
    if email:
        norm = _normalize_email(str(email))
        if norm:
            handles.append(("email", norm))
    return handles


class WorldModelContactBridge:
    """Creates and maintains shadow contacts from Neo4j Person nodes.

    Shadow contacts:
    - trust_tier = "acquaintance"
    - interaction_allowed = False
    - import_source = "world_model"
    - person_node_id = Person.id

    The bridge deduplicates against existing curated contacts by
    person_node_id, overlapping handles, or high name similarity.
    """

    def __init__(
        self,
        graph,  # ColonyGraph instance
        store: SQLiteContactStore,
        min_signals: int = 2,
        min_memories: int = 2,
        name_match_threshold: float = 0.85,
    ) -> None:
        self._graph = graph
        self._store = store
        self._min_signals = min_signals
        self._min_memories = min_memories
        self._name_match_threshold = name_match_threshold

    # ── Public API ─────────────────────────────────────────────────────────────

    async def sync_person_to_contact(self, person_id: str) -> Optional[Contact]:
        """Ensure a single Person node is reflected as a contact.

        Returns the existing or newly-created contact, or None if the
        person does not meet the substance threshold.
        """
        person = await self._graph.get_person(person_id)
        if not person:
            logger.debug("sync_person_to_contact: Person %s not found in graph", person_id)
            return None

        if not person.get("name"):
            logger.debug("sync_person_to_contact: Person %s has no name, skipping", person_id)
            return None

        # Check substance threshold
        handles = _person_handle_gaps(person)
        has_handle = bool(handles)

        # We also accept persons with signals/memories — graph query already
        # filters, but for single-person sync we do a lightweight check.
        if not has_handle:
            # Need to verify signals/memories via graph
            people = await self._graph.get_people_with_substance(
                min_signals=self._min_signals,
                min_memories=self._min_memories,
            )
            if not any(p["id"] == person_id for p in people):
                logger.debug(
                    "sync_person_to_contact: Person %s lacks substance (no handle, < %d signals/memories)",
                    person_id, self._min_signals,
                )
                return None

        # Deduplication: check for existing contact
        existing = await self._find_existing_contact(person_id, person.get("name"), handles)
        if existing:
            # Ensure person_node_id is linked
            if not existing.person_node_id:
                await self._store.update(existing.contact_id, person_node_id=person_id)
                existing.person_node_id = person_id
            logger.debug(
                "sync_person_to_contact: Person %s matched existing contact %s",
                person_id, existing.contact_id,
            )
            return existing

        # Create shadow contact
        contact = await self._store.create(
            display_name=person.get("name"),
            trust_tier="acquaintance",
            interaction_allowed=False,
            import_source="world_model",
            notes=person.get("notes") or None,
        )
        await self._store.update(contact.contact_id, person_node_id=person_id)
        contact.person_node_id = person_id

        # Attach handles from Person properties
        for gateway, address in handles:
            try:
                await self._store.add_handle(
                    contact.contact_id,
                    gateway,
                    address,
                    is_primary=True,
                    source="world_model",
                    confidence=0.7,
                )
            except ValueError:
                # Handle already owned by another contact (possible race or
                # overlap with curated contact created since dedup check)
                logger.debug(
                    "sync_person_to_contact: handle conflict for %s %s on person %s",
                    gateway, address, person_id,
                )

        logger.info(
            "Created shadow contact %s for Person %s (%s)",
            contact.contact_id, person_id, person.get("name"),
        )
        return contact

    async def backfill_all_people(self) -> Dict[str, int]:
        """One-shot backfill of all substantive Person nodes into contacts.

        Returns counters: {"created": int, "linked": int, "skipped": int}
        """
        people = await self._graph.get_people_with_substance(
            min_signals=self._min_signals,
            min_memories=self._min_memories,
        )
        stats = {"created": 0, "linked": 0, "skipped": 0}
        for person in people:
            person_id = person["id"]
            name = person.get("name")
            handles = _person_handle_gaps(person)

            existing = await self._find_existing_contact(person_id, name, handles)
            if existing:
                if not existing.person_node_id:
                    await self._store.update(existing.contact_id, person_node_id=person_id)
                    stats["linked"] += 1
                else:
                    stats["skipped"] += 1
                continue

            contact = await self._store.create(
                display_name=name,
                trust_tier="acquaintance",
                interaction_allowed=False,
                import_source="world_model",
                notes=person.get("notes") or None,
            )
            await self._store.update(contact.contact_id, person_node_id=person_id)

            for gateway, address in handles:
                try:
                    await self._store.add_handle(
                        contact.contact_id, gateway, address,
                        is_primary=True, source="world_model", confidence=0.7,
                    )
                except ValueError:
                    pass

            stats["created"] += 1
            logger.info(
                "Backfill: created shadow contact %s for Person %s (%s)",
                contact.contact_id, person_id, name,
            )

        logger.info(
            "Backfill complete: created=%d linked=%d skipped=%d total_people=%d",
            stats["created"], stats["linked"], stats["skipped"], len(people),
        )
        return stats

    async def absorb_discovered_contact(
        self,
        curated_contact_id: str,
        discovered_contact: Contact,
    ) -> bool:
        """Absorb a discovered contact into a curated one.

        Copies person_node_id and handles, then soft-deletes the discovered
        contact. Returns True if anything was absorbed.
        """
        if discovered_contact.import_source != "world_model":
            return False

        updates: Dict[str, Any] = {}
        if discovered_contact.person_node_id and not (
            await self._store.find_by_person_node_id(discovered_contact.person_node_id)
        ):
            updates["person_node_id"] = discovered_contact.person_node_id

        if updates:
            await self._store.update(curated_contact_id, **updates)

        # Migrate handles
        handles = await self._store.get_handles(discovered_contact.contact_id)
        for h in handles:
            try:
                await self._store.add_handle(
                    curated_contact_id,
                    h.gateway,
                    h.address,
                    is_primary=h.is_primary,
                    source=f"absorbed:{h.source}",
                    confidence=h.confidence,
                )
            except ValueError:
                # Handle already on curated contact — fine
                pass

        await self._store.soft_delete(
            discovered_contact.contact_id,
            reason=f"absorbed_into:{curated_contact_id}",
            performed_by="system",
        )
        await self._store.record_audit(
            curated_contact_id,
            "absorbed_discovered",
            {
                "absorbed_contact_id": discovered_contact.contact_id,
                "person_node_id": discovered_contact.person_node_id,
            },
        )
        logger.info(
            "Absorbed discovered contact %s into curated %s",
            discovered_contact.contact_id, curated_contact_id,
        )
        return True

    async def prune_orphaned_shadows(self) -> int:
        """Soft-delete shadow contacts whose Person node no longer exists.

        Returns the number of contacts pruned.
        """
        # Fetch all shadow contacts with person_node_id
        shadow_contacts = await self._store.list(
            trust_tier="acquaintance", limit=10000,
        )
        if not shadow_contacts:
            return 0

        alive_ids = set(await self._graph.list_person_ids())
        pruned = 0
        for contact in shadow_contacts:
            if not contact.person_node_id:
                continue
            if contact.person_node_id not in alive_ids:
                await self._store.soft_delete(
                    contact.contact_id,
                    reason="orphaned_person_node",
                    performed_by="system",
                )
                pruned += 1
                logger.info(
                    "Pruned orphaned shadow contact %s (Person %s deleted from graph)",
                    contact.contact_id, contact.person_node_id,
                )
        return pruned

    # ── Internal helpers ─────────────────────────────────────────────────────────────

    async def _find_existing_contact(
        self,
        person_id: str,
        name: Optional[str],
        handles: List[tuple[str, str]],
    ) -> Optional[Contact]:
        """Check if a Person already has a matching contact (curated or discovered)."""
        # 1. Exact person_node_id match
        contact = await self._store.find_by_person_node_id(person_id)
        if contact:
            return contact

        # 2. Handle overlap
        for gateway, address in handles:
            contact = await self._store.resolve_handle(gateway, address)
            if contact:
                return contact

        # 3. Name similarity
        if name:
            matches = await self._store.find_by_name(name, threshold=self._name_match_threshold)
            if matches:
                return matches[0]

        return None
