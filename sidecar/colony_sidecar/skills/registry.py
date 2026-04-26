"""Colony Skills — local SQLite registry for skill management."""

from __future__ import annotations

import json
import logging
import pathlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, List, Optional

from colony_sidecar.skills.models import SkillManifest, SkillPermissions, SkillStatus
from colony_sidecar.skills.schema import SKILLS_SCHEMA

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Local SQLite registry for Colony skills.

    Thread-safety: uses WAL mode with check_same_thread=False. All writes
    acquire an explicit EXCLUSIVE lock. Reads use the default shared lock.
    """

    def __init__(self, db_path: pathlib.Path) -> None:
        self._path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        """Open the registry database and apply schema migrations."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._apply_schema()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SkillRegistry":
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        assert self._conn is not None, "Registry not opened."
        self._conn.execute("BEGIN EXCLUSIVE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    async def register(
        self, manifest: SkillManifest, skill_dir: Optional[pathlib.Path]
    ) -> None:
        """Register a new skill (DRAFT status)."""
        dir_str = str(skill_dir) if skill_dir is not None else (manifest.skill_dir or "")
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO skills (
                    skill_id, name, version, description, author_colony_id,
                    status, tags, dependencies, entry_point,
                    checksum_sha256, origin_task_id, parent_skill_id,
                    trust_score, skill_dir,
                    trigger_patterns, context_tokens_estimate, lazy_loader,
                    created_at, updated_at
                ) VALUES (
                    :skill_id, :name, :version, :description, :author_colony_id,
                    :status, :tags, :deps, :entry_point,
                    :checksum, :origin_task_id, :parent_skill_id,
                    :trust_score, :skill_dir,
                    :trigger_patterns, :context_tokens_estimate, :lazy_loader,
                    :created_at, :updated_at
                )
                """,
                {
                    "skill_id": manifest.skill_id,
                    "name": manifest.name,
                    "version": manifest.version,
                    "description": manifest.description,
                    "author_colony_id": manifest.author_colony_id,
                    "status": manifest.status.value,
                    "tags": ",".join(manifest.tags),
                    "deps": ",".join(manifest.dependencies),
                    "entry_point": manifest.entry_point,
                    "checksum": manifest.checksum_sha256,
                    "origin_task_id": manifest.origin_task_id,
                    "parent_skill_id": manifest.parent_skill_id,
                    "trust_score": manifest.trust_score,
                    "skill_dir": dir_str,
                    "trigger_patterns": json.dumps(manifest.trigger_patterns),
                    "context_tokens_estimate": manifest.context_tokens_estimate,
                    "lazy_loader": manifest.lazy_loader,
                    "created_at": manifest.created_at.isoformat(),
                    "updated_at": manifest.updated_at.isoformat(),
                },
            )

    async def register_or_update(
        self, manifest: SkillManifest, skill_dir: Optional[pathlib.Path]
    ) -> None:
        """Register a skill or update if exists (upsert)."""
        dir_str = str(skill_dir) if skill_dir is not None else (manifest.skill_dir or "")
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO skills (
                    skill_id, name, version, description, author_colony_id,
                    status, tags, dependencies, entry_point,
                    checksum_sha256, origin_task_id, parent_skill_id,
                    trust_score, skill_dir,
                    trigger_patterns, context_tokens_estimate, lazy_loader,
                    created_at, updated_at
                ) VALUES (
                    :skill_id, :name, :version, :description, :author_colony_id,
                    :status, :tags, :deps, :entry_point,
                    :checksum, :origin_task_id, :parent_skill_id,
                    :trust_score, :skill_dir,
                    :trigger_patterns, :context_tokens_estimate, :lazy_loader,
                    :created_at, :updated_at
                )
                ON CONFLICT(skill_id) DO UPDATE SET
                    name = excluded.name,
                    version = excluded.version,
                    description = excluded.description,
                    status = excluded.status,
                    trigger_patterns = excluded.trigger_patterns,
                    updated_at = excluded.updated_at
                """,
                {
                    "skill_id": manifest.skill_id,
                    "name": manifest.name,
                    "version": manifest.version,
                    "description": manifest.description,
                    "author_colony_id": manifest.author_colony_id,
                    "status": manifest.status.value,
                    "tags": ",".join(manifest.tags),
                    "deps": ",".join(manifest.dependencies),
                    "entry_point": manifest.entry_point,
                    "checksum": manifest.checksum_sha256,
                    "origin_task_id": manifest.origin_task_id,
                    "parent_skill_id": manifest.parent_skill_id,
                    "trust_score": manifest.trust_score,
                    "skill_dir": dir_str,
                    "trigger_patterns": json.dumps(manifest.trigger_patterns),
                    "context_tokens_estimate": manifest.context_tokens_estimate,
                    "lazy_loader": manifest.lazy_loader,
                    "created_at": manifest.created_at.isoformat(),
                    "updated_at": manifest.updated_at.isoformat(),
                },
            )

    async def activate(self, skill_id: str) -> None:
        """Set skill status to ACTIVE."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE skills SET status = 'active', updated_at = ? WHERE skill_id = ?",
                (datetime.now(timezone.utc).isoformat(), skill_id),
            )

    async def deactivate(self, skill_id: str) -> None:
        """Set skill status to INACTIVE."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE skills SET status = 'inactive', updated_at = ? WHERE skill_id = ?",
                (datetime.now(timezone.utc).isoformat(), skill_id),
            )

    async def deprecate(self, skill_id: str) -> None:
        """Set skill status to DEPRECATED."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE skills SET status = 'deprecated', updated_at = ? WHERE skill_id = ?",
                (datetime.now(timezone.utc).isoformat(), skill_id),
            )

    async def archive(self, skill_id: str) -> None:
        """Set skill status to ARCHIVED."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE skills SET status = 'archived', updated_at = ? WHERE skill_id = ?",
                (datetime.now(timezone.utc).isoformat(), skill_id),
            )

    async def quarantine(self, skill_id: str, reason: str) -> None:
        """Move skill to QUARANTINED status and record the reason."""
        now = datetime.now(timezone.utc).isoformat()
        with self._tx() as conn:
            conn.execute(
                "UPDATE skills SET status = 'quarantined', updated_at = ? WHERE skill_id = ?",
                (now, skill_id),
            )
            conn.execute(
                "INSERT INTO skill_quarantine_log (skill_id, reason, created_at) VALUES (?, ?, ?)",
                (skill_id, reason, now),
            )

    async def update_trigger_patterns(
        self, skill_id: str, patterns: List[str]
    ) -> None:
        """Update trigger patterns for a skill."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE skills SET trigger_patterns = ?, updated_at = ? WHERE skill_id = ?",
                (json.dumps(patterns), datetime.now(timezone.utc).isoformat(), skill_id),
            )

    async def update_trust_score(self, skill_id: str, trust_score: float) -> None:
        """Update the trust score for a skill."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE skills SET trust_score = ?, updated_at = ? WHERE skill_id = ?",
                (trust_score, datetime.now(timezone.utc).isoformat(), skill_id),
            )

    async def record_execution(
        self,
        skill_id: str,
        execution_id: str,
        status: str,
        duration_ms: int,
        violations: Optional[List[str]] = None,
    ) -> None:
        """Record a skill execution in the log."""
        now = datetime.now(timezone.utc).isoformat()
        violations_str = ",".join(violations) if violations else ""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO skill_execution_log
                    (skill_id, execution_id, status, duration_ms, violations, executed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (skill_id, execution_id, status, duration_ms, violations_str, now),
            )
            if status == "success":
                conn.execute(
                    """
                    UPDATE skills
                    SET execution_count = execution_count + 1,
                        last_executed_at = ?,
                        updated_at = ?
                    WHERE skill_id = ?
                    """,
                    (now, now, skill_id),
                )

    async def search(
        self,
        query: str = "",
        tags: Optional[List[str]] = None,
        status: Optional[SkillStatus] = None,
        limit: int = 50,
    ) -> List[SkillManifest]:
        """Search the registry by text query and/or tags."""
        assert self._conn is not None
        sql = "SELECT * FROM skills WHERE 1=1"
        params: list = []
        if query:
            sql += " AND (name LIKE ? OR description LIKE ?)"
            params += [f"%{query}%", f"%{query}%"]
        if status:
            sql += " AND status = ?"
            params.append(status.value)
        if tags:
            for tag in tags:
                sql += " AND tags LIKE ?"
                params.append(f"%{tag}%")
        sql += " ORDER BY trust_score DESC, execution_count DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_manifest(r) for r in rows]

    async def get(self, skill_id: str) -> Optional[SkillManifest]:
        """Fetch a single skill by ID."""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT * FROM skills WHERE skill_id = ?", (skill_id,)
        ).fetchone()
        return self._row_to_manifest(row) if row else None

    async def list_summaries(self) -> List["SkillSummary"]:  # noqa: F821
        """Return lightweight summaries for novelty scoring."""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT skill_id, description, tags, dependencies FROM skills WHERE status != 'archived'"
        ).fetchall()
        from colony_sidecar.skills.models import SkillSummary
        return [
            SkillSummary(
                skill_id=r["skill_id"],
                description=r["description"],
                tags=r["tags"].split(",") if r["tags"] else [],
                dependencies=r["dependencies"].split(",") if r["dependencies"] else [],
            )
            for r in rows
        ]

    async def list_all(self, status: Optional[SkillStatus] = None) -> List[SkillManifest]:
        """List all skills, optionally filtered by status."""
        assert self._conn is not None
        if status:
            rows = self._conn.execute(
                "SELECT * FROM skills WHERE status = ? ORDER BY updated_at DESC",
                (status.value,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM skills ORDER BY updated_at DESC"
            ).fetchall()
        return [self._row_to_manifest(r) for r in rows]

    def _row_to_manifest(self, row: sqlite3.Row) -> SkillManifest:
        keys = row.keys()
        raw_patterns = row["trigger_patterns"] if "trigger_patterns" in keys else "[]"
        try:
            trigger_patterns = json.loads(raw_patterns or "[]")
        except (json.JSONDecodeError, TypeError):
            trigger_patterns = []
        return SkillManifest(
            skill_id=row["skill_id"],
            name=row["name"],
            version=row["version"],
            description=row["description"],
            author_colony_id=row["author_colony_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            status=SkillStatus(row["status"]),
            entry_point=row["entry_point"],
            permissions=SkillPermissions(),
            tags=row["tags"].split(",") if row["tags"] else [],
            dependencies=row["dependencies"].split(",") if row["dependencies"] else [],
            checksum_sha256=row["checksum_sha256"],
            origin_task_id=row["origin_task_id"],
            parent_skill_id=row["parent_skill_id"],
            trust_score=row["trust_score"],
            execution_count=row["execution_count"],
            skill_dir=row["skill_dir"],
            trigger_patterns=trigger_patterns,
            context_tokens_estimate=row["context_tokens_estimate"] if "context_tokens_estimate" in keys else 2048,
            lazy_loader=row["lazy_loader"] if "lazy_loader" in keys else None,
        )

    def _apply_schema(self) -> None:
        assert self._conn is not None
        self._conn.executescript(SKILLS_SCHEMA)
