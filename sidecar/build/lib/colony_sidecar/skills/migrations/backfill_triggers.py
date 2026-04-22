"""Backfill trigger_patterns for existing skills from their name and tags.

Usage:
    python -m colony.skills.migrations.backfill_triggers <db_path>
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


async def backfill(db_path: Path) -> None:
    from colony_sidecar.skills.registry import SkillRegistry

    registry = SkillRegistry(db_path)
    registry.open()
    try:
        manifests = await registry.list_all()
        updated = 0
        for m in manifests:
            if m.trigger_patterns:
                continue  # Already populated
            patterns = [m.name] + m.tags
            await registry.update_trigger_patterns(m.skill_id, patterns)
            updated += 1
        print(f"Backfilled trigger_patterns for {updated} skill(s).")
    finally:
        registry.close()


if __name__ == "__main__":
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("~/.colony/skills.db").expanduser()
    asyncio.run(backfill(db))
