"""ChainManager — main coordinator tying all chain components together.

Responsibilities:
- Owns the ChainStore, ChainStateMachine, and Mempool
- Chain sync protocol (pull blocks from Sentinels)
- New block announcement broadcasting
- Transaction submission and nonce tracking
- Integration hook for the federation module
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from .block import Block
from .state_machine import ChainStateMachine
from .storage import ChainStore
from .transactions import ChainState, Transaction, TxType
from .validation import BlockValidator, TransactionValidator, ValidationResult
from .protocol import (
    ChainAnnouncement,
    ChainStateResponse,
    ChainSyncRequest,
    ChainSyncResponse,
    TxSubmitResponse,
)

logger = logging.getLogger(__name__)

_CHECKPOINT_INTERVAL = 100

_chain_manager_instance: Optional["ChainManager"] = None


@dataclass
class SubmitResult:
    accepted: bool
    tx_id: str
    reason: str = ""


class ChainManager:
    """Main coordinator for ColonyChain.

    Manages the local chain replica, handles sync with Sentinels,
    validates and queues transactions, and broadcasts new blocks.
    """

    def __init__(
        self,
        db_path: Path | str,
        colony_id: str,
        send_message: Callable[[str, dict[str, Any]], Coroutine] | None = None,
    ) -> None:
        self.colony_id = colony_id
        self.store = ChainStore(db_path)
        self.state_machine = ChainStateMachine(self.store)
        self._send_message = send_message
        self._state: ChainState | None = None
        self._state_lock = asyncio.Lock()
        self._block_validator = BlockValidator()

    # ── State access ────────────────────────────────────────────────────────

    async def get_state(self) -> ChainState:
        """Return current chain state (thread-safe cached)."""
        async with self._state_lock:
            if self._state is None:
                self._state = self.state_machine.get_current_state()
            return self._state

    def _invalidate_state(self) -> None:
        self._state = None

    # ── Transaction submission ───────────────────────────────────────────────

    async def submit_transaction(self, tx: Transaction) -> SubmitResult:
        """Validate and add a transaction to the mempool."""
        state = await self.get_state()

        # Nonce check (against persisted chain)
        if tx.from_colony_id != "system":
            persisted_nonce = self.store.get_nonce(tx.from_colony_id)
            if tx.nonce <= persisted_nonce:
                return SubmitResult(
                    accepted=False,
                    tx_id=tx.tx_id,
                    reason=f"nonce {tx.nonce} <= last accepted {persisted_nonce}",
                )

        validator = TransactionValidator(state)
        result = validator.validate(tx)
        if not result.ok:
            return SubmitResult(accepted=False, tx_id=tx.tx_id, reason=result.reason)

        self.store.add_to_mempool(tx)
        return SubmitResult(accepted=True, tx_id=tx.tx_id)

    # ── Block commitment ─────────────────────────────────────────────────────

    async def commit_block(self, block: Block) -> None:
        """Commit a block to the chain and update state."""
        latest = self.store.get_latest_block()
        bv = self._block_validator
        result = bv.validate_block(block, latest)
        if not result.ok:
            raise ValueError(f"Block validation failed: {result.reason}")

        self.store.append_block(block)
        self._invalidate_state()

        # Save checkpoint every 100 blocks
        if block.index > 0 and block.index % _CHECKPOINT_INTERVAL == 0:
            state = await self.get_state()
            self.store.save_checkpoint(state, block.index)

        # Announce to peers
        announcement = ChainAnnouncement(
            block_index=block.index,
            block_hash=block.block_hash,
            producer_id=block.producer_id,
            tx_count=len(block.transactions),
        )
        await self._broadcast(announcement.to_dict())

        logger.info(
            "Block %d committed (%d txs, hash %s…)",
            block.index,
            len(block.transactions),
            block.block_hash[:16],
        )

    # ── Chain sync ───────────────────────────────────────────────────────────

    async def sync_from_sentinel(
        self,
        sentinel_id: str,
        from_height: int | None = None,
    ) -> int:
        """Pull blocks from a Sentinel to catch up. Returns blocks added."""
        if from_height is None:
            from_height = self.store.get_height() + 1

        req = ChainSyncRequest(
            from_index=from_height,
            to_index=from_height + 499,  # batch up to 500 blocks
            requester_id=self.colony_id,
        )
        if self._send_message:
            await self._send_message(sentinel_id, req.to_dict())
        return 0

    async def handle_chain_sync_response(self, data: dict[str, Any]) -> int:
        """Process a CHAIN_SYNC_RESPONSE message. Returns blocks committed."""
        blocks_data = data.get("blocks", [])
        committed = 0
        for block_dict in blocks_data:
            block = Block.from_dict(block_dict)
            try:
                await self.commit_block(block)
                committed += 1
            except ValueError as exc:
                logger.warning("Skipping block %d: %s", block.index, exc)
                break
        return committed

    async def handle_chain_announce(self, data: dict[str, Any]) -> None:
        """Handle a CHAIN_ANNOUNCE — trigger sync if we're behind."""
        announced_height = data.get("block_index", 0)
        our_height = self.store.get_height()
        if announced_height > our_height + 1:
            sender_id = data.get("producer_id", "")
            if sender_id:
                await self.sync_from_sentinel(sender_id, from_height=our_height + 1)

    async def handle_tx_submit(
        self, data: dict[str, Any]
    ) -> TxSubmitResponse:
        """Handle a TX_SUBMIT message from another colony."""
        tx_json = data.get("tx_json", "{}")
        try:
            tx_dict = json.loads(tx_json)
            tx = Transaction.from_dict(tx_dict)
        except Exception as exc:
            return TxSubmitResponse(tx_id="", accepted=False, reason=str(exc))

        result = await self.submit_transaction(tx)
        return TxSubmitResponse(
            tx_id=result.tx_id,
            accepted=result.accepted,
            reason=result.reason,
        )

    async def get_chain_state_summary(
        self, responder_id: str | None = None
    ) -> ChainStateResponse:
        """Build a ChainStateResponse summary."""
        state = await self.get_state()
        return ChainStateResponse(
            height=state.height,
            last_block_hash=state.last_block_hash,
            n_colonies=len(state.colony_registry),
            n_sentinels=len(state.active_sentinels()),
            n_suspended=len(state.suspended_colonies),
            network_id=state.network_id,
            responder_id=responder_id or self.colony_id,
        )

    # ── Mempool helpers ──────────────────────────────────────────────────────

    def drain_mempool(self, max_txs: int = 500) -> list[Transaction]:
        """Drain pending transactions for block production."""
        return self.store.get_mempool(limit=max_txs)

    def evict_included(self, tx_ids: list[str]) -> None:
        self.store.remove_from_mempool(tx_ids)

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        """Broadcast a message to all known peers (best-effort)."""
        # In a real deployment this would iterate discovered peers.
        # Here we call the send_message hook if provided.
        if self._send_message:
            state = await self.get_state()
            for sentinel in state.active_sentinels():
                try:
                    await self._send_message(sentinel.sentinel_id, msg)
                except Exception as exc:
                    logger.debug(
                        "Failed to send to sentinel %s: %s", sentinel.sentinel_id, exc
                    )

    def get_height(self) -> int:
        return self.store.get_height()

    @classmethod
    def get_instance(cls) -> "ChainManager":
        """Return the process-wide singleton backed by ~/.colony/chain.db."""
        global _chain_manager_instance
        if _chain_manager_instance is None:
            colony_home = Path(os.environ.get("COLONY_HOME", str(Path.home() / ".colony")))
            db_path = colony_home / "chain.db"
            # Derive colony_id from genesis block cryptographic identity
            colony_id = "local"
            if db_path.exists():
                try:
                    conn = sqlite3.connect(str(db_path))
                    row = conn.execute(
                        "SELECT raw_json FROM chain_blocks WHERE block_index = 0"
                    ).fetchone()
                    if row:
                        genesis_data = json.loads(row[0])
                        # Use the cryptographic colony_id (sha256 of Ed25519 pubkey)
                        # from genesis block metadata — not producer_id which is
                        # just a human-readable label and trivially spoofable.
                        colony_id = (
                            genesis_data.get("metadata", {}).get("genesis_colony_id")
                            or genesis_data.get("producer_id", "local")
                        )
                    conn.close()
                except Exception:
                    pass
            _chain_manager_instance = cls(db_path=db_path, colony_id=colony_id)
        return _chain_manager_instance

    def get_status(self) -> dict:
        """Return chain status as a plain dict for the API router."""
        height = self.store.get_height()
        latest = self.store.get_latest_block()
        mempool = self.store.get_mempool(limit=10000)
        return {
            "height": height,
            "last_block_hash": latest.block_hash if latest else "0" * 64,
            "last_block_timestamp": latest.timestamp if latest else None,
            "colony_id": self.colony_id,
            "mempool_size": len(mempool),
            "protocol_version": "1.0.0",
            "sync_status": "synced",
        }
