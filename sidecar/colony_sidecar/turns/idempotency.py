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


class SourceErased(ValueError):
    """The requested source or an exact message copy was deliberately erased."""


def source_message_hash(session_id: str, message: Mapping[str, Any]) -> str:
    # Ledger-owned normalization metadata retains the input message identity.
    # Public message schemas cannot supply this top-level field.
    original = message.get("_source_message_hash")
    if isinstance(original, str) and re.fullmatch(r"[0-9a-f]{64}", original):
        return original
    return canonical_turn_digest({"session_id": session_id, "role": message.get("role"), "content": message.get("content")})


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
        conn.execute("PRAGMA secure_delete=ON")
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
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS source_erasures (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        contact_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL,
                        message_hashes_json TEXT NOT NULL,
                        erased_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    )
                """)
                conn.execute("CREATE TABLE IF NOT EXISTS source_projection_erasures (turn_id TEXT NOT NULL, source_turn_id TEXT NOT NULL, PRIMARY KEY(turn_id, source_turn_id))")
                from colony_sidecar.beliefs.source_projection import initialize
                initialize(conn)
                from colony_sidecar.turns.media import initialize as initialize_media
                initialize_media(conn)
                from colony_sidecar.turns.source_vectors import initialize as initialize_vectors
                initialize_vectors(conn)
                from colony_sidecar.self_model.judgments import initialize as initialize_judgments
                initialize_judgments(conn)
            self._initialized = True

    def record_source(
        self, turn_id: str, *, contact_id: str, session_id: str,
        messages: list[dict[str, Any]], scope: str = "person",
        occurred_at: str | None = None,
        timezone_name: str | None = None,
        derive_claims: bool = True,
    ) -> bool:
        """Atomically retain source JSON and its rebuildable lexical index.

        Checkpoint sources are session-scoped because a full transcript does
        not attest the speaker of every old message. Ordinary attributed turns
        can be recalled by that person across sessions. No inference or affect
        update occurs in this storage path. True means newly committed.

        Reviewed historical imports can set derive_claims=False to retain
        quotations without scheduling assertion learning. Text indexing still
        uses the same source ledger and semantic projection queue.
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
            rules = self._erasure_rules(conn, contact_id)
            # Source IDs are globally unique. Reattributing an erased source
            # must not resurrect it under a different contact after restore or
            # a reviewed historical mapping change. Exact message-copy rules
            # below remain scoped to their original contact and session.
            if conn.execute("SELECT 1 FROM source_erasures WHERE turn_id=?", (turn_id,)).fetchone():
                raise SourceErased("source was erased")
            retained = self._retained_messages(messages, session_id, rules)
            if retained != messages:
                for rule in rules:
                    conn.execute("INSERT OR IGNORE INTO source_projection_erasures(turn_id,source_turn_id) VALUES (?,?)", (turn_id, rule["turn_id"]))
            messages = retained
            if not messages:
                raise SourceErased("source contains only erased messages")
            encoded = json.dumps(messages, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
            row = conn.execute("SELECT content_sha256 FROM turn_sources WHERE turn_id=?", (turn_id,)).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("source id already contains different evidence")
                return False
            from colony_sidecar.turns.media import normalize_messages, SourceMedia
            messages = normalize_messages(conn, SourceMedia(self).store, turn_id, session_id, messages)
            encoded = json.dumps(messages, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
            conn.execute(
                "INSERT INTO turn_sources "
                "(turn_id, content_sha256, contact_id, session_id, scope, messages_json, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (turn_id, digest, contact_id, session_id, scope, encoded, occurred_at),
            )
            self._index_messages(conn, turn_id, messages)
            from colony_sidecar.beliefs.source_projection import enqueue
            if derive_claims:
                enqueue(conn, turn_id, messages, scope=scope, timezone_name=timezone_name)
                from colony_sidecar.self_model.judgments import enqueue as enqueue_judgments
                enqueue_judgments(conn, turn_id, contact_id, messages, scope=scope)
            from colony_sidecar.turns.source_vectors import enqueue as enqueue_vectors
            enqueue_vectors(conn, turn_id)
        return True

    @staticmethod
    def _index_messages(conn: sqlite3.Connection, turn_id: str, messages: list[dict[str, Any]]) -> None:
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


    @staticmethod
    def _erasure_rules(conn: sqlite3.Connection, contact_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM source_erasures WHERE contact_id=?", (contact_id,)
        )]

    @staticmethod
    def _retained_messages(messages: list[dict[str, Any]], session_id: str, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hashes = {h for rule in rules if rule["session_id"] == session_id
                  for h in json.loads(rule["message_hashes_json"])}
        return [message for message in messages if source_message_hash(session_id, message) not in hashes]

    def is_source_erased(self, turn_id: str, contact_id: str | None = None) -> bool:
        with closing(self._connect()) as conn:
            sql, args = "SELECT 1 FROM source_erasures WHERE turn_id=?", [turn_id]
            if contact_id is not None:
                sql += " AND contact_id=?"
                args.append(contact_id)
            return conn.execute(sql, args).fetchone() is not None

    def is_projection_erased(self, turn_id: str) -> bool:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT 1 FROM source_projection_erasures WHERE turn_id=?", (turn_id,)).fetchone() is not None

    def retained_messages(self, *, contact_id: str, session_id: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            return self._retained_messages(messages, session_id, self._erasure_rules(conn, contact_id))

    def erasure_watermark(self, contact_id: str) -> int:
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT coalesce(max(sequence), 0) FROM source_erasures WHERE contact_id=?", (contact_id,)).fetchone()[0])

    def erasure_feed(self, contact_id: str, after: int = 0, limit: int = 250) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            head = conn.execute("SELECT coalesce(max(sequence), 0) FROM source_erasures WHERE contact_id=?", (contact_id,)).fetchone()[0]
            if after > head:
                raise ValueError("erasure cursor exceeds server history; restore requires reconciliation")
            rows = conn.execute("SELECT * FROM source_erasures WHERE contact_id=? AND sequence>? ORDER BY sequence LIMIT ?", (contact_id, after, max(1, min(limit, 500)))).fetchall()
            events = [{"sequence": row["sequence"], "turn_id": row["turn_id"], "session_id": row["session_id"], "message_hashes": json.loads(row["message_hashes_json"])} for row in rows]
            through = events[-1]["sequence"] if events else after
            return {"contact_id": contact_id, "head": head, "through": through, "events": events, "complete": through == head}

    def erase_sources(self, *, contact_id: str, turn_ids: list[str] | None = None, old_text: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        """Selective source erasure, including exact checkpoint message copies.

        Tombstones retain IDs and hashes only. Unknown IDs are rejected rather
        than allowing a contact to reserve or erase another contact's future ID.
        Exact-text compatibility is deliberately unambiguous and session-bound.
        """
        if not contact_id or bool(turn_ids) == bool(old_text):
            raise ValueError("select source IDs or exact old_text")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT * FROM turn_sources WHERE contact_id=?", (contact_id,)).fetchall()
            by_id = {row["turn_id"]: row for row in rows}
            if old_text:
                if not session_id:
                    raise ValueError("exact text erasure requires a session")
                matches = [row["turn_id"] for row in rows if row["session_id"] == session_id
                           and any(message.get("content") == old_text for message in json.loads(row["messages_json"]))]
                # A checkpoint plus its ordinary source are two matches. Callers
                # must select explicit provenance rather than guessing ownership.
                if len(matches) != 1:
                    raise ValueError("ambiguous_source" if matches else "source_not_found")
                turn_ids = matches
            selected = list(dict.fromkeys(turn_ids or []))
            if not 1 <= len(selected) <= 100 or any(not item or len(item) > 256 for item in selected):
                raise ValueError("select 1..100 source IDs")
            existing_rules = self._erasure_rules(conn, contact_id)
            known_erased = {rule["turn_id"] for rule in existing_rules}
            if any(item not in by_id and item not in known_erased for item in selected):
                raise ValueError("source_not_found")
            for turn_id in selected:
                row = by_id.get(turn_id)
                if row is None:
                    continue
                hashes = [source_message_hash(row["session_id"], message) for message in json.loads(row["messages_json"])]
                conn.execute("INSERT OR IGNORE INTO source_erasures(contact_id, turn_id, session_id, message_hashes_json) VALUES (?, ?, ?, ?)", (contact_id, turn_id, row["session_id"], json.dumps(hashes)))
            rules = self._erasure_rules(conn, contact_id)
            erased_ids = {rule["turn_id"] for rule in rules}
            affected = []
            for row in rows:
                messages = json.loads(row["messages_json"])
                retained = [] if row["turn_id"] in erased_ids else self._retained_messages(messages, row["session_id"], rules)
                if retained == messages:
                    continue
                from colony_sidecar.beliefs.source_projection import erase_removed
                erase_removed(conn, row["turn_id"], row["session_id"], retained)
                from colony_sidecar.turns.media import erase_removed as erase_media
                erase_media(conn, row["turn_id"], row["session_id"], retained)
                from colony_sidecar.self_model.perspective import erase_removed as erase_preferences
                erase_preferences(conn, row["turn_id"], row["session_id"], retained)
                from colony_sidecar.self_model.judgments import erase_removed as erase_judgments
                erase_judgments(conn, row["turn_id"], row["session_id"], retained)
                affected.append(row["turn_id"])
                for selected_id in selected:
                    conn.execute("INSERT OR IGNORE INTO source_projection_erasures(turn_id,source_turn_id) VALUES (?,?)", (row["turn_id"], selected_id))
                conn.execute("DELETE FROM turn_source_search WHERE turn_id=?", (row["turn_id"],))
                if retained:
                    conn.execute("UPDATE turn_sources SET messages_json=? WHERE turn_id=?", (json.dumps(retained, ensure_ascii=True, sort_keys=True, separators=(",", ":")), row["turn_id"]))
                    self._index_messages(conn, row["turn_id"], retained)
                    from colony_sidecar.turns.source_vectors import enqueue as enqueue_vectors
                    enqueue_vectors(conn, row['turn_id'])
                else:
                    conn.execute("DELETE FROM turn_sources WHERE turn_id=?", (row["turn_id"],))
                    conn.execute('DELETE FROM source_vector_jobs WHERE turn_id=?', (row['turn_id'],))
                # An ordinary response can carry derived excerpts. Preserve its
                # digest/state fence, but remove cached content after erasure.
                conn.execute("UPDATE turn_ingestion SET response_json=NULL, error=NULL WHERE turn_id=?", (row["turn_id"],))
            # Preserve cleanup targets so a retry after a graph outage also
            # removes copies that disappeared from the source table already.
            for selected_id in selected:
                affected.extend(row[0] for row in conn.execute("SELECT turn_id FROM source_projection_erasures WHERE source_turn_id=?", (selected_id,)))
            watermark = conn.execute("SELECT coalesce(max(sequence),0) FROM source_erasures WHERE contact_id=?", (contact_id,)).fetchone()[0]
        from colony_sidecar.turns.media import SourceMedia
        media = SourceMedia(self)
        try:
            media.collect_orphans(limit=100)
            media_cleanup = media.cleanup_status()
        except OSError:
            media_cleanup = "pending"
        return {"source_ids": selected, "affected_source_ids": list(dict.fromkeys(affected)),
                "watermark": watermark, "media_cleanup": media_cleanup}

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
