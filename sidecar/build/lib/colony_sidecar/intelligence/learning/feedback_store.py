"""FeedbackStore — persist and retrieve user corrections for learning.

Corrections feed into ContinuousLearner and are periodically summarized
by MetaLearner into durable preference updates.

Storage: SQLite (default ~/.colony/feedback.db) so corrections survive
process restarts without requiring Neo4j to be reachable.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = os.path.join(
    os.path.expanduser("~"), ".colony", "feedback.db"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS corrections (
    correction_id  TEXT PRIMARY KEY,
    timestamp      TEXT NOT NULL,
    original_response TEXT NOT NULL,
    correction_text   TEXT NOT NULL,
    correction_type   TEXT NOT NULL,
    context_hash      TEXT NOT NULL,
    applied           INTEGER NOT NULL DEFAULT 0,
    person_id         TEXT NOT NULL DEFAULT '',
    processed_at      TEXT
);
"""

# Migrations for existing databases that pre-date new columns.
_MIGRATIONS = [
    "ALTER TABLE corrections ADD COLUMN person_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE corrections ADD COLUMN processed_at TEXT",
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class UserCorrection:
    """A single user correction event."""

    correction_id: str
    timestamp: datetime
    original_response: str
    correction_text: str
    correction_type: str   # "factual" | "tone" | "action" | "preference"
    context_hash: str
    applied: bool = False
    person_id: str = ""
    processed_at: Optional[datetime] = None

    @classmethod
    def create(
        cls,
        original_response: str,
        correction_text: str,
        correction_type: str,
        context_hash: str,
        person_id: str = "",
    ) -> "UserCorrection":
        """Factory that generates a UUID and timestamps the correction."""
        return cls(
            correction_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            original_response=original_response,
            correction_text=correction_text,
            correction_type=correction_type,
            context_hash=context_hash,
            person_id=person_id,
        )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class FeedbackStore:
    """Persist and retrieve user corrections for learning.

    Thread-safe via SQLite's WAL mode. All public methods are synchronous
    so they can be called from both sync and async contexts without a
    dedicated event loop.

    Args:
        db_path: Path to the SQLite database file. Defaults to
            ``~/.colony/feedback.db``. Pass ``:memory:`` for tests.
    """

    def __init__(self, db_path: str = _DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        # For :memory: DBs we keep a single persistent connection so schema survives
        self._mem_conn: sqlite3.Connection | None = None
        if db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.execute("PRAGMA journal_mode=WAL")
            self._mem_conn.executescript(_SCHEMA)
            self._mem_conn.commit()
        else:
            self._ensure_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_correction(self, correction: UserCorrection) -> None:
        """Persist *correction* to the store (idempotent by correction_id)."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO corrections "
                "(correction_id, timestamp, original_response, correction_text, "
                " correction_type, context_hash, applied, person_id, processed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    correction.correction_id,
                    correction.timestamp.isoformat(),
                    correction.original_response,
                    correction.correction_text,
                    correction.correction_type,
                    correction.context_hash,
                    int(correction.applied),
                    correction.person_id,
                    correction.processed_at.isoformat() if correction.processed_at else None,
                ),
            )
        logger.debug("Recorded correction %s (%s)", correction.correction_id, correction.correction_type)

    def get_unapplied(self, limit: int = 50) -> List[UserCorrection]:
        """Return up to *limit* corrections that have not yet been applied."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM corrections WHERE applied = 0 "
                "ORDER BY timestamp ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_correction(r) for r in rows]

    def mark_processed(self, correction_ids: List[str]) -> None:
        """Stamp *processed_at* on each correction so consume_unprocessed skips it."""
        if not correction_ids:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" * len(correction_ids))
        with self._conn() as conn:
            conn.execute(
                f"UPDATE corrections SET processed_at = ? "
                f"WHERE correction_id IN ({placeholders})",
                [now_iso] + list(correction_ids),
            )
        logger.debug("Marked %d corrections as processed", len(correction_ids))

    def consume_unprocessed(self) -> List[UserCorrection]:
        """Fetch all unprocessed corrections, mark them processed, and return them."""
        with self._conn() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(corrections)").fetchall()}
            if "processed_at" not in cols:
                conn.execute("ALTER TABLE corrections ADD COLUMN processed_at TEXT")

            rows = conn.execute(
                "SELECT * FROM corrections WHERE processed_at IS NULL ORDER BY timestamp ASC"
            ).fetchall()

        if not rows:
            return []

        corrections = [self._row_to_correction(r) for r in rows]
        self.mark_processed([c.correction_id for c in corrections])
        logger.info("consume_unprocessed: returned %d corrections", len(corrections))
        return corrections

    def mark_applied(self, correction_ids: List[str]) -> None:
        """Mark the given corrections as applied (won't be returned by get_unapplied)."""
        if not correction_ids:
            return
        placeholders = ",".join("?" * len(correction_ids))
        with self._conn() as conn:
            conn.execute(
                f"UPDATE corrections SET applied = 1 WHERE correction_id IN ({placeholders})",
                correction_ids,
            )
        logger.debug("Marked %d corrections as applied", len(correction_ids))

    def get_correction_summary(self, days: int = 7) -> Dict[str, object]:
        """Return summary statistics for corrections in the last *days* days."""
        cutoff = (
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
        )
        # Subtract days manually (no timedelta import needed above)
        import datetime as _dt
        cutoff = cutoff - _dt.timedelta(days=days)
        cutoff_iso = cutoff.isoformat()

        with self._conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM corrections WHERE timestamp >= ?", (cutoff_iso,)
            ).fetchone()[0]
            applied = conn.execute(
                "SELECT COUNT(*) FROM corrections WHERE timestamp >= ? AND applied = 1",
                (cutoff_iso,),
            ).fetchone()[0]
            by_type_rows = conn.execute(
                "SELECT correction_type, COUNT(*) FROM corrections "
                "WHERE timestamp >= ? GROUP BY correction_type",
                (cutoff_iso,),
            ).fetchall()

        by_type: Dict[str, int] = {row[0]: row[1] for row in by_type_rows}
        return {
            "days": days,
            "total": total,
            "applied": applied,
            "unapplied": total - applied,
            "by_type": by_type,
        }

    def count(self) -> int:
        """Return total number of stored corrections."""
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0]

    def clear(self) -> None:
        """Delete all records (useful for tests / onboarding reset)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM corrections")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_db(self) -> None:
        """Create the database file and schema if they don't exist, then migrate."""
        if self._db_path != ":memory:":
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            # Best-effort migrations for pre-existing databases.
            for stmt in _MIGRATIONS:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # Column already exists — that's fine.

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        if self._mem_conn is not None:
            # In-memory: reuse the persistent connection; caller manages commit
            try:
                yield self._mem_conn
                self._mem_conn.commit()
            except Exception:
                self._mem_conn.rollback()
                raise
            return
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _row_to_correction(row: sqlite3.Row) -> UserCorrection:
        ts_raw = row["timestamp"]
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            ts = datetime.now(timezone.utc)
        return UserCorrection(
            correction_id=row["correction_id"],
            timestamp=ts,
            original_response=row["original_response"],
            correction_text=row["correction_text"],
            correction_type=row["correction_type"],
            context_hash=row["context_hash"],
            applied=bool(row["applied"]),
        )
