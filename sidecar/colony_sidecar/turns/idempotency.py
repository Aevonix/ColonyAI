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
import os
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
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
    # Preserve digests already stored before the additive checkpoint field.
    if isinstance(payload, dict) and payload.get("checkpoint_messages") is None:
        payload = {key: value for key, value in payload.items() if key != "checkpoint_messages"}
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
        os.chmod(self.db_path, 0o600)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=FULL")
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
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS turn_sources (
                        turn_id TEXT PRIMARY KEY,
                        content_sha256 TEXT NOT NULL,
                        contact_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        scope TEXT NOT NULL CHECK(scope IN ('person', 'session')),
                        messages_json TEXT NOT NULL,
                        occurred_at TEXT,
                        ingested_at TEXT NOT NULL DEFAULT (
                            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        )
                    )
                """)
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS turn_source_search USING fts5(
                        turn_id UNINDEXED, role UNINDEXED, content,
                        tokenize='unicode61'
                    )
                """)
            self._initialized = True

    def record_source(
        self, turn_id: str, *, contact_id: str, session_id: str,
        messages: list[dict[str, Any]], scope: str = "person",
        occurred_at: str | None = None,
    ) -> bool:
        """Atomically retain source JSON and its rebuildable lexical index.

        Checkpoint sources are session-scoped because a full transcript does
        not attest the speaker of every old message. Ordinary attributed turns
        can be recalled by that person across sessions. No inference or affect
        update occurs in this storage path. True means newly committed.
        """
        if not turn_id or not contact_id or not session_id or not messages:
            raise ValueError("source requires an id, participant, session and messages")
        if scope not in {"person", "session"}:
            raise ValueError("invalid source scope")
        if occurred_at is not None:
            if not isinstance(occurred_at, str) or len(occurred_at) > 64:
                raise ValueError("source occurrence time must be an ISO timestamp")
            occurred = datetime.fromisoformat(occurred_at)
            if occurred.tzinfo is None:
                raise ValueError("source occurrence time requires a timezone")
            occurred_at = occurred.astimezone(timezone.utc).isoformat()
        encoded = json.dumps(messages, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 8 * 1024 * 1024:
            raise ValueError("source exceeds 8 MiB")
        digest = canonical_turn_digest({
            "messages": messages, "contact_id": contact_id,
            "session_id": session_id, "scope": scope,
            "occurred_at": occurred_at,
        })
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT content_sha256 FROM turn_sources WHERE turn_id=?", (turn_id,)).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("source id already contains different evidence")
                return False
            conn.execute(
                "INSERT INTO turn_sources "
                "(turn_id, content_sha256, contact_id, session_id, scope, messages_json, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (turn_id, digest, contact_id, session_id, scope, encoded, occurred_at),
            )
            for message in messages:
                content = message.get("content")
                # Media references remain in source JSON. Only explicit text
                # blocks are indexed; URLs, tool metadata and API wrappers are not.
                if isinstance(content, list):
                    content = "\n".join(
                        block.get("text", "") for block in content
                        if isinstance(block, dict)
                        and block.get("type") in {"text", "input_text", "output_text"}
                        and isinstance(block.get("text"), str)
                    )
                if not isinstance(content, str) or not content.strip():
                    continue
                # Overlap avoids losing a phrase at a chunk boundary. The full
                # message remains losslessly stored above, without chunk joins.
                for start in range(0, len(content), 1800):
                    conn.execute(
                        "INSERT INTO turn_source_search(turn_id, role, content) VALUES (?, ?, ?)",
                        (turn_id, message["role"], content[start:start + 2000]),
                    )
        return True

    def search_sources(
        self, query: str, *, contact_id: str, session_id: str, limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Recall direct source excerpts inside the already-authorized viewer."""
        if not contact_id:
            return []
        stop = {"the", "and", "that", "this", "what", "when", "where", "how", "you", "your", "was", "were", "are", "for", "with", "remember", "about"}
        words = list(dict.fromkeys(
            word.lower() for word in re.findall(r"\w+", query[:4096])
            if len(word) > 2 and word.lower() not in stop
        ))[:12]
        if not words:
            return []
        expression = " OR ".join('"' + word + '"' for word in words)
        with closing(self._connect()) as conn:
            rows = conn.execute("""
                SELECT f.turn_id, f.role, f.content, s.session_id, s.scope,
                       s.occurred_at, s.ingested_at
                FROM turn_source_search AS f
                JOIN turn_sources AS s ON s.turn_id=f.turn_id
                WHERE turn_source_search MATCH ? AND s.contact_id=?
                  AND (s.scope='person' OR s.session_id=?)
                ORDER BY bm25(turn_source_search)
                LIMIT ?
            """, (expression, contact_id, session_id, max(1, min(limit, 10)) * 2)).fetchall()
        result, seen = [], set()
        for row in rows:
            # Ordinary-turn and checkpoint copies do not crowd out other hits.
            key = (row["role"], row["content"])
            if key in seen:
                continue
            seen.add(key)
            result.append(dict(row))
            if len(result) >= limit:
                break
        return result

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
