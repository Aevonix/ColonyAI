"""Durable server-side idempotency for host turn ingestion.

The ledger is intentionally small and independent of graph/vector stores. A
turn ID is reserved durably before any downstream effect runs. Identical
replays return the stored response; different canonical content for the same
ID is a conflict. An interrupted first attempt is marked ``ambiguous`` and is
never automatically replayed because some downstream effects may already have
occurred.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional


class ReservationOutcome(str, Enum):
    CREATED = "created"
    REPLAYED = "replayed"
    CONFLICT = "conflict"
    IN_PROGRESS = "in_progress"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Reservation:
    outcome: ReservationOutcome
    response: Optional[dict[str, Any]] = None


def canonical_turn_digest(payload: Any) -> str:
    """Hash the normalized turn envelope, preserving list order and values."""
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json", exclude_none=False)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TurnIdempotencyLedger:
    """SQLite reservation ledger safe across threads and sidecar processes."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._initialized = False
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with closing(self._connect()) as conn, conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS turn_ingestion (
                        turn_id TEXT PRIMARY KEY,
                        content_sha256 TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN ('processing', 'completed', 'ambiguous')
                        ),
                        response_json TEXT,
                        created_at TEXT NOT NULL DEFAULT (
                            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        ),
                        completed_at TEXT,
                        error TEXT
                    )
                    """
                )
            self._initialized = True

    def reserve(self, turn_id: str, content_sha256: str) -> Reservation:
        turn_id = (turn_id or "").strip()
        if not turn_id or len(turn_id) > 256:
            raise ValueError("turn_id must contain 1..256 non-whitespace characters")
        if len(content_sha256) != 64:
            raise ValueError("content_sha256 must be a SHA-256 hex digest")

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT content_sha256, state, response_json "
                "FROM turn_ingestion WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO turn_ingestion "
                    "(turn_id, content_sha256, state) VALUES (?, ?, 'processing')",
                    (turn_id, content_sha256),
                )
                conn.commit()
                return Reservation(ReservationOutcome.CREATED)

            conn.commit()
            if row["content_sha256"] != content_sha256:
                return Reservation(ReservationOutcome.CONFLICT)
            if row["state"] == "completed":
                response = None
                if row["response_json"]:
                    response = json.loads(row["response_json"])
                return Reservation(ReservationOutcome.REPLAYED, response=response)
            if row["state"] == "ambiguous":
                return Reservation(ReservationOutcome.AMBIGUOUS)
            return Reservation(ReservationOutcome.IN_PROGRESS)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete(
        self,
        turn_id: str,
        content_sha256: str,
        response: Mapping[str, Any],
    ) -> None:
        response_json = json.dumps(
            dict(response), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        with closing(self._connect()) as conn, conn:
            changed = conn.execute(
                """
                UPDATE turn_ingestion
                SET state='completed', response_json=?,
                    completed_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    error=NULL
                WHERE turn_id=? AND content_sha256=? AND state='processing'
                """,
                (response_json, turn_id, content_sha256),
            ).rowcount
            if changed != 1:
                row = conn.execute(
                    "SELECT content_sha256, state FROM turn_ingestion WHERE turn_id=?",
                    (turn_id,),
                ).fetchone()
                if not row or row["content_sha256"] != content_sha256:
                    raise RuntimeError("turn reservation changed before completion")
                if row["state"] != "completed":
                    raise RuntimeError(f"turn cannot complete from state {row['state']}")

    def mark_ambiguous(
        self,
        turn_id: str,
        content_sha256: str,
        error: BaseException,
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE turn_ingestion
                SET state='ambiguous', error=?
                WHERE turn_id=? AND content_sha256=? AND state='processing'
                """,
                (f"{type(error).__name__}: {error}"[:1000], turn_id, content_sha256),
            )

    def get(self, turn_id: str) -> Optional[dict[str, Any]]:
        """Read-only reconciliation view used by tests and future tooling."""
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT * FROM turn_ingestion WHERE turn_id=?", (turn_id,)
            ).fetchone()
        return dict(row) if row else None


@lru_cache(maxsize=16)
def _cached_ledger(db_path: str) -> TurnIdempotencyLedger:
    return TurnIdempotencyLedger(db_path)


def get_turn_idempotency_ledger(state_dir: str | Path) -> TurnIdempotencyLedger:
    path = Path(state_dir).resolve() / "turn-idempotency.db"
    return _cached_ledger(str(path))
