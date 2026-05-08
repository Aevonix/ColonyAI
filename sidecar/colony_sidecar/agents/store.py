"""Agent and Invite stores for multi-agent Colony.

Provides:
- AgentStore: Registry of connected agents with SQLite persistence
- InviteStore: Setup code management with rate limiting
- Audit logging
- Certificate Revocation List (CRL)
"""

import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import Agent, AgentMetadata, AgentStatus

logger = logging.getLogger(__name__)

# Type alias for Colony key manager (avoid circular import)
LocalKeyManager = Any


def get_state_dir() -> Path:
    """Get Colony state directory."""
    state_dir = os.environ.get("COLONY_STATE_DIR")
    if state_dir:
        return Path(state_dir)
    return Path.home() / ".colony" / "data"


def generate_setup_code() -> str:
    """Generate a random setup code: COLONY-XXXX-XXXX-XXXX."""
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # No 0/O, 1/I confusion
    segments = ["COLONY"]
    for _ in range(3):
        segment = "".join(secrets.choice(chars) for _ in range(4))
        segments.append(segment)
    return "-".join(segments)


def hash_setup_code(code: str) -> str:
    """Hash setup code for secure storage."""
    pepper = os.environ.get(
        "COLONY_CODE_PEPPER",
        "default-pepper-change-in-production",
    )
    return hashlib.sha256(f"{code}:{pepper}".encode()).hexdigest()


