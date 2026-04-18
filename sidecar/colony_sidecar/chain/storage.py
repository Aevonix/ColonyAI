"""SQLite-backed persistent storage for ColonyChain.

Stores blocks, transactions, mempool, checkpoints, and chain metadata.
Supports full chain replay and state verification.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from .block import Block
from .transactions import (
    ChainState,
    ColonyRecord,
    KeyHistoryEntry,
    ProtocolConfig,
    ProtocolUpgradeRecord,
    SentinelRecord,
    Transaction,
    TrustEdge,
    TxType,
    UntrustEvent,
)

logger = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS chain_blocks (
    block_index     INTEGER PRIMARY KEY,
    block_hash      TEXT    NOT NULL UNIQUE,
    previous_hash   TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    merkle_root     TEXT    NOT NULL,
    producer_id     TEXT    NOT NULL,
    signature       TEXT    NOT NULL,
    raw_json        TEXT    NOT NULL,
    committed_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS chain_transactions (
    tx_id           TEXT    PRIMARY KEY,
    block_index     INTEGER NOT NULL REFERENCES chain_blocks(block_index),
    tx_type         TEXT    NOT NULL,
    from_colony_id  TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    nonce           INTEGER NOT NULL,
    payload_json    TEXT    NOT NULL,
    signature       TEXT    NOT NULL,
    UNIQUE(from_colony_id, nonce)
);

CREATE TABLE IF NOT EXISTS chain_checkpoints (
    height          INTEGER PRIMARY KEY,
    state_json      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS chain_meta (
    key             TEXT    PRIMARY KEY,
    value           TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS chain_mempool (
    tx_id           TEXT    PRIMARY KEY,
    tx_type         TEXT    NOT NULL,
    from_colony_id  TEXT    NOT NULL,
    received_at     TEXT    NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 0,
    payload_json    TEXT    NOT NULL,
    signature       TEXT    NOT NULL,
    raw_json        TEXT    NOT NULL,
    expires_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_tx_colony    ON chain_transactions(from_colony_id);
CREATE INDEX IF NOT EXISTS idx_tx_type      ON chain_transactions(tx_type);
CREATE INDEX IF NOT EXISTS idx_block_ts     ON chain_blocks(timestamp);
CREATE INDEX IF NOT EXISTS idx_tx_block     ON chain_transactions(block_index);
CREATE INDEX IF NOT EXISTS idx_mempool_ord  ON chain_mempool(priority DESC, received_at ASC);
CREATE INDEX IF NOT EXISTS idx_mempool_exp  ON chain_mempool(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_block_prod   ON chain_blocks(producer_id);
"""


