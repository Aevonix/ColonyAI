"""Colony Graph Schema Migrations.

Applies Neo4j constraints and indexes required by the Colony graph memory
system.  Designed to be idempotent — safe to re-run.

Note: Vector index creation has been removed.  Vector search is now handled
by a local LanceDB store (see colony/vector/).  The Neo4j Community Edition
does not support ``db.index.vector.*`` procedures.
"""

from __future__ import annotations

import logging
from typing import List

try:
    from neo4j import AsyncDriver
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Schema V1 — initial Colony graph schema
# ──────────────────────────────────────────────────────────────────────

# Each statement is executed individually so that one failure does not
# block subsequent (independent) statements.

SCHEMA_V1: List[str] = [
    # ── Node constraints ──────────────────────────────────────────────
    "CREATE CONSTRAINT memory_id IF NOT EXISTS FOR (m:Memory) REQUIRE m.id IS UNIQUE",
    "CREATE CONSTRAINT person_id IF NOT EXISTS FOR (p:Person) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT entity_name IF NOT EXISTS FOR (e:Entity) REQUIRE e.name IS UNIQUE",
    "CREATE CONSTRAINT owner_id IF NOT EXISTS FOR (o:Owner) REQUIRE o.id IS UNIQUE",
    "CREATE CONSTRAINT signal_id IF NOT EXISTS FOR (s:Signal) REQUIRE s.id IS UNIQUE",
    "CREATE CONSTRAINT context_id IF NOT EXISTS FOR (c:Context) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT score_event_id IF NOT EXISTS FOR (se:ScoreEvent) REQUIRE se.id IS UNIQUE",
    "CREATE CONSTRAINT prediction_id IF NOT EXISTS FOR (pr:Prediction) REQUIRE pr.id IS UNIQUE",
    # ── Indexes for common queries ────────────────────────────────────
    "CREATE INDEX memory_type IF NOT EXISTS FOR (m:Memory) ON (m.type)",
    "CREATE INDEX memory_strength IF NOT EXISTS FOR (m:Memory) ON (m.strength)",
    "CREATE INDEX person_tier IF NOT EXISTS FOR (p:Person) ON (p.tier)",
    "CREATE INDEX signal_type IF NOT EXISTS FOR (s:Signal) ON (s.signal_type)",
    "CREATE INDEX signal_timestamp IF NOT EXISTS FOR (s:Signal) ON (s.timestamp)",
    "CREATE INDEX person_last_interaction IF NOT EXISTS FOR (p:Person) ON (p.lastInteraction)",
    "CREATE INDEX memory_accessed_at IF NOT EXISTS FOR (m:Memory) ON (m.accessed_at)",
    "CREATE INDEX memory_created_at IF NOT EXISTS FOR (m:Memory) ON (m.created_at)",
    "CREATE INDEX prediction_expires IF NOT EXISTS FOR (pr:Prediction) ON (pr.expires_at)",
    "CREATE INDEX prediction_resolved IF NOT EXISTS FOR (pr:Prediction) ON (pr.resolved)",
    "CREATE INDEX person_score IF NOT EXISTS FOR (p:Person) ON (p.score)",
]

async def run_migrations(
    driver: AsyncDriver,
    database: str = "colony",
    embedding_dimensions: int = 1536,
) -> dict[str, int]:
    """Apply all Colony graph schema migrations.

    Args:
        driver: An authenticated ``AsyncDriver`` instance.
        database: Target Neo4j database name.
        embedding_dimensions: Kept for API compat; vector index creation
            is now handled by LanceDB (see colony/vector/).

    Returns:
        A summary dict with counts of ``applied``, ``skipped``, and
        ``failed`` statements.
    """
    applied = 0
    skipped = 0
    failed = 0

    async with driver.session(database=database) as session:
        # ── Constraints & indexes ─────────────────────────────────────
        for stmt in SCHEMA_V1:
            try:
                await session.run(stmt)
                applied += 1
            except Exception as exc:
                msg = str(exc).lower()
                if "already exists" in msg or "equivalent" in msg:
                    skipped += 1
                else:
                    failed += 1

    # Vector index creation removed — vector search now uses local LanceDB.
    # Neo4j Community Edition does not support db.index.vector.* procedures.
    logger.info(
        "Graph migrations complete (applied=%d, skipped=%d, failed=%d). "
        "Vector search handled by LanceDB — run 'colony vector setup' to configure.",
        applied, skipped, failed,
    )

    return {"applied": applied, "skipped": skipped, "failed": failed}