class AgentStore:
    """Manages agent registry with SQLite persistence and CRL support."""

    def __init__(
        self,
        state_dir: Optional[Path] = None,
        colony_key_manager: Optional[LocalKeyManager] = None,
    ):
        self._state_dir = Path(state_dir) if state_dir else get_state_dir()
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._state_dir / "agents.db"
        self._backup_path = self._state_dir / "agents.db.backup"
        self._colony_km = colony_key_manager

        # In-memory CRL for fast lookup
        self._revoked_node_ids: set = set()

        self._db = self._init_db()
        self._load_crl()

    def _init_db(self) -> sqlite3.Connection:
        """Initialize database with recovery."""
        try:
            return self._connect()
        except sqlite3.DatabaseError:
            logger.warning("agents.db corrupted, attempting recovery")

            if self._backup_path.exists():
                shutil.copy(self._backup_path, self._db_path)
                logger.info("Restored agents.db from backup")
            else:
                self._db_path.unlink(missing_ok=True)
                logger.warning("No backup available, starting fresh")

            return self._connect()

    def _connect(self) -> sqlite3.Connection:
        """Connect to database with WAL mode for reliability."""
        # check_same_thread=False allows TestClient to access the DB from
        # a different thread (test thread vs event loop thread). This is
        # safe for tests; production uses a single process/thread.
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        # WAL mode for better crash recovery
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")

        self._create_tables(conn)
        self._create_audit_tables(conn)

        return conn

    def _create_tables(self, conn: sqlite3.Connection) -> None:
        """Create agents table."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY,
                node_id TEXT NOT NULL,
                colony_id TEXT NOT NULL,
                name TEXT NOT NULL,
                
                connection_mode TEXT DEFAULT 'local',
                gateway_url TEXT,
                websocket_connected INTEGER DEFAULT 0,
                
                capabilities TEXT DEFAULT '[]',
                is_primary INTEGER DEFAULT 0,
                priority INTEGER DEFAULT 1,
                max_concurrent INTEGER DEFAULT 5,
                max_initiatives_per_hour INTEGER DEFAULT 10,
                excluded_types TEXT DEFAULT '[]',
                included_types TEXT DEFAULT '[]',
                
                status TEXT DEFAULT 'offline',
                current_assignments INTEGER DEFAULT 0,
                last_seen_at TIMESTAMP,
                
                metadata TEXT DEFAULT '{}',
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                node_cert TEXT,
                
                UNIQUE(node_id, colony_id)
            )
            """
        )

        # Indexes
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_primary ON agents(is_primary)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_colony ON agents(colony_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_last_seen ON agents(last_seen_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_node_id ON agents(node_id)")

        conn.commit()

    def _create_audit_tables(self, conn: sqlite3.Connection) -> None:
        """Create audit log table."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                action TEXT NOT NULL,
                actor TEXT,
                target TEXT,
                details TEXT,
                ip_address TEXT,
                user_agent TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor)"
        )
        conn.commit()

    def _load_crl(self) -> None:
        """Load CRL from database into memory."""
        cursor = self._db.execute(
            "SELECT node_id FROM agents WHERE status = ?",
            [AgentStatus.REVOKED.value],
        )
        self._revoked_node_ids = {row["node_id"] for row in cursor.fetchall()}
        logger.info("Loaded CRL: %d revoked node_ids", len(self._revoked_node_ids))

    # ------------------------------------------------------------------
    # Agent CRUD
    # ------------------------------------------------------------------

    def create(self, data: Dict[str, Any]) -> Agent:
        """Create a new agent."""
        agent_id = data.get("agent_id") or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        cursor = self._db.execute(
            """
            INSERT INTO agents (
                agent_id, node_id, colony_id, name,
                connection_mode, gateway_url,
                capabilities, is_primary, priority, max_concurrent, max_initiatives_per_hour,
                excluded_types, included_types,
                status, current_assignments, last_seen_at,
                metadata, registered_at, node_cert
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                agent_id,
                data["node_id"],
                data["colony_id"],
                data["name"],
                data.get("connection_mode", "local"),
                data.get("gateway_url"),
                json.dumps(data.get("capabilities", [])),
                1 if data.get("is_primary") else 0,
                data.get("priority", 1),
                data.get("max_concurrent", 5),
                data.get("max_initiatives_per_hour", 10),
                json.dumps(data.get("excluded_types", [])),
                json.dumps(data.get("included_types", [])),
                data.get("status", "offline"),
                data.get("current_assignments", 0),
                data.get("last_seen_at"),
                json.dumps(data.get("metadata", {})),
                now,
                json.dumps(data["node_cert"]) if data.get("node_cert") else None,
            ],
        )
        self._db.commit()

        # Log audit
        self.log_audit(
            action="agent_create",
            actor="system",
            target=agent_id,
            details={"name": data["name"], "node_id": data["node_id"]},
        )

        return self.get(agent_id)

    def get(self, agent_id: str) -> Optional[Agent]:
        """Get agent by ID."""
        cursor = self._db.execute(
            "SELECT * FROM agents WHERE agent_id = ?",
            [agent_id],
        )
        row = cursor.fetchone()
        if row:
            return Agent.from_row(dict(row))
        return None

    def get_by_node_id(self, node_id: str) -> Optional[Agent]:
        """Get agent by node ID."""
        cursor = self._db.execute(
            "SELECT * FROM agents WHERE node_id = ?",
            [node_id],
        )
        row = cursor.fetchone()
        if row:
            return Agent.from_row(dict(row))
        return None

    def list(
        self,
        status: Optional[List[str]] = None,
        colony_id: Optional[str] = None,
        is_primary: Optional[bool] = None,
        capability: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Agent]:
        """List agents with filters."""
        query = "SELECT * FROM agents WHERE 1=1"
        params: List[Any] = []

        if status:
            placeholders = ",".join("?" * len(status))
            query += f" AND status IN ({placeholders})"
            params.extend(status)

        if colony_id:
            query += " AND colony_id = ?"
            params.append(colony_id)

        if is_primary is not None:
            query += " AND is_primary = ?"
            params.append(1 if is_primary else 0)

        query += " ORDER BY priority DESC, name ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = self._db.execute(query, params)
        agents = [Agent.from_row(dict(row)) for row in cursor.fetchall()]

        if capability:
            agents = [a for a in agents if capability in a.capabilities]

        return agents

    def update(self, agent_id: str, **updates) -> Optional[Agent]:
        """Update agent fields."""
        if not updates:
            return self.get(agent_id)

        # Build SET clause
        set_parts = []
        params = []

        for key, value in updates.items():
            if key in (
                "capabilities",
                "excluded_types",
                "included_types",
                "metadata",
                "node_cert",
            ):
                set_parts.append(f"{key} = ?")
                params.append(json.dumps(value) if not isinstance(value, str) else value)
            elif key in ("is_primary", "websocket_connected"):
                set_parts.append(f"{key} = ?")
                params.append(1 if value else 0)
            elif key in ("last_seen_at",) and isinstance(value, datetime):
                set_parts.append(f"{key} = ?")
                params.append(value.isoformat())
            else:
                set_parts.append(f"{key} = ?")
                params.append(value)

        if not set_parts:
            return self.get(agent_id)

        params.append(agent_id)
        query = f"UPDATE agents SET {', '.join(set_parts)} WHERE agent_id = ?"

        self._db.execute(query, params)
        self._db.commit()

        return self.get(agent_id)

    def delete(self, agent_id: str) -> bool:
        """Delete an agent."""
        cursor = self._db.execute(
            "DELETE FROM agents WHERE agent_id = ?",
            [agent_id],
        )
        self._db.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Status Management
    # ------------------------------------------------------------------

    def set_online(
        self,
        agent_id: str,
        websocket_connected: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Agent]:
        """Mark agent as online."""
        updates = {
            "status": AgentStatus.ONLINE.value,
            "last_seen_at": datetime.now(timezone.utc),
            "websocket_connected": websocket_connected,
        }
        if metadata:
            updates["metadata"] = metadata

        return self.update(agent_id, **updates)

    def set_offline(self, agent_id: str) -> Optional[Agent]:
        """Mark agent as offline."""
        return self.update(
            agent_id,
            status=AgentStatus.OFFLINE.value,
            websocket_connected=False,
        )

    def mark_all_offline(self) -> int:
        """Mark all agents as offline (called on Colony restart)."""
        cursor = self._db.execute(
            "UPDATE agents SET status = ?, websocket_connected = 0",
            [AgentStatus.OFFLINE.value],
        )
        self._db.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------

    def revoke(self, agent_id: str, reason: str = "") -> Optional[Agent]:
        """Revoke an agent."""
        agent = self.get(agent_id)
        if not agent:
            return None

        # Update status
        self.update(agent_id, status=AgentStatus.REVOKED.value)

        # Add to CRL
        self._revoked_node_ids.add(agent.node_id)

        # Log audit
        self.log_audit(
            action="agent_revoke",
            actor="api",
            target=agent_id,
            details={"reason": reason, "node_id": agent.node_id},
        )

        return self.get(agent_id)

    def is_revoked(self, node_id: str) -> bool:
        """Check if node_id is revoked."""
        return node_id in self._revoked_node_ids

    # ------------------------------------------------------------------
    # Assignment Tracking
    # ------------------------------------------------------------------

    def increment_assignments(self, agent_id: str) -> None:
        """Increment current_assignments counter."""
        self._db.execute(
            "UPDATE agents SET current_assignments = current_assignments + 1 WHERE agent_id = ?",
            [agent_id],
        )
        self._db.commit()

    def decrement_assignments(self, agent_id: str) -> None:
        """Decrement current_assignments counter."""
        self._db.execute(
            "UPDATE agents SET current_assignments = MAX(0, current_assignments - 1) WHERE agent_id = ?",
            [agent_id],
        )
        self._db.commit()

    # ------------------------------------------------------------------
    # Ghost Cleanup
    # ------------------------------------------------------------------

    def list_ghosts(self, registered_before: datetime) -> List[Agent]:
        """List agents that registered but never connected.

        Ghost agents are:
        - status='offline'
        - websocket_connected=0
        - last_seen_at IS NULL (never connected)
        - registered_at < threshold
        """
        cursor = self._db.execute(
            """
            SELECT * FROM agents
            WHERE status = ?
            AND websocket_connected = 0
            AND last_seen_at IS NULL
            AND registered_at < ?
            """,
            [AgentStatus.OFFLINE.value, registered_before.isoformat()],
        )
        return [Agent.from_row(dict(row)) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Audit Logging
    # ------------------------------------------------------------------

    def log_audit(
        self,
        action: str,
        actor: str,
        target: str,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        """Log audit event."""
        self._db.execute(
            """
            INSERT INTO audit_log (action, actor, target, details, ip_address, user_agent)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                action,
                actor,
                target,
                json.dumps(details) if details else None,
                ip_address,
                user_agent,
            ],
        )
        self._db.commit()

    def get_audit_logs(
        self,
        action: Optional[str] = None,
        actor: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get audit logs with filters."""
        query = "SELECT * FROM audit_log WHERE 1=1"
        params: List[Any] = []

        if action:
            query += " AND action = ?"
            params.append(action)

        if actor:
            query += " AND actor = ?"
            params.append(actor)

        if since:
            query += " AND timestamp >= ?"
            params.append(since.isoformat())

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor = self._db.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Certificate Signing
    # ------------------------------------------------------------------

    async def sign_node_certificate(
        self,
        node_id: str,
        node_public_key: str,
        expires_days: int = 365,
    ) -> Dict[str, Any]:
        """Sign a node certificate for remote agent."""
        if not self._colony_km:
            raise ValueError("Colony key not available for signing")

        # Import here to avoid circular dependency
        from colony_sidecar.chain.identity import get_or_create_colony_id

        colony_id = get_or_create_colony_id(self._state_dir)

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=expires_days)

        cert = {
            "colony_id": colony_id,
            "node_id": node_id,
            "node_public_key_ed25519": node_public_key,
            "issued_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

        # Sign with Colony private key
        # The LocalKeyManager should have a sign() method
        payload = json.dumps(cert, sort_keys=True).encode()
        signature = self._colony_km.sign(payload)
        cert["signature"] = signature.hex()

        return cert

    # ------------------------------------------------------------------
    # Backup/Recovery
    # ------------------------------------------------------------------

    def backup(self) -> None:
        """Create backup of database."""
        shutil.copy2(self._db_path, self._backup_path)

    def close(self) -> None:
        """Close connection and create backup."""
        self.backup()
        self._db.close()


class InviteStore:
    """Manages agent invitation/setup codes."""

    MAX_FAILED_ATTEMPTS = 5
    LOCKOUT_MINUTES = 15
    DEFAULT_EXPIRY_SECONDS = 900  # 15 minutes

    def __init__(self, state_dir: Optional[Path] = None):
        self._state_dir = Path(state_dir) if state_dir else get_state_dir()
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._state_dir / "agents.db"
        self._db = self._get_or_create_db()

    def _get_or_create_db(self) -> sqlite3.Connection:
        """Get or create database connection."""
        # check_same_thread=False allows TestClient to access the DB from
        # a different thread (test thread vs event loop thread).
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables(conn)
        return conn

    def _create_tables(self, conn: sqlite3.Connection) -> None:
        """Create invites table."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_invites (
                code TEXT,
                code_hash TEXT UNIQUE,
                colony_id TEXT NOT NULL,
                
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                max_uses INTEGER DEFAULT 1,
                use_count INTEGER DEFAULT 0,
                
                failed_attempts INTEGER DEFAULT 0,
                locked_until TIMESTAMP,
                
                used_at TIMESTAMP,
                used_by_agent_id TEXT,
                used_by_node_id TEXT,
                
                granted_capabilities TEXT DEFAULT '[]',
                granted_is_primary INTEGER DEFAULT 0,
                granted_max_concurrent INTEGER DEFAULT 5,
                
                created_by_agent_id TEXT,
                label TEXT
            )
            """
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_invites_expires ON agent_invites(expires_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_invites_code_hash ON agent_invites(code_hash)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_invites_locked ON agent_invites(locked_until)"
        )
        conn.commit()

    def create(
        self,
        colony_id: str,
        capabilities: Optional[List[str]] = None,
        is_primary: bool = False,
        max_concurrent: int = 5,
        expires_seconds: Optional[int] = None,
        label: Optional[str] = None,
        created_by_agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new invite."""
        code = generate_setup_code()
        code_hash = hash_setup_code(code)

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=expires_seconds or self.DEFAULT_EXPIRY_SECONDS)

        self._db.execute(
            """
            INSERT INTO agent_invites (
                code, code_hash, colony_id,
                expires_at,
                granted_capabilities, granted_is_primary, granted_max_concurrent,
                label, created_by_agent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                code,  # Keep for display, but lookups use hash
                code_hash,
                colony_id,
                expires_at.isoformat(),
                json.dumps(capabilities or []),
                1 if is_primary else 0,
                max_concurrent,
                label,
                created_by_agent_id,
            ],
        )
        self._db.commit()

        return {
            "setup_code": code,  # Return plaintext once
            "code_hash": code_hash,
            "expires_at": expires_at.isoformat(),
            "expires_in_seconds": expires_seconds or self.DEFAULT_EXPIRY_SECONDS,
            "capabilities": capabilities or [],
            "is_primary": is_primary,
            "max_concurrent": max_concurrent,
        }

    def validate(self, code: str) -> Dict[str, Any]:
        """Validate setup code (checking rate limits).

        Returns invite data if valid.
        Raises ValueError if invalid, expired, used, or locked.
        """
        code_hash = hash_setup_code(code)
        now = datetime.now(timezone.utc)

        cursor = self._db.execute(
            "SELECT * FROM agent_invites WHERE code_hash = ?",
            [code_hash],
        )
        invite = cursor.fetchone()

        if not invite:
            raise ValueError("Invalid setup code")

        invite = dict(invite)

        # Check if locked
        if invite.get("locked_until"):
            locked_until = datetime.fromisoformat(invite["locked_until"])
            if now < locked_until:
                raise ValueError(f"Setup code locked until {locked_until.isoformat()}")

        # Check expiry
        expires_at = datetime.fromisoformat(invite["expires_at"])
        if now > expires_at:
            raise ValueError("Setup code expired")

        # Check usage
        if invite["use_count"] >= invite["max_uses"]:
            raise ValueError("Setup code already used")

        return invite

    def record_failed_attempt(self, code: str) -> None:
        """Record failed validation attempt and check lockout."""
        code_hash = hash_setup_code(code)
        now = datetime.now(timezone.utc)

        # Increment failed attempts
        cursor = self._db.execute(
            "UPDATE agent_invites SET failed_attempts = failed_attempts + 1 WHERE code_hash = ?",
            [code_hash],
        )

        if cursor.rowcount == 0:
            return

        # Check if we should lock
        cursor = self._db.execute(
            "SELECT failed_attempts FROM agent_invites WHERE code_hash = ?",
            [code_hash],
        )
        row = cursor.fetchone()
        if row and row["failed_attempts"] >= self.MAX_FAILED_ATTEMPTS:
            locked_until = now + timedelta(minutes=self.LOCKOUT_MINUTES)
            self._db.execute(
                "UPDATE agent_invites SET locked_until = ? WHERE code_hash = ?",
                [locked_until.isoformat(), code_hash],
            )
            logger.warning("Setup code locked due to %d failed attempts", row["failed_attempts"])

        self._db.commit()

    def clear_failed_attempts(self, code: str) -> None:
        """Clear failed attempts after successful use."""
        code_hash = hash_setup_code(code)
        self._db.execute(
            "UPDATE agent_invites SET failed_attempts = 0 WHERE code_hash = ?",
            [code_hash],
        )
        self._db.commit()

    def use(
        self,
        code: str,
        node_id: str,
        agent_id: str,
    ) -> Dict[str, Any]:
        """Use setup code (atomic operation).

        This validates, marks as used, and returns the invite data.
        Raises ValueError if already used or invalid.
        """
        code_hash = hash_setup_code(code)
        now = datetime.now(timezone.utc)

        # Atomic UPDATE with WHERE conditions
        cursor = self._db.execute(
            """
            UPDATE agent_invites
            SET used_at = ?,
                used_by_node_id = ?,
                used_by_agent_id = ?,
                use_count = use_count + 1
            WHERE code_hash = ?
            AND used_at IS NULL
            AND expires_at > ?
            AND (locked_until IS NULL OR locked_until < ?)
            """,
            [
                now.isoformat(),
                node_id,
                agent_id,
                code_hash,
                now.isoformat(),
                now.isoformat(),
            ],
        )
        self._db.commit()

        if cursor.rowcount == 0:
            # Either already used, expired, or locked
            raise ValueError("Setup code already used, expired, or locked")

        # Get the invite
        cursor = self._db.execute(
            "SELECT * FROM agent_invites WHERE code_hash = ?",
            [code_hash],
        )
        return dict(cursor.fetchone())

    def get(self, code: str) -> Optional[Dict[str, Any]]:
        """Get invite by code."""
        code_hash = hash_setup_code(code)
        cursor = self._db.execute(
            "SELECT * FROM agent_invites WHERE code_hash = ?",
            [code_hash],
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def list(
        self,
        colony_id: Optional[str] = None,
        unused_only: bool = False,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List invites."""
        query = "SELECT * FROM agent_invites WHERE 1=1"
        params: List[Any] = []

        if colony_id:
            query += " AND colony_id = ?"
            params.append(colony_id)

        if unused_only:
            query += " AND used_at IS NULL"

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cursor = self._db.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def delete(self, code: str) -> bool:
        """Delete an invite."""
        code_hash = hash_setup_code(code)
        cursor = self._db.execute(
            "DELETE FROM agent_invites WHERE code_hash = ?",
            [code_hash],
        )
        self._db.commit()
        return cursor.rowcount > 0
