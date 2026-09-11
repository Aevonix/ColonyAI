"""Backfill trigger_patterns for existing skills from their name and tags.

NOTE: This migration was written for a legacy SQLite-backed SkillRegistry.
The current SkillRegistry is in-memory and auto-loads built-in skills.
This script is kept for historical reference but is a no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path


def backfill(db_path: Path) -> None:
    print("INFO: Legacy migration skipped — current SkillRegistry is in-memory.")


if __name__ == "__main__":
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("~/.colony/skills.db").expanduser()
    backfill(db)
