"""File Reconciler — ground truth reconciliation for file-sourced memories.

Validates memories derived from files against their current on-disk content,
invalidating stale memories and creating superseded versions when content changes.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from colony_sidecar.intelligence.graph.client import ColonyGraph, EpistemicState

logger = logging.getLogger(__name__)


class FileReconciler:
    """Reconciles file-sourced memories against ground truth."""

    def __init__(self, graph: ColonyGraph) -> None:
        self.graph = graph

    async def reconcile(
        self,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Run full file reconciliation.

        For each memory with source_type='file':
        1. Check if file exists
        2. If not: mark memory STALE
        3. If yes and content_hash matches: mark VERIFIED
        4. If yes and hash differs: create new memory with updated content

        Returns:
            Dict with files_checked, memories_verified, memories_staled,
            memories_superseded, errors.
        """
        files_checked = 0
        memories_verified = 0
        memories_staled = 0
        memories_superseded = 0
        errors: List[str] = []

        async with self.graph.driver.session(database=self.graph.database) as session:
            result = await session.run(
                """
                MATCH (m:Memory)
                WHERE m.source_type = 'file'
                RETURN m.id AS id, m.source_uri AS path,
                       m.content_hash AS content_hash, m.content AS content
                """
            )
            rows = [
                {
                    "id": r["id"],
                    "path": r["path"],
                    "content_hash": r["content_hash"],
                    "content": r["content"],
                }
                async for r in result
            ]

        for row in rows:
            try:
                filepath = row["path"]
                if not filepath:
                    continue

                path = Path(filepath)
                files_checked += 1

                if not path.exists():
                    # File deleted — mark stale
                    if not dry_run:
                        await self.graph.transition_epistemic_state(
                            row["id"], EpistemicState.STALE.value
                        )
                    memories_staled += 1
                    continue

                # Compute current hash
                current_content = path.read_text(encoding="utf-8", errors="replace")
                current_hash = hashlib.sha256(
                    current_content.encode("utf-8")
                ).hexdigest()

                if row["content_hash"] == current_hash:
                    # Unchanged — mark verified
                    if not dry_run:
                        await self.graph.verify_memory(row["id"])
                    memories_verified += 1
                else:
                    # Changed — supersede old, create new
                    if not dry_run:
                        # Create new memory with updated content
                        new_id = await self.graph.store_memory(
                            content=current_content,
                            memory_type="semantic",
                            entities=[],
                            importance=0.85,
                            source_type="file",
                            source_uri=filepath,
                            content_hash=current_hash,
                        )
                        # Transition old to superseded
                        await self.graph.transition_epistemic_state(
                            row["id"],
                            EpistemicState.SUPERSEDED.value,
                            superseded_by=new_id,
                        )
                        # Link new to old
                        async with self.graph.driver.session(
                            database=self.graph.database
                        ) as session:
                            await session.run(
                                """
                                MATCH (new:Memory {id: $new_id}), (old:Memory {id: $old_id})
                                MERGE (new)-[r:SUPERSEDES]->(old)
                                SET r.superseded_at = datetime()
                                """,
                                new_id=new_id,
                                old_id=row["id"],
                            )
                    memories_superseded += 1

            except Exception as exc:
                logger.warning("Reconciliation failed for %s: %s", row.get("id"), exc)
                errors.append(f"{row.get('id')}: {exc}")

        return {
            "files_checked": files_checked,
            "memories_verified": memories_verified,
            "memories_staled": memories_staled,
            "memories_superseded": memories_superseded,
            "errors": errors,
        }
