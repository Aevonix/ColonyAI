"""Autonomy Scheduler — periodic task scheduling for Colony subsystems."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


class TaskSchedule:
    """A scheduled periodic task."""

    def __init__(
        self,
        id: str,
        name: str,
        interval_seconds: int,
        callback_name: str,
        last_run: Optional[datetime] = None,
        next_run: Optional[datetime] = None,
        enabled: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.id = id
        self.name = name
        self.interval_seconds = interval_seconds
        self.callback_name = callback_name
        self.last_run = last_run
        self.next_run = next_run or datetime.now(timezone.utc)
        self.enabled = enabled
        self.metadata = metadata or {}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "interval_seconds": self.interval_seconds,
            "callback_name": self.callback_name,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "enabled": self.enabled,
            "metadata": self.metadata,
        }


class ScheduleStore:
    """SQLite-backed schedule persistence."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._init_db()

    def _init_db(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    callback_name TEXT NOT NULL,
                    last_run TEXT,
                    next_run TEXT,
                    enabled INTEGER DEFAULT 1,
                    metadata TEXT DEFAULT '{}'
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def upsert(self, schedule: TaskSchedule) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO schedules (id, name, interval_seconds, callback_name, last_run, next_run, enabled, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    name=excluded.name, interval_seconds=excluded.interval_seconds,
                    callback_name=excluded.callback_name, next_run=excluded.next_run,
                    enabled=excluded.enabled, metadata=excluded.metadata
            """, (
                schedule.id, schedule.name, schedule.interval_seconds,
                schedule.callback_name,
                schedule.last_run.isoformat() if schedule.last_run else None,
                schedule.next_run.isoformat() if schedule.next_run else None,
                1 if schedule.enabled else 0,
                json.dumps(schedule.metadata),
            ))

    def get_due(self) -> List[TaskSchedule]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE enabled=1 AND next_run <= ?",
                (now,),
            ).fetchall()
            return [self._row_to_schedule(r) for r in rows]

    def update_last_run(self, schedule_id: str) -> None:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            row = conn.execute("SELECT interval_seconds FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            if row:
                interval = row["interval_seconds"]
                next_run = datetime.fromtimestamp(now.timestamp() + interval, tz=timezone.utc)
                conn.execute(
                    "UPDATE schedules SET last_run=?, next_run=? WHERE id=?",
                    (now.isoformat(), next_run.isoformat(), schedule_id),
                )

    def list_all(self) -> List[TaskSchedule]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM schedules ORDER BY next_run").fetchall()
            return [self._row_to_schedule(r) for r in rows]

    def set_enabled(self, schedule_id: str, enabled: bool) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE schedules SET enabled=? WHERE id=?",
                (1 if enabled else 0, schedule_id),
            )
            return cursor.rowcount > 0

    def _row_to_schedule(self, row: sqlite3.Row) -> TaskSchedule:
        return TaskSchedule(
            id=row["id"],
            name=row["name"],
            interval_seconds=row["interval_seconds"],
            callback_name=row["callback_name"],
            last_run=datetime.fromisoformat(row["last_run"]) if row["last_run"] else None,
            next_run=datetime.fromisoformat(row["next_run"]) if row["next_run"] else None,
            enabled=bool(row["enabled"]),
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )


class AutonomyScheduler:
    """Lightweight periodic task scheduler for Colony subsystems.

    Not a full cron daemon — just enough for Colony's periodic needs:
    memory consolidation, briefing generation, signal ingestion, etc.

    Usage:
        scheduler = AutonomyScheduler(db_path="schedules.db")
        scheduler.register("memory_consolidate", my_callback, interval_seconds=3600)
        results = await scheduler.tick()  # Called by autonomy loop
    """

    def __init__(self, db_path: str):
        self._store = ScheduleStore(db_path)
        self._callbacks: Dict[str, Callable] = {}

    def register(
        self,
        name: str,
        callback: Union[Callable, Callable[..., Coroutine]],
        interval_seconds: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Register a periodic task.

        If a task with this name already exists, updates the interval and callback.
        Preserves last_run and next_run from the existing schedule.
        """
        self._callbacks[name] = callback

        # Check if schedule already exists
        existing = None
        for s in self._store.list_all():
            if s.name == name:
                existing = s
                break

        if existing:
            existing.interval_seconds = interval_seconds
            existing.metadata = metadata or {}
            self._store.upsert(existing)
            logger.debug("Updated schedule: %s (every %ds)", name, interval_seconds)
            return existing.id

        schedule = TaskSchedule(
            id=str(uuid.uuid4()),
            name=name,
            interval_seconds=interval_seconds,
            callback_name=name,
            next_run=datetime.now(timezone.utc),
            enabled=True,
            metadata=metadata or {},
        )
        self._store.upsert(schedule)
        logger.info("Registered schedule: %s (every %ds)", name, interval_seconds)
        return schedule.id

    async def tick(self) -> List[dict]:
        """Check and execute all due tasks. Called by the autonomy loop.

        Returns a list of results for tasks that executed.
        """
        due = self._store.get_due()
        if not due:
            return []

        results = []
        for task in due:
            callback = self._callbacks.get(task.callback_name)
            if not callback:
                logger.warning("No callback registered for schedule '%s'", task.callback_name)
                continue

            try:
                if asyncio.iscoroutinefunction(callback):
                    result = await callback()
                else:
                    result = callback()
                results.append({
                    "task": task.name,
                    "status": "ok",
                    "result": result,
                })
                logger.debug("Scheduled task completed: %s", task.name)
            except Exception as e:
                results.append({
                    "task": task.name,
                    "status": "error",
                    "error": str(e),
                })
                logger.warning("Scheduled task failed: %s — %s", task.name, e)

            self._store.update_last_run(task.id)

        return results

    def list_schedules(self) -> List[TaskSchedule]:
        """List all registered schedules."""
        return self._store.list_all()

    def enable(self, schedule_id: str) -> bool:
        """Enable a scheduled task."""
        return self._store.set_enabled(schedule_id, True)

    def disable(self, schedule_id: str) -> bool:
        """Disable a scheduled task."""
        return self._store.set_enabled(schedule_id, False)
