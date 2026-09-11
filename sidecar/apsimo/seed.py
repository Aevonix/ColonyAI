"""Compatibility entry points for retired built-in self-knowledge seeding.

Runtime capabilities are deployment-specific. The former static catalog is
retired; these entry points retain their result shape without opening stores
or modifying historical memories, entities, or skills.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apsimo.intelligence.graph.client import ColonyGraph
    from apsimo.contacts.store import ContactStore
    from apsimo.goals.store import GoalStore
    from apsimo.world_model.store import WorldModelStore
    from apsimo.skills.registry import SkillRegistry


async def seed_self_knowledge(
    graph: "ColonyGraph | None" = None,
    contacts_store: "ContactStore | None" = None,
    goals_store: "GoalStore | None" = None,
    world_store: "WorldModelStore | None" = None,
    skills_registry: "SkillRegistry | None" = None,
    force: bool = False,
) -> dict:
    """Return the retirement disposition; legacy arguments have no effect."""
    return {
        "memories": 0,
        "entities": 0,
        "skills": 0,
        "insights": 0,
        "skipped": ["builtin_self_knowledge_retired"],
        "errors": [],
    }


def seed_self_knowledge_summary() -> str:
    """Describe the retired command without claiming runtime capabilities."""
    return "Built-in self-knowledge seeding is retired. Historical records are preserved."