class ChainStore:
    """SQLite-backed persistent chain storage."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")   # durability > speed for chain data (SEC-14-L-04)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA cache_size=-8000")
        return conn

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._tx() as conn:
            conn.executescript(_SCHEMA)

    # ── Block operations ────────────────────────────────────────────────────

    def append_block(self, block: Block) -> None:
        """Append a committed block. Raises if hash chain is broken."""
        latest = self.get_latest_block()
        if latest is not None:
            if block.previous_hash != latest.block_hash:
                raise ValueError(
                    f"Hash chain broken: block {block.index} previous_hash "
                    f"{block.previous_hash!r} != latest hash {latest.block_hash!r}"
                )
            if block.index != latest.index + 1:
                raise ValueError(
                    f"Block index gap: expected {latest.index + 1}, got {block.index}"
                )
        else:
            if block.index != 0:
                raise ValueError(f"First block must have index 0, got {block.index}")

        raw_json = json.dumps(block.to_dict(), sort_keys=True)
        committed_at = datetime.now(timezone.utc).isoformat()
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO chain_blocks
                   (block_index, block_hash, previous_hash, timestamp,
                    merkle_root, producer_id, signature, raw_json, committed_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    block.index,
                    block.block_hash,
                    block.previous_hash,
                    block.timestamp,
                    block.merkle_root,
                    block.producer_id,
                    block.signature,
                    raw_json,
                    committed_at,
                ),
            )
            for tx in block.transactions:
                conn.execute(
                    """INSERT OR IGNORE INTO chain_transactions
                       (tx_id, block_index, tx_type, from_colony_id, timestamp,
                        nonce, payload_json, signature)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        tx.tx_id,
                        block.index,
                        tx.type.value if isinstance(tx.type, TxType) else tx.type,
                        tx.from_colony_id,
                        tx.timestamp,
                        tx.nonce,
                        json.dumps(tx.payload),
                        tx.signature,
                    ),
                )
            # Remove from mempool
            for tx in block.transactions:
                conn.execute(
                    "DELETE FROM chain_mempool WHERE tx_id = ?", (tx.tx_id,)
                )

    def get_block(self, index: int) -> Block | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT raw_json FROM chain_blocks WHERE block_index = ?", (index,)
            ).fetchone()
        if row is None:
            return None
        return Block.from_dict(json.loads(row["raw_json"]))

    def get_block_by_hash(self, block_hash: str) -> Block | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT raw_json FROM chain_blocks WHERE block_hash = ?", (block_hash,)
            ).fetchone()
        if row is None:
            return None
        return Block.from_dict(json.loads(row["raw_json"]))

    def get_latest_block(self) -> Block | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT raw_json FROM chain_blocks ORDER BY block_index DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return Block.from_dict(json.loads(row["raw_json"]))

    def get_block_range(self, from_index: int, to_index: int) -> list[Block]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT raw_json FROM chain_blocks "
                "WHERE block_index >= ? AND block_index <= ? "
                "ORDER BY block_index ASC",
                (from_index, to_index),
            ).fetchall()
        return [Block.from_dict(json.loads(r["raw_json"])) for r in rows]

    def get_height(self) -> int:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT MAX(block_index) as h FROM chain_blocks"
            ).fetchone()
        if row is None or row["h"] is None:
            return -1
        return row["h"]

    # ── Mempool operations ──────────────────────────────────────────────────

    def add_to_mempool(self, tx: Transaction) -> None:
        """Add a validated transaction to the mempool."""
        received_at = datetime.now(timezone.utc).isoformat()
        raw_json = json.dumps(tx.to_dict())
        with self._tx() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO chain_mempool
                   (tx_id, tx_type, from_colony_id, received_at, priority,
                    payload_json, signature, raw_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    tx.tx_id,
                    tx.type.value if isinstance(tx.type, TxType) else tx.type,
                    tx.from_colony_id,
                    received_at,
                    0,
                    json.dumps(tx.payload),
                    tx.signature,
                    raw_json,
                ),
            )

    def get_mempool(self, limit: int = 500) -> list[Transaction]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT raw_json FROM chain_mempool "
                "ORDER BY priority DESC, received_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Transaction.from_dict(json.loads(r["raw_json"])) for r in rows]

    def remove_from_mempool(self, tx_ids: list[str]) -> None:
        if not tx_ids:
            return
        with self._tx() as conn:
            placeholders = ",".join("?" for _ in tx_ids)
            conn.execute(
                f"DELETE FROM chain_mempool WHERE tx_id IN ({placeholders})", tx_ids
            )

    def mempool_size(self) -> int:
        with self._get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) as n FROM chain_mempool").fetchone()
        return row["n"] if row else 0

    # ── Nonce tracking ──────────────────────────────────────────────────────

    def get_nonce(self, colony_id: str) -> int:
        """Return the highest accepted nonce for colony_id (0 if none)."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT MAX(nonce) as n FROM chain_transactions WHERE from_colony_id = ?",
                (colony_id,),
            ).fetchone()
        if row is None or row["n"] is None:
            return 0
        return row["n"]

    # ── Checkpoint operations ───────────────────────────────────────────────

    def save_checkpoint(self, state: ChainState, height: int) -> None:
        state_json = _serialize_chain_state(state)
        created_at = datetime.now(timezone.utc).isoformat()
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO chain_checkpoints (height, state_json, created_at) "
                "VALUES (?,?,?)",
                (height, state_json, created_at),
            )

    def get_latest_checkpoint(
        self, at_or_before: int
    ) -> tuple[ChainState, int] | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT height, state_json FROM chain_checkpoints "
                "WHERE height <= ? ORDER BY height DESC LIMIT 1",
                (at_or_before,),
            ).fetchone()
        if row is None:
            return None
        state = _deserialize_chain_state(row["state_json"])
        return state, row["height"]

    # ── Query helpers ───────────────────────────────────────────────────────

    def get_transactions_for_colony(self, colony_id: str) -> list[Transaction]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT tx_id, tx_type, from_colony_id, timestamp, nonce, "
                "payload_json, signature FROM chain_transactions "
                "WHERE from_colony_id = ? ORDER BY nonce ASC",
                (colony_id,),
            ).fetchall()
        result = []
        for r in rows:
            result.append(
                Transaction(
                    tx_id=r["tx_id"],
                    type=TxType(r["tx_type"]),
                    from_colony_id=r["from_colony_id"],
                    timestamp=r["timestamp"],
                    nonce=r["nonce"],
                    payload=json.loads(r["payload_json"]),
                    signature=r["signature"],
                )
            )
        return result

    # ── Metadata ────────────────────────────────────────────────────────────

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO chain_meta (key, value) VALUES (?,?)",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM chain_meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None


# ---------------------------------------------------------------------------
# ChainState serialization helpers
# ---------------------------------------------------------------------------

def _serialize_chain_state(state: ChainState) -> str:
    """Serialize ChainState to JSON string."""

    def _ke(e: KeyHistoryEntry) -> dict:
        return {
            "public_key_hex": e.public_key_hex,
            "active_from_height": e.active_from_height,
            "rotated_at_height": e.rotated_at_height,
            "revoked_at_height": e.revoked_at_height,
            "revocation_tx": e.revocation_tx,
        }

    def _te(e: TrustEdge) -> dict:
        return {
            "from_colony_id": e.from_colony_id,
            "to_colony_id": e.to_colony_id,
            "trust_level": e.trust_level,
            "attested_at_height": e.attested_at_height,
            "attested_at_tx": e.attested_at_tx,
            "valid_until": e.valid_until,
        }

    def _ue(e: UntrustEvent) -> dict:
        return {
            "from_colony_id": e.from_colony_id,
            "target_colony_id": e.target_colony_id,
            "at_height": e.at_height,
            "tx_id": e.tx_id,
            "report_abuse": e.report_abuse,
            "timestamp": e.timestamp,
        }

    def _sr(r: SentinelRecord) -> dict:
        return {
            "sentinel_id": r.sentinel_id,
            "colony_id": r.colony_id,
            "host": r.host,
            "port": r.port,
            "public_key_hex": r.public_key_hex,
            "registered_at_height": r.registered_at_height,
            "status": r.status,
            "uptime_percent": r.uptime_percent,
        }

    def _cr(r: ColonyRecord) -> dict:
        return {
            "colony_id": r.colony_id,
            "name": r.name,
            "active_public_key_hex": r.active_public_key_hex,
            "endpoint": r.endpoint,
            "description": r.description,
            "capabilities": r.capabilities,
            "protocol_version": r.protocol_version,
            "registered_at_height": r.registered_at_height,
            "registered_at_tx": r.registered_at_tx,
            "is_genesis_admin": r.is_genesis_admin,
            "status": r.status,
            "metadata": r.metadata,
        }

    data: dict[str, Any] = {
        "height": state.height,
        "last_block_hash": state.last_block_hash,
        "colony_registry": {k: _cr(v) for k, v in state.colony_registry.items()},
        "name_registry": state.name_registry,
        "key_history": {k: [_ke(e) for e in v] for k, v in state.key_history.items()},
        "trust_graph": {
            json.dumps(list(k)): _te(v) for k, v in state.trust_graph.items()
        },
        "untrust_counters": {
            k: [_ue(e) for e in v] for k, v in state.untrust_counters.items()
        },
        "sentinel_roster": {k: _sr(v) for k, v in state.sentinel_roster.items()},
        "suspended_colonies": list(state.suspended_colonies),
        "protocol_config": {
            "block_interval_secs": state.protocol_config.block_interval_secs,
            "untrust_threshold": state.protocol_config.untrust_threshold,
            "untrust_window_days": state.protocol_config.untrust_window_days,
            "uptime_requirement_percent": state.protocol_config.uptime_requirement_percent,
            "min_sentinels": state.protocol_config.min_sentinels,
            "min_protocol_version": state.protocol_config.min_protocol_version,
        },
        "upgrade_history": [
            {
                "upgrade_id": u.upgrade_id,
                "title": u.title,
                "proposed_at_height": u.proposed_at_height,
                "activation_height": u.activation_height,
                "ratified": u.ratified,
                "changes": u.changes,
            }
            for u in state.upgrade_history
        ],
        "genesis_admin_id": state.genesis_admin_id,
        "network_id": state.network_id,
    }
    return json.dumps(data, sort_keys=True)


def _deserialize_chain_state(state_json: str) -> ChainState:
    """Deserialize ChainState from JSON string."""
    data = json.loads(state_json)
    state = ChainState()
    state.height = data["height"]
    state.last_block_hash = data["last_block_hash"]

    for k, v in data["colony_registry"].items():
        state.colony_registry[k] = ColonyRecord(**v)

    state.name_registry = data["name_registry"]

    for k, entries in data["key_history"].items():
        state.key_history[k] = [KeyHistoryEntry(**e) for e in entries]

    for k_str, v in data["trust_graph"].items():
        k_list = json.loads(k_str)
        key = (k_list[0], k_list[1])
        state.trust_graph[key] = TrustEdge(**v)

    for k, events in data["untrust_counters"].items():
        state.untrust_counters[k] = [UntrustEvent(**e) for e in events]

    for k, v in data["sentinel_roster"].items():
        state.sentinel_roster[k] = SentinelRecord(**v)

    state.suspended_colonies = set(data["suspended_colonies"])

    pc = data["protocol_config"]
    state.protocol_config = ProtocolConfig(**pc)

    state.upgrade_history = [
        ProtocolUpgradeRecord(**u) for u in data.get("upgrade_history", [])
    ]
    state.genesis_admin_id = data.get("genesis_admin_id", "")
    state.network_id = data.get("network_id", "")
    return state
