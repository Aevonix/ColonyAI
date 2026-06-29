"""Channel registration store -- backed by colony-channels.db.

Tracks which channels are registered with this Colony instance, their
capabilities, and authentication tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from colony_sidecar.channels.manifest import ChannelManifest
from colony_sidecar.migrations import run_migrations_sync

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


@dataclass
class RegisteredChannel:
    channel_key: str
    display_name: str
    gateway_family: str
    manifest: ChannelManifest
    channel_token: str
    registered_at: str
    last_seen_at: Optional[str]
    status: str


class ChannelStore:
    """SQLite-backed channel registration store."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        run_migrations_sync(self._conn, _MIGRATIONS_DIR)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if not self._conn:
            raise RuntimeError("ChannelStore not connected")
        return self._conn

    # ── Registration ─────────────────────────────────────────────────────

    def register(
        self,
        manifest: ChannelManifest,
        *,
        channel_token: Optional[str] = None,
    ) -> RegisteredChannel:
        """Register a new channel or update an existing one.

        If the channel_key already exists and the provided token matches,
        the registration is updated (upsert).  If no token is provided for
        a new registration, one is generated.

        Raises ValueError on token mismatch (409 at the API layer).
        """
        conn = self._require_conn()
        now = datetime.now(timezone.utc).isoformat()

        existing = self.get(manifest.channel_key)

        if existing is not None:
            if not channel_token or not self.verify_token(
                manifest.channel_key, channel_token
            ):
                raise ValueError(
                    f"Channel '{manifest.channel_key}' already registered; "
                    "provide the correct channel_token to update"
                )
            conn.execute(
                """UPDATE channels
                   SET display_name = ?, gateway_family = ?,
                       manifest_json = ?, last_seen_at = ?
                 WHERE channel_key = ?""",
                (
                    manifest.display_name,
                    manifest.gateway_family,
                    manifest.model_dump_json(),
                    now,
                    manifest.channel_key,
                ),
            )
            conn.commit()
            return self.get(manifest.channel_key)  # type: ignore[return-value]

        token = channel_token or _generate_token()
        token_hash = _hash_token(token)

        conn.execute(
            """INSERT INTO channels
               (channel_key, display_name, gateway_family,
                manifest_json, channel_token, registered_at, last_seen_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
            (
                manifest.channel_key,
                manifest.display_name,
                manifest.gateway_family,
                manifest.model_dump_json(),
                token_hash,
                now,
                now,
            ),
        )
        conn.commit()

        registered = self.get(manifest.channel_key)
        assert registered is not None
        registered.channel_token = token
        return registered

    # ── Queries ──────────────────────────────────────────────────────────

    def get(self, channel_key: str) -> Optional[RegisteredChannel]:
        conn = self._require_conn()
        cur = conn.execute(
            "SELECT * FROM channels WHERE channel_key = ?", (channel_key,)
        )
        row = cur.fetchone()
        return _row_to_channel(row) if row else None

    def list_active(self) -> list[RegisteredChannel]:
        conn = self._require_conn()
        cur = conn.execute(
            "SELECT * FROM channels WHERE status = 'active' ORDER BY channel_key"
        )
        return [_row_to_channel(row) for row in cur.fetchall()]

    def list_all(self) -> list[RegisteredChannel]:
        conn = self._require_conn()
        cur = conn.execute("SELECT * FROM channels ORDER BY channel_key")
        return [_row_to_channel(row) for row in cur.fetchall()]

    def get_phone_gateways(self) -> set[str]:
        """Return channel_keys with phone_identity_unification enabled."""
        return {
            ch.channel_key
            for ch in self.list_active()
            if ch.manifest.phone_identity_unification
        }

    # ── Mutations ────────────────────────────────────────────────────────

    def touch(self, channel_key: str) -> None:
        """Update last_seen_at for the given channel."""
        conn = self._require_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE channels SET last_seen_at = ? WHERE channel_key = ?",
            (now, channel_key),
        )
        conn.commit()

    def revoke(self, channel_key: str) -> bool:
        conn = self._require_conn()
        cur = conn.execute(
            "UPDATE channels SET status = 'revoked' WHERE channel_key = ?",
            (channel_key,),
        )
        conn.commit()
        return cur.rowcount > 0

    def delete(self, channel_key: str) -> bool:
        conn = self._require_conn()
        cur = conn.execute(
            "DELETE FROM channels WHERE channel_key = ?", (channel_key,)
        )
        conn.commit()
        return cur.rowcount > 0

    # ── Auth ─────────────────────────────────────────────────────────────

    def verify_token(self, channel_key: str, token: str) -> bool:
        conn = self._require_conn()
        cur = conn.execute(
            "SELECT channel_token FROM channels WHERE channel_key = ?",
            (channel_key,),
        )
        row = cur.fetchone()
        if not row:
            return False
        return hmac.compare_digest(row["channel_token"], _hash_token(token))


# ── Helpers ──────────────────────────────────────────────────────────────


def _generate_token() -> str:
    return "ch_" + secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _row_to_channel(row: sqlite3.Row) -> RegisteredChannel:
    manifest = ChannelManifest.model_validate_json(row["manifest_json"])
    return RegisteredChannel(
        channel_key=row["channel_key"],
        display_name=row["display_name"],
        gateway_family=row["gateway_family"],
        manifest=manifest,
        channel_token=row["channel_token"],
        registered_at=row["registered_at"],
        last_seen_at=row["last_seen_at"],
        status=row["status"],
    )
