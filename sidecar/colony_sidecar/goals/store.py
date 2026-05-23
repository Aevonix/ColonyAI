"""GoalStore — SQLite-backed persistence for Goals and GoalDAGs.

Uses synchronous sqlite3 for simplicity (goals are not high-throughput).
WAL mode enabled for safe concurrent reads.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from colony_sidecar import get_state_dir

from colony_sidecar.goals.models import (
    Goal,
    GoalDAG,
    GoalOutcome,
    GoalPriority,
    GoalSource,
    GoalStatus,
    GoalTransitionRecord,
    Subtask,
    SubtaskStatus,
)

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_goal_store_instance: Optional["GoalStore"] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


class GoalNotFoundError(KeyError):
    """Raised when a goal_id is not found in the store."""


class GoalStore:
    """Persistent storage for goals and DAGs (SQLite + optional Neo4j mirror).

    Thread-safe for single-process use (sqlite3 serialised mode).
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        # ":memory:" for tests; actual path for production
        self._db_path = db_path or ":memory:"
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        conn = self._get_conn()
        schema = _SCHEMA_PATH.read_text()
        conn.executescript(schema)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @classmethod
    def get_instance(cls) -> "GoalStore":
        """Return the process-wide singleton backed by ~/.colony/data/goals.db."""
        global _goal_store_instance
        if _goal_store_instance is None:
            colony_home = get_state_dir()
            _goal_store_instance = cls(str(colony_home / "goals.db"))
        return _goal_store_instance

    def list(
        self,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> dict:
        """Return a paginated dict of goals for the API router."""
        offset = 0
        if cursor:
            try:
                offset = int(cursor)
            except ValueError:
                pass

        goal_status = None
        if status:
            try:
                goal_status = GoalStatus(status)
            except ValueError:
                pass

        goals = self.list_goals(status=goal_status, limit=limit + 1, offset=offset)

        if priority:
            goals = [g for g in goals if (g.priority.value if hasattr(g.priority, 'value') else g.priority) == priority]

        has_more = len(goals) > limit
        if has_more:
            goals = goals[:limit]

        items = [
            {
                "goal_id": g.goal_id,
                "title": g.title,
                "description": g.description,
                "status": g.status.value if hasattr(g.status, 'value') else g.status,
                "priority": g.priority.value if hasattr(g.priority, 'value') else g.priority,
                "source": g.source.value if hasattr(g.source, 'value') else g.source,
                "created_at": g.created_at.isoformat(),
                "updated_at": g.updated_at.isoformat(),
                "tags": g.tags,
                "progress_pct": g.progress_pct,
            }
            for g in goals
        ]
        return {
            "data": items,
            "meta": {
                "total": len(items),
                "page_size": limit,
                "has_more": has_more,
                "cursor": str(offset + limit) if has_more else None,
            },
        }

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ── Goal CRUD ──────────────────────────────────────────────────────────────

    def save_goal(self, goal: Goal) -> None:
        """Insert or update a goal record."""
        goal.updated_at = datetime.now(timezone.utc)
        outcome_json = None
        if goal.outcome:
            outcome_json = json.dumps({
                "description": goal.outcome.description,
                "success_criteria": goal.outcome.success_criteria,
                "measurable": goal.outcome.measurable,
                "target_value": goal.outcome.target_value,
                "target_unit": goal.outcome.target_unit,
            })

        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO goals (
                    goal_id, title, description, source, status, priority,
                    outcome_json, deadline, parent_goal_id,
                    tags_json, context_json,
                    created_at, updated_at, accepted_at, completed_at,
                    abandoned_at, abandon_reason,
                    replan_count, estimated_hours, progress_pct,
                    last_initiative_at, snoozed_until, snooze_count, dismissal_reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(goal_id) DO UPDATE SET
                    title=excluded.title,
                    description=excluded.description,
                    source=excluded.source,
                    status=excluded.status,
                    priority=excluded.priority,
                    outcome_json=excluded.outcome_json,
                    deadline=excluded.deadline,
                    parent_goal_id=excluded.parent_goal_id,
                    tags_json=excluded.tags_json,
                    context_json=excluded.context_json,
                    updated_at=excluded.updated_at,
                    accepted_at=excluded.accepted_at,
                    completed_at=excluded.completed_at,
                    abandoned_at=excluded.abandoned_at,
                    abandon_reason=excluded.abandon_reason,
                    replan_count=excluded.replan_count,
                    estimated_hours=excluded.estimated_hours,
                    progress_pct=excluded.progress_pct,
                    last_initiative_at=excluded.last_initiative_at,
                    snoozed_until=excluded.snoozed_until,
                    snooze_count=excluded.snooze_count,
                    dismissal_reason=excluded.dismissal_reason
                """,
                (
                    goal.goal_id,
                    goal.title,
                    goal.description,
                    goal.source.value if hasattr(goal.source, 'value') else goal.source,
                    goal.status.value if hasattr(goal.status, 'value') else goal.status,
                    goal.priority.value if hasattr(goal.priority, 'value') else goal.priority,
                    outcome_json,
                    goal.deadline.isoformat() if goal.deadline else None,
                    goal.parent_goal_id,
                    json.dumps(goal.tags),
                    json.dumps(goal.context),
                    goal.created_at.isoformat(),
                    goal.updated_at.isoformat(),
                    goal.accepted_at.isoformat() if goal.accepted_at else None,
                    goal.completed_at.isoformat() if goal.completed_at else None,
                    goal.abandoned_at.isoformat() if goal.abandoned_at else None,
                    goal.abandon_reason,
                    goal.replan_count,
                    goal.estimated_hours,
                    goal.progress_pct,
                    goal.last_initiative_at.isoformat() if goal.last_initiative_at else None,
                    goal.snoozed_until.isoformat() if goal.snoozed_until else None,
                    goal.snooze_count,
                    goal.dismissal_reason,
                ),
            )
        try:
            from colony_sidecar.events.broadcaster import emit as _emit
            _emit("goal_update", {
                "goal_id": goal.goal_id,
                "status": goal.status.value if hasattr(goal.status, 'value') else goal.status,
                "progress_pct": goal.progress_pct,
                "title": goal.title,
            })
        except Exception:
            logger.debug("Goal update event broadcast failed", exc_info=True)

    def get_goal(self, goal_id: str) -> Goal:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM goals WHERE goal_id = ?", (goal_id,)
        ).fetchone()
        if row is None:
            raise GoalNotFoundError(f"Goal not found: {goal_id}")
        return self._goal_from_row(row)

    def list_goals(
        self,
        status: Optional[GoalStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Goal]:
        conn = self._get_conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM goals WHERE status = ? ORDER BY priority DESC, created_at DESC LIMIT ? OFFSET ?",
                (status.value if hasattr(status, 'value') else status, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM goals ORDER BY priority DESC, created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._goal_from_row(r) for r in rows]

    def delete_goal(self, goal_id: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM subtasks WHERE goal_id = ?", (goal_id,))
            conn.execute("DELETE FROM goal_audit_log WHERE goal_id = ?", (goal_id,))
            conn.execute("DELETE FROM goal_dag_versions WHERE goal_id = ?", (goal_id,))
            conn.execute("DELETE FROM goals WHERE goal_id = ?", (goal_id,))

    def _goal_from_row(self, row: sqlite3.Row) -> Goal:
        outcome = None
        if row["outcome_json"]:
            d = json.loads(row["outcome_json"])
            outcome = GoalOutcome(
                description=d.get("description", ""),
                success_criteria=d.get("success_criteria", []),
                measurable=d.get("measurable", False),
                target_value=d.get("target_value"),
                target_unit=d.get("target_unit"),
            )
        return Goal(
            goal_id=row["goal_id"],
            title=row["title"],
            description=row["description"],
            source=GoalSource(row["source"]),
            status=GoalStatus(row["status"]),
            priority=GoalPriority(row["priority"]),
            outcome=outcome,
            deadline=_parse_dt(row["deadline"]),
            parent_goal_id=row["parent_goal_id"],
            tags=json.loads(row["tags_json"] or "{}"),
            context=json.loads(row["context_json"] or "{}"),
            created_at=_parse_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_parse_dt(row["updated_at"]) or datetime.now(timezone.utc),
            accepted_at=_parse_dt(row["accepted_at"]),
            completed_at=_parse_dt(row["completed_at"]),
            abandoned_at=_parse_dt(row["abandoned_at"]),
            abandon_reason=row["abandon_reason"],
            replan_count=row["replan_count"],
            estimated_hours=row["estimated_hours"],
            progress_pct=row["progress_pct"],
            last_initiative_at=_parse_dt(row["last_initiative_at"]),
            snoozed_until=_parse_dt(row["snoozed_until"]),
            snooze_count=row["snooze_count"],
            dismissal_reason=row["dismissal_reason"],
        )

    # ── Subtask CRUD ───────────────────────────────────────────────────────────

    def save_subtask(self, subtask: Subtask, dag_version: int = 1) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO subtasks (
                    subtask_id, goal_id, title, job_type,
                    payload_json, capabilities_json, depends_on_json,
                    status, job_id, result_json,
                    depth, is_critical_path, retry_count, max_retries,
                    estimated_hours, started_at, completed_at, error, dag_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(subtask_id) DO UPDATE SET
                    title=excluded.title,
                    job_type=excluded.job_type,
                    payload_json=excluded.payload_json,
                    capabilities_json=excluded.capabilities_json,
                    depends_on_json=excluded.depends_on_json,
                    status=excluded.status,
                    job_id=excluded.job_id,
                    result_json=excluded.result_json,
                    depth=excluded.depth,
                    is_critical_path=excluded.is_critical_path,
                    retry_count=excluded.retry_count,
                    max_retries=excluded.max_retries,
                    estimated_hours=excluded.estimated_hours,
                    started_at=excluded.started_at,
                    completed_at=excluded.completed_at,
                    error=excluded.error,
                    dag_version=excluded.dag_version
                """,
                (
                    subtask.subtask_id,
                    subtask.goal_id,
                    subtask.title,
                    subtask.job_type,
                    json.dumps(subtask.payload),
                    json.dumps(subtask.capabilities),
                    json.dumps(subtask.depends_on),
                    subtask.status.value if hasattr(subtask.status, 'value') else subtask.status,
                    subtask.job_id,
                    json.dumps(subtask.result) if subtask.result else None,
                    subtask.depth,
                    1 if subtask.is_critical_path else 0,
                    subtask.retry_count,
                    subtask.max_retries,
                    subtask.estimated_hours,
                    subtask.started_at.isoformat() if subtask.started_at else None,
                    subtask.completed_at.isoformat() if subtask.completed_at else None,
                    subtask.error,
                    dag_version,
                ),
            )

    def get_subtasks(self, goal_id: str, dag_version: Optional[int] = None) -> List[Subtask]:
        conn = self._get_conn()
        if dag_version is not None:
            rows = conn.execute(
                "SELECT * FROM subtasks WHERE goal_id = ? AND dag_version = ?",
                (goal_id, dag_version),
            ).fetchall()
        else:
            # Latest version for each subtask_id
            rows = conn.execute(
                """
                SELECT * FROM subtasks WHERE goal_id = ?
                AND dag_version = (
                    SELECT MAX(dag_version) FROM subtasks s2
                    WHERE s2.goal_id = subtasks.goal_id
                )
                """,
                (goal_id,),
            ).fetchall()
        return [self._subtask_from_row(r) for r in rows]

    def _subtask_from_row(self, row: sqlite3.Row) -> Subtask:
        return Subtask(
            subtask_id=row["subtask_id"],
            goal_id=row["goal_id"],
            title=row["title"],
            job_type=row["job_type"],
            payload=json.loads(row["payload_json"] or "{}"),
            capabilities=json.loads(row["capabilities_json"] or "[]"),
            depends_on=json.loads(row["depends_on_json"] or "[]"),
            status=SubtaskStatus(row["status"]),
            job_id=row["job_id"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            depth=row["depth"],
            is_critical_path=bool(row["is_critical_path"]),
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            estimated_hours=row["estimated_hours"],
            started_at=_parse_dt(row["started_at"]),
            completed_at=_parse_dt(row["completed_at"]),
            error=row["error"],
        )

    # ── DAG CRUD ───────────────────────────────────────────────────────────────

    def save_dag(self, dag: GoalDAG) -> None:
        """Persist a GoalDAG (subtasks + version snapshot)."""
        # Save each subtask
        for subtask in dag.subtasks.values():
            self.save_subtask(subtask, dag_version=dag.version)

        # Persist full DAG snapshot for version history
        dag_dict = {
            "goal_id": dag.goal_id,
            "root_ids": dag.root_ids,
            "leaf_ids": dag.leaf_ids,
            "critical_path": dag.critical_path,
            "max_depth": dag.max_depth,
            "version": dag.version,
            "created_at": dag.created_at.isoformat(),
            "subtasks": {
                sid: {
                    "subtask_id": s.subtask_id,
                    "goal_id": s.goal_id,
                    "title": s.title,
                    "job_type": s.job_type,
                    "payload": s.payload,
                    "capabilities": s.capabilities,
                    "depends_on": s.depends_on,
                    "status": s.status.value if hasattr(s.status, 'value') else s.status,
                    "job_id": s.job_id,
                    "result": s.result,
                    "depth": s.depth,
                    "is_critical_path": s.is_critical_path,
                    "retry_count": s.retry_count,
                    "max_retries": s.max_retries,
                    "estimated_hours": s.estimated_hours,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                    "error": s.error,
                }
                for sid, s in dag.subtasks.items()
            },
        }
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO goal_dag_versions (goal_id, version, dag_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(goal_id, version) DO UPDATE SET dag_json=excluded.dag_json
                """,
                (dag.goal_id, dag.version, json.dumps(dag_dict), _now_iso()),
            )

    def get_dag(self, goal_id: str) -> Optional[GoalDAG]:
        """Retrieve the latest DAG for a goal."""
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT dag_json FROM goal_dag_versions
            WHERE goal_id = ?
            ORDER BY version DESC LIMIT 1
            """,
            (goal_id,),
        ).fetchone()
        if row is None:
            return None
        return self._dag_from_json(json.loads(row["dag_json"]))

    def _dag_from_json(self, d: Dict[str, Any]) -> GoalDAG:
        subtasks = {}
        for sid, s in d.get("subtasks", {}).items():
            subtasks[sid] = Subtask(
                subtask_id=s["subtask_id"],
                goal_id=s["goal_id"],
                title=s["title"],
                job_type=s.get("job_type", "custom"),
                payload=s.get("payload", {}),
                capabilities=s.get("capabilities", []),
                depends_on=s.get("depends_on", []),
                status=SubtaskStatus(s.get("status", "pending")),
                job_id=s.get("job_id"),
                result=s.get("result"),
                depth=s.get("depth", 0),
                is_critical_path=bool(s.get("is_critical_path", False)),
                retry_count=s.get("retry_count", 0),
                max_retries=s.get("max_retries", 2),
                estimated_hours=s.get("estimated_hours"),
                started_at=_parse_dt(s.get("started_at")),
                completed_at=_parse_dt(s.get("completed_at")),
                error=s.get("error"),
            )
        return GoalDAG(
            goal_id=d["goal_id"],
            subtasks=subtasks,
            root_ids=d.get("root_ids", []),
            leaf_ids=d.get("leaf_ids", []),
            critical_path=d.get("critical_path", []),
            max_depth=d.get("max_depth", 0),
            version=d.get("version", 1),
            created_at=_parse_dt(d.get("created_at")) or datetime.now(timezone.utc),
        )

    # ── Audit Log ──────────────────────────────────────────────────────────────

    def log_transition(
        self,
        goal_id: str,
        from_status: GoalStatus,
        to_status: GoalStatus,
        trigger: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO goal_audit_log (goal_id, from_status, to_status, trigger, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    goal_id,
                    from_status.value if hasattr(from_status, 'value') else from_status,
                    to_status.value if hasattr(to_status, 'value') else to_status,
                    trigger,
                    _now_iso(),
                    json.dumps(metadata or {}),
                ),
            )

    def get_audit_trail(self, goal_id: str) -> List[GoalTransitionRecord]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM goal_audit_log WHERE goal_id = ? ORDER BY created_at ASC",
            (goal_id,),
        ).fetchall()
        return [
            GoalTransitionRecord(
                goal_id=r["goal_id"],
                from_status=r["from_status"],
                to_status=r["to_status"],
                trigger=r["trigger"],
                created_at=_parse_dt(r["created_at"]) or datetime.now(timezone.utc),
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
            for r in rows
        ]

    # ── Initiative Task Management (v0.7.10) ──────────────────────────────────

    # Maximum snooze count before auto-dismissal
    MAX_SNOOZE_COUNT = 3

    def complete_task(self, goal_id: str) -> bool:
        """Mark a goal/task as completed."""
        try:
            goal = self.get_goal(goal_id)
        except GoalNotFoundError:
            return False
        goal.status = GoalStatus.COMPLETED
        goal.completed_at = datetime.now(timezone.utc)
        goal.updated_at = datetime.now(timezone.utc)
        self.save_goal(goal)
        self.log_transition(
            goal_id, GoalStatus(goal.status.value if hasattr(goal.status, 'value') else goal.status),
            GoalStatus.COMPLETED, trigger="llm_complete",
        )
        return True

    def snooze_task(self, goal_id: str, hours: int, reason: str = "") -> bool:
        """Snooze a goal/task for N hours.

        If snooze_count >= MAX_SNOOZE_COUNT, auto-dismiss instead.
        """
        try:
            goal = self.get_goal(goal_id)
        except GoalNotFoundError:
            return False

        hours = min(hours, 168)  # Cap at 1 week

        goal.snooze_count += 1
        if goal.snooze_count >= self.MAX_SNOOZE_COUNT:
            # Snooze fatigue: auto-dismiss after too many snoozes
            goal.status = GoalStatus.ABANDONED
            goal.abandoned_at = datetime.now(timezone.utc)
            goal.abandon_reason = f"auto_dismissed: snoozed {goal.snooze_count} times"
            goal.dismissal_reason = "snooze_fatigue"
            goal.updated_at = datetime.now(timezone.utc)
            self.save_goal(goal)
            self.log_transition(
                goal_id, goal.status, GoalStatus.ABANDONED,
                trigger="snooze_fatigue",
                metadata={"snooze_count": goal.snooze_count},
            )
            logger.info("Auto-dismissed goal %s after %d snoozes", goal_id, goal.snooze_count)
            return True

        goal.snoozed_until = datetime.now(timezone.utc) + timedelta(hours=hours)
        goal.updated_at = datetime.now(timezone.utc)
        self.save_goal(goal)
        return True

    def dismiss_task(self, goal_id: str, reason: str = "stale") -> bool:
        """Dismiss a goal/task as no longer relevant."""
        try:
            goal = self.get_goal(goal_id)
        except GoalNotFoundError:
            return False

        goal.status = GoalStatus.ABANDONED
        goal.abandoned_at = datetime.now(timezone.utc)
        goal.abandon_reason = reason
        goal.dismissal_reason = reason
        goal.updated_at = datetime.now(timezone.utc)
        self.save_goal(goal)
        self.log_transition(
            goal_id, goal.status, GoalStatus.ABANDONED,
            trigger="llm_dismiss", metadata={"reason": reason},
        )
        return True

    def get_active_tasks(self, cooldown_hours: float = 12.0) -> List[Goal]:
        """Get goals that should generate initiatives.

        Filters out:
        - Non-pending/proposed/accepted/active goals
        - Snoozed goals (snoozed_until > now)
        - Goals that had an initiative within cooldown period
        """
        now = datetime.now(timezone.utc)
        cooldown_delta = timedelta(hours=cooldown_hours)

        candidates = []
        for status in (GoalStatus.PROPOSED, GoalStatus.ACCEPTED, GoalStatus.ACTIVE, GoalStatus.BLOCKED):
            candidates.extend(self.list_goals(status=status, limit=200))

        active = []
        for goal in candidates:
            # Skip snoozed
            if goal.snoozed_until and goal.snoozed_until > now:
                continue
            # Skip if initiative generated within cooldown
            if goal.last_initiative_at and (now - goal.last_initiative_at) < cooldown_delta:
                continue
            active.append(goal)

        return active

    def mark_initiative_generated(self, goal_id: str) -> bool:
        """Mark that an initiative was just generated for this goal."""
        try:
            goal = self.get_goal(goal_id)
        except GoalNotFoundError:
            return False
        goal.last_initiative_at = datetime.now(timezone.utc)
        goal.updated_at = datetime.now(timezone.utc)
        self.save_goal(goal)
        return True
