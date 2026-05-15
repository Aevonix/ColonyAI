"""Initiative store for multi-agent Colony.

Provides:
- InitiativeStore: SQLite persistence for initiatives
- Assignment history tracking
- Dead letter queue
- Timeout and expiry checks
"""

import json
import logging
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import AssignmentHistory, InitiativeStatus, StoredInitiative

logger = logging.getLogger(__name__)

# Maximum pending initiatives before rejecting new ones
MAX_PENDING_INITIATIVES = 1000


def get_state_dir() -> Path:
    """Get Colony state directory."""
    import os
    state_dir = os.environ.get("COLONY_STATE_DIR")
    if state_dir:
        return Path(state_dir)
    return Path.home() / ".colony" / "data"


class InitiativeStore:
    """Manages initiative persistence with SQLite."""

    def __init__(self, state_dir: Optional[Path] = None):
        self._state_dir = Path(state_dir) if state_dir else get_state_dir()
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._state_dir / "initiatives.db"
        self._backup_path = self._state_dir / "initiatives.db.backup"
        self._dlq_path = self._state_dir / "dead-letter-queue.jsonl"

        self._db = self._init_db()

    def _init_db(self) -> sqlite3.Connection:
        """Initialize database with recovery."""
        try:
            return self._connect()
        except sqlite3.DatabaseError:
            logger.warning("initiatives.db corrupted, attempting recovery")

            if self._backup_path.exists():
                shutil.copy(self._backup_path, self._db_path)
                logger.info("Restored initiatives.db from backup")
            else:
                self._db_path.unlink(missing_ok=True)
                logger.warning("No backup available, starting fresh")

            return self._connect()

    def _connect(self) -> sqlite3.Connection:
        """Connect to database with WAL mode."""
        # check_same_thread=False allows TestClient to access the DB from
        # a different thread (test thread vs event loop thread).
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        # WAL mode for better crash recovery
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")

        self._create_tables(conn)

        return conn

    def _create_tables(self, conn: sqlite3.Connection) -> None:
        """Create initiatives and history tables."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS initiatives (
                id TEXT PRIMARY KEY,
                dedup_key TEXT UNIQUE,
                type TEXT NOT NULL,
                description TEXT NOT NULL,
                priority REAL DEFAULT 0.5,
                rationale TEXT,
                action_hint TEXT,
                entity_id TEXT,
                
                source_type TEXT,
                source_id TEXT,
                created_by TEXT,
                
                status TEXT DEFAULT 'pending',
                assigned_agent_id TEXT,
                assigned_agent_name TEXT,
                assigned_at TIMESTAMP,
                acknowledged_at TIMESTAMP,
                completed_at TIMESTAMP,
                cancelled_at TIMESTAMP,
                cancelled_by TEXT,
                cancelled_reason TEXT,
                failed_at TIMESTAMP,
                failed_reason TEXT,
                
                attempt_count INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                timeout_seconds INTEGER DEFAULT 300,
                last_attempt_at TIMESTAMP,
                
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                
                delivery_mode TEXT DEFAULT 'websocket',
                delivery_attempts INTEGER DEFAULT 0,
                last_delivery_at TIMESTAMP,
                delivery_failed_at TIMESTAMP,
                delivery_failed_reason TEXT,
                
                result TEXT,
                result_metadata TEXT DEFAULT '{}',
                
                preferred_agent_id TEXT,
                stale_reason TEXT,
                recovery_reason TEXT
            )
            """
        )

        # Indexes
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_initiatives_status ON initiatives(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_initiatives_assigned ON initiatives(assigned_agent_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_initiatives_dedup ON initiatives(dedup_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_initiatives_priority ON initiatives(priority DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_initiatives_created ON initiatives(created_at DESC)"
        )

        # Assignment history table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assignment_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                initiative_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                agent_name TEXT,
                action TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                details TEXT,
                
                FOREIGN KEY (initiative_id) REFERENCES initiatives(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_initiative ON assignment_history(initiative_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_agent ON assignment_history(agent_id)"
        )

        conn.commit()

    # ------------------------------------------------------------------
    # CRUD Operations
    # ------------------------------------------------------------------

    def create(
        self,
        type: str,
        description: str,
        priority: float = 0.5,
        rationale: str = "",
        action_hint: Optional[str] = None,
        entity_id: Optional[str] = None,
        dedup_key: Optional[str] = None,
        source_type: Optional[str] = None,
        source_id: Optional[str] = None,
        created_by: Optional[str] = None,
        timeout_seconds: int = 300,
        expires_at: Optional[datetime] = None,
        preferred_agent_id: Optional[str] = None,
        **extra,
    ) -> StoredInitiative:
        """Create a new initiative."""
        # Check pending limit
        pending_count = self.count(status="pending")
        if pending_count >= MAX_PENDING_INITIATIVES:
            raise ValueError(
                f"Too many pending initiatives (max {MAX_PENDING_INITIATIVES})"
            )

        # Check dedup
        if dedup_key:
            existing = self.get_by_dedup_key(dedup_key)
            if existing:
                if existing.is_active:
                    logger.info(
                        "Initiative with dedup_key %s already exists: %s",
                        dedup_key,
                        existing.id,
                    )
                    return existing
                # Only reactivate FAILED initiatives so they can be retried.
                # Completed and cancelled initiatives are terminal — do NOT
                # resurrect them, or the autonomy loop will spam the agent
                # with stale follow-ups forever.
                if existing.status == InitiativeStatus.FAILED.value:
                    logger.info(
                        "Reactivating failed initiative %s with dedup_key %s",
                        existing.id,
                        dedup_key,
                    )
                    return self.update(
                        existing.id,
                        status=InitiativeStatus.PENDING.value,
                        failed_at=None,
                        failed_reason=None,
                        attempt_count=0,
                        assigned_agent_id=None,
                        assigned_agent_name=None,
                        assigned_at=None,
                        acknowledged_at=None,
                    )
                # Terminal state (completed/cancelled) — return as-is.
                logger.info(
                    "Dedup hit on terminal initiative %s (status=%s), not reactivating",
                    existing.id,
                    existing.status,
                )
                return existing

        # Bug 26: Validate priority range
        priority = max(0.0, min(1.0, priority))
        
        initiative_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        self._db.execute(
            """
            INSERT INTO initiatives (
                id, dedup_key, type, description, priority, rationale,
                action_hint, entity_id, source_type, source_id, created_by,
                timeout_seconds, expires_at, preferred_agent_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                initiative_id,
                dedup_key,
                type,
                description,
                priority,
                rationale,
                action_hint,
                entity_id,
                source_type,
                source_id,
                created_by,
                timeout_seconds,
                expires_at.isoformat() if expires_at else None,
                preferred_agent_id,
                now.isoformat(),
            ],
        )
        self._db.commit()

        return self.get(initiative_id)

    def get(self, initiative_id: str) -> Optional[StoredInitiative]:
        """Get initiative by ID."""
        cursor = self._db.execute(
            "SELECT * FROM initiatives WHERE id = ?",
            [initiative_id],
        )
        row = cursor.fetchone()
        if row:
            return StoredInitiative.from_row(dict(row))
        return None

    def get_by_dedup_key(self, dedup_key: str) -> Optional[StoredInitiative]:
        """Get initiative by dedup key."""
        cursor = self._db.execute(
            "SELECT * FROM initiatives WHERE dedup_key = ?",
            [dedup_key],
        )
        row = cursor.fetchone()
        if row:
            return StoredInitiative.from_row(dict(row))
        return None

    def update(self, initiative_id: str, **updates) -> Optional[StoredInitiative]:
        """Update initiative fields."""
        if not updates:
            return self.get(initiative_id)

        # Build SET clause
        set_parts = []
        params = []

        for key, value in updates.items():
            if key in ("result_metadata",):
                set_parts.append(f"{key} = ?")
                params.append(json.dumps(value) if not isinstance(value, str) else value)
            elif key in (
                "assigned_at",
                "acknowledged_at",
                "completed_at",
                "cancelled_at",
                "failed_at",
                "expires_at",
                "last_attempt_at",
                "last_delivery_at",
                "delivery_failed_at",
            ):
                set_parts.append(f"{key} = ?")
                if isinstance(value, datetime):
                    params.append(value.isoformat())
                else:
                    params.append(value)
            else:
                set_parts.append(f"{key} = ?")
                params.append(value)

        if not set_parts:
            return self.get(initiative_id)

        params.append(initiative_id)
        query = f"UPDATE initiatives SET {', '.join(set_parts)} WHERE id = ?"

        self._db.execute(query, params)
        self._db.commit()

        return self.get(initiative_id)

    def list(
        self,
        status: Optional[List[str]] = None,
        type: Optional[str] = None,
        assigned_agent_id: Optional[str] = None,
        created_before: Optional[datetime] = None,
        created_after: Optional[datetime] = None,
        acknowledged_before: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[StoredInitiative]:
        """List initiatives with filters."""
        query = "SELECT * FROM initiatives WHERE 1=1"
        params: List[Any] = []

        if status:
            placeholders = ",".join("?" * len(status))
            query += f" AND status IN ({placeholders})"
            params.extend(status)

        if type:
            query += " AND type = ?"
            params.append(type)

        if assigned_agent_id:
            query += " AND assigned_agent_id = ?"
            params.append(assigned_agent_id)

        if created_before:
            query += " AND created_at < ?"
            params.append(created_before.isoformat())

        if created_after:
            query += " AND created_at > ?"
            params.append(created_after.isoformat())

        if acknowledged_before:
            query += " AND acknowledged_at < ?"
            params.append(acknowledged_before.isoformat())

        query += " ORDER BY priority DESC, created_at ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = self._db.execute(query, params)
        return [StoredInitiative.from_row(dict(row)) for row in cursor.fetchall()]

    def count(
        self,
        status: Optional[List[str]] = None,
        assigned_agent_id: Optional[str] = None,
    ) -> int:
        """Count initiatives with filters."""
        query = "SELECT COUNT(*) FROM initiatives WHERE 1=1"
        params: List[Any] = []

        if status:
            placeholders = ",".join("?" * len(status))
            query += f" AND status IN ({placeholders})"
            params.extend(status)

        if assigned_agent_id:
            query += " AND assigned_agent_id = ?"
            params.append(assigned_agent_id)

        cursor = self._db.execute(query, params)
        return cursor.fetchone()[0]

    # ------------------------------------------------------------------
    # Status Transitions
    # ------------------------------------------------------------------

    def assign(
        self,
        initiative_id: str,
        agent_id: str,
        agent_name: Optional[str] = None,
    ) -> Optional[StoredInitiative]:
        """Assign initiative to agent (atomic)."""
        now = datetime.now(timezone.utc)

        # Atomic UPDATE - only works on pending initiatives
        cursor = self._db.execute(
            """
            UPDATE initiatives
            SET status = ?,
                assigned_agent_id = ?,
                assigned_agent_name = ?,
                assigned_at = ?
            WHERE id = ? AND status = ?
            """,
            [
                InitiativeStatus.ASSIGNED.value,
                agent_id,
                agent_name,
                now.isoformat(),
                initiative_id,
                InitiativeStatus.PENDING.value,
            ],
        )
        self._db.commit()

        if cursor.rowcount == 0:
            return None

        # Log history
        self.log_history(
            initiative_id,
            action="assigned",
            agent_id=agent_id,
            agent_name=agent_name,
        )

        return self.get(initiative_id)

    def acknowledge(
        self,
        initiative_id: str,
        agent_id: str,
    ) -> Optional[StoredInitiative]:
        """Mark initiative as acknowledged by agent."""
        now = datetime.now(timezone.utc)

        initiative = self.get(initiative_id)
        if not initiative or initiative.assigned_agent_id != agent_id:
            return None

        updated = self.update(
            initiative_id,
            status=InitiativeStatus.ACKNOWLEDGED.value,
            acknowledged_at=now,
        )

        if updated:
            self.log_history(
                initiative_id,
                action="acknowledged",
                agent_id=agent_id,
            )

        return updated

    def complete(
        self,
        initiative_id: str,
        agent_id: str,
        result: Optional[str] = None,
        result_metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[StoredInitiative]:
        """Mark initiative as completed."""
        now = datetime.now(timezone.utc)

        initiative = self.get(initiative_id)
        if not initiative:
            return None

        # Check if expired
        if initiative.is_expired:
            return self.update(
                initiative_id,
                status=InitiativeStatus.FAILED.value,
                failed_at=now,
                failed_reason="initiative_expired",
            )

        updated = self.update(
            initiative_id,
            status=InitiativeStatus.COMPLETED.value,
            completed_at=now,
            result=result,
            result_metadata=result_metadata or {},
        )

        if updated:
            self.log_history(
                initiative_id,
                action="completed",
                agent_id=agent_id,
                details={"result": result},
            )

        return updated

    def fail(
        self,
        initiative_id: str,
        agent_id: str,
        reason: str,
        retry: bool = False,
    ) -> Optional[StoredInitiative]:
        """Mark initiative as failed."""
        now = datetime.now(timezone.utc)

        initiative = self.get(initiative_id)
        if not initiative:
            return None

        new_attempt_count = initiative.attempt_count + 1

        # If retry requested and attempts remaining, reset to pending
        if retry and new_attempt_count < initiative.max_attempts:
            updated = self.update(
                initiative_id,
                status=InitiativeStatus.PENDING.value,
                assigned_agent_id=None,
                assigned_agent_name=None,
                assigned_at=None,
                acknowledged_at=None,
                attempt_count=new_attempt_count,
                last_attempt_at=now,
            )

            self.log_history(
                initiative_id,
                action="retry_scheduled",
                agent_id=agent_id,
                details={"reason": reason, "attempt": new_attempt_count},
            )
        else:
            # Mark as failed
            updated = self.update(
                initiative_id,
                status=InitiativeStatus.FAILED.value,
                failed_at=now,
                failed_reason=reason,
                attempt_count=new_attempt_count,
                last_attempt_at=now,
            )

            self.log_history(
                initiative_id,
                action="failed",
                agent_id=agent_id,
                details={"reason": reason, "attempt": new_attempt_count},
            )

            # Add to dead letter queue if max attempts reached
            if new_attempt_count >= initiative.max_attempts:
                self._add_to_dlq(initiative, reason)

        return updated

    def cancel(
        self,
        initiative_id: str,
        cancelled_by: str,
        reason: Optional[str] = None,
    ) -> Optional[StoredInitiative]:
        """Cancel an initiative."""
        now = datetime.now(timezone.utc)

        initiative = self.get(initiative_id)
        if not initiative or not initiative.is_active:
            return None

        updated = self.update(
            initiative_id,
            status=InitiativeStatus.CANCELLED.value,
            cancelled_at=now,
            cancelled_by=cancelled_by,
            cancelled_reason=reason,
        )

        if updated:
            self.log_history(
                initiative_id,
                action="cancelled",
                agent_id=cancelled_by,
                details={"reason": reason},
            )

        return updated

    # ------------------------------------------------------------------
    # Reassignment
    # ------------------------------------------------------------------

    def reassign_from_agent(
        self,
        agent_id: str,
        only_pending: bool = True,
    ) -> int:
        """Reassign initiatives from an agent.

        Args:
            agent_id: Agent to reassign from
            only_pending: If True, only reassign PENDING (not ACKNOWLEDGED)

        Returns:
            Number of initiatives reassigned
        """
        if only_pending:
            initiatives = self.list(
                status=[InitiativeStatus.PENDING.value],
                assigned_agent_id=agent_id,
            )
        else:
            initiatives = self.list(
                status=[
                    InitiativeStatus.PENDING.value,
                    InitiativeStatus.ASSIGNED.value,
                    InitiativeStatus.ACKNOWLEDGED.value,
                ],
                assigned_agent_id=agent_id,
            )

        reassigned = 0
        for init in initiatives:
            updated = self.update(
                init.id,
                status=InitiativeStatus.PENDING.value,
                assigned_agent_id=None,
                assigned_agent_name=None,
                assigned_at=None,
                acknowledged_at=None,
                recovery_reason="agent_offline",
            )

            if updated:
                self.log_history(
                    init.id,
                    action="reassigned",
                    agent_id=agent_id,
                    details={"reason": "agent_offline", "only_pending": only_pending},
                )
                reassigned += 1

        return reassigned

    # ------------------------------------------------------------------
    # Timeout & Expiry
    # ------------------------------------------------------------------

    def find_timed_out(self, now: datetime) -> List[StoredInitiative]:
        """Find initiatives that have exceeded their timeout."""
        cursor = self._db.execute(
            """
            SELECT * FROM initiatives
            WHERE status IN ('assigned', 'acknowledged')
            AND timeout_seconds IS NOT NULL
            AND assigned_at IS NOT NULL
            AND datetime(assigned_at, '+' || timeout_seconds || ' seconds') < ?
            """,
            [now.isoformat()],
        )
        return [StoredInitiative.from_row(dict(row)) for row in cursor.fetchall()]

    def find_expired(self, now: datetime) -> List[StoredInitiative]:
        """Find initiatives that have expired."""
        cursor = self._db.execute(
            """
            SELECT * FROM initiatives
            WHERE status IN ('pending', 'assigned', 'acknowledged')
            AND expires_at IS NOT NULL
            AND expires_at < ?
            """,
            [now.isoformat()],
        )
        return [StoredInitiative.from_row(dict(row)) for row in cursor.fetchall()]

    def find_stale_acknowledged(
        self,
        threshold: datetime,
    ) -> List[StoredInitiative]:
        """Find initiatives stuck in acknowledged state."""
        cursor = self._db.execute(
            """
            SELECT * FROM initiatives
            WHERE status = ?
            AND acknowledged_at < ?
            """,
            [InitiativeStatus.ACKNOWLEDGED.value, threshold.isoformat()],
        )
        return [StoredInitiative.from_row(dict(row)) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def log_history(
        self,
        initiative_id: str,
        action: str,
        agent_id: str,
        agent_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log assignment history."""
        self._db.execute(
            """
            INSERT INTO assignment_history (
                initiative_id, agent_id, agent_name, action, details
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                initiative_id,
                agent_id,
                agent_name,
                action,
                json.dumps(details) if details else None,
            ],
        )
        self._db.commit()

    def get_history(
        self,
        initiative_id: str,
        limit: int = 50,
    ) -> List[AssignmentHistory]:
        """Get assignment history for an initiative."""
        cursor = self._db.execute(
            """
            SELECT * FROM assignment_history
            WHERE initiative_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            [initiative_id, limit],
        )
        return [AssignmentHistory.from_row(dict(row)) for row in cursor.fetchall()]

    def get_agent_history(
        self,
        agent_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[AssignmentHistory]:
        """Get assignment history for an agent."""
        cursor = self._db.execute(
            """
            SELECT * FROM assignment_history
            WHERE agent_id = ?
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """,
            [agent_id, limit, offset],
        )
        return [AssignmentHistory.from_row(dict(row)) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Dead Letter Queue
    # ------------------------------------------------------------------

    def _add_to_dlq(
        self,
        initiative: StoredInitiative,
        reason: str,
    ) -> None:
        """Add failed initiative to dead letter queue."""
        entry = {
            "initiative_id": initiative.id,
            "type": initiative.type,
            "description": initiative.description,
            "reason": reason,
            "attempt_count": initiative.attempt_count,
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }

        with open(self._dlq_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

        logger.warning(
            "Added initiative %s to dead letter queue: %s",
            initiative.id,
            reason,
        )

    def get_dlq_entries(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get entries from dead letter queue."""
        if not self._dlq_path.exists():
            return []

        entries = []
        with open(self._dlq_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        return entries[-limit:]

    def remove_from_dlq(self, initiative_id: str) -> bool:
        """Remove initiative from dead letter queue."""
        if not self._dlq_path.exists():
            return False

        entries = []
        removed = False

        with open(self._dlq_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        if entry.get("initiative_id") == initiative_id:
                            removed = True
                        else:
                            entries.append(entry)
                    except json.JSONDecodeError:
                        continue

        if removed:
            with open(self._dlq_path, "w") as f:
                for entry in entries:
                    f.write(json.dumps(entry) + "\n")

        return removed

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def delete_old(
        self,
        status: List[str],
        before: datetime,
    ) -> int:
        """Delete old initiatives with given statuses."""
        placeholders = ",".join("?" * len(status))
        cursor = self._db.execute(
            f"""
            DELETE FROM initiatives
            WHERE status IN ({placeholders})
            AND created_at < ?
            """,
            [*status, before.isoformat()],
        )
        self._db.commit()
        return cursor.rowcount

    def backup(self) -> None:
        """Create backup of database."""
        shutil.copy2(self._db_path, self._backup_path)

    def close(self) -> None:
        """Close connection and create backup."""
        self.backup()
        self._db.close()
