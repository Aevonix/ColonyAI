"""Raft-Lite consensus among Sentinel validators.

Simplified Raft for 30-second block intervals. Omits log compaction,
membership change entries, and read linearizability optimizations.

State machine: FOLLOWER → CANDIDATE → LEADER → FOLLOWER
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

from .block import Block, build_merkle_root
from .transactions import ChainState, Transaction

logger = logging.getLogger(__name__)


class RaftState(str, Enum):
    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"


@dataclass
class RaftConfig:
    sentinel_id: str
    election_timeout_min_ms: int = 150
    election_timeout_max_ms: int = 300
    heartbeat_interval_ms: int = 5_000
    block_interval_secs: float = 30.0


@dataclass
class VoteRequest:
    term: int
    candidate_id: str
    last_log_index: int
    last_log_hash: str


@dataclass
class VoteResponse:
    term: int
    granted: bool
    voter_id: str


@dataclass
class BlockProposal:
    term: int
    block: Block


@dataclass
class BlockAck:
    term: int
    block_hash: str
    sentinel_id: str
    signature: str


@dataclass
class BlockNack:
    term: int
    block_hash: str
    sentinel_id: str
    reason: str


@dataclass
class CommitBlock:
    term: int
    block: Block


@dataclass
class Heartbeat:
    term: int
    leader_id: str
    height: int
    last_block_hash: str


class RaftNode:
    """A single Raft-Lite node (Sentinel) participating in consensus.

    The caller provides:
    - A list of peer sentinel IDs and a send_message coroutine for network I/O
    - A sign_block callable to sign produced blocks
    - A build_block callable to assemble a candidate block from the mempool
    - A on_commit callable invoked when a block is committed
    """

    def __init__(
        self,
        config: RaftConfig,
        peer_ids: list[str],
        send_message: Callable[[str, dict[str, Any]], Coroutine],
        sign_block: Callable[[Block], str],
        build_block: Callable[[], Block],
        on_commit: Callable[[Block], Coroutine],
        sign_data: Optional[Callable[[bytes], str]] = None,
        get_peer_pubkey: Optional[Callable[[str], Optional[bytes]]] = None,
    ) -> None:
        self.config = config
        self.peer_ids = list(peer_ids)
        self.send_message = send_message
        self.sign_block = sign_block
        self.build_block = build_block
        self.on_commit = on_commit
        self.sign_data = sign_data
        self.get_peer_pubkey = get_peer_pubkey

        self.state = RaftState.FOLLOWER
        self.current_term = 0
        self.voted_for: str | None = None
        self.height = 0
        self.last_block_hash = "0" * 64

        # Metrics for monitoring
        self._metrics: dict[str, int] = {
            "blocks_rejected": 0,
            "blocks_accepted": 0,
            "invalid_signatures": 0,
        }

        self._election_timeout: float = self._random_timeout()
        self._last_heartbeat: float = time.monotonic()
        self._votes_received: set[str] = set()
        self._acks_received: dict[str, BlockAck] = {}
        self._pending_block: Block | None = None
        self._running = False
        self._tasks: list[asyncio.Task] = []

    def _random_timeout(self) -> float:
        import secrets
        spread = self.config.election_timeout_max_ms - self.config.election_timeout_min_ms
        return (self.config.election_timeout_min_ms + secrets.randbelow(spread + 1)) / 1000.0

    @property
    def is_leader(self) -> bool:
        return self.state == RaftState.LEADER

    def quorum(self) -> int:
        n = len(self.peer_ids) + 1  # include self
        return max(1, (n * 2 // 3) + 1)

    def get_metrics(self) -> dict[str, int]:
        """Return consensus metrics for monitoring."""
        return dict(self._metrics)

    async def start(self) -> None:
        self._running = True
        self._tasks = [
            asyncio.create_task(self._election_loop()),
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._block_production_loop()),
        ]

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── Loops ───────────────────────────────────────────────────────────────

    async def _election_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.05)
            if self.state == RaftState.LEADER:
                continue
            elapsed = time.monotonic() - self._last_heartbeat
            if elapsed >= self._election_timeout:
                await self._start_election()

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.config.heartbeat_interval_ms / 1000.0)
            if self.state == RaftState.LEADER:
                await self._send_heartbeat()

    async def _block_production_loop(self) -> None:
        """Produce blocks on the configured interval when leader."""
        while self._running:
            await asyncio.sleep(self.config.block_interval_secs)
            if self.state == RaftState.LEADER:
                await self._produce_block()

    # ── Election ────────────────────────────────────────────────────────────

    async def _start_election(self) -> None:
        self.current_term += 1
        self.state = RaftState.CANDIDATE
        self.voted_for = self.config.sentinel_id
        self._votes_received = {self.config.sentinel_id}
        self._election_timeout = self._random_timeout()
        self._last_heartbeat = time.monotonic()

        logger.info(
            "%s starting election for term %d",
            self.config.sentinel_id,
            self.current_term,
        )

        req = VoteRequest(
            term=self.current_term,
            candidate_id=self.config.sentinel_id,
            last_log_index=self.height,
            last_log_hash=self.last_block_hash,
        )
        for peer_id in self.peer_ids:
            asyncio.create_task(
                self.send_message(
                    peer_id,
                    {"type": "VOTE_REQUEST", "data": self._vote_request_dict(req)},
                )
            )

        # Single-node cluster: self-vote satisfies quorum immediately
        if len(self._votes_received) >= self.quorum():
            await self._become_leader()

    def _vote_request_dict(self, req: VoteRequest) -> dict:
        return {
            "term": req.term,
            "candidate_id": req.candidate_id,
            "last_log_index": req.last_log_index,
            "last_log_hash": req.last_log_hash,
        }

    async def handle_vote_request(self, data: dict) -> None:
        req = VoteRequest(**data)
        # Reject vote requests from nodes not in our known peer set
        if req.candidate_id not in self.peer_ids:
            logger.warning(
                "Dropping VOTE_REQUEST from unknown candidate %s", req.candidate_id
            )
            return
        granted = False
        if req.term > self.current_term:
            self.current_term = req.term
            self.state = RaftState.FOLLOWER
            self.voted_for = None

        if req.term >= self.current_term and (
            self.voted_for is None or self.voted_for == req.candidate_id
        ):
            # Grant if candidate log is at least as up-to-date as ours
            if req.last_log_index >= self.height:
                granted = True
                self.voted_for = req.candidate_id
                self._last_heartbeat = time.monotonic()

        resp = {
            "type": "VOTE_RESPONSE",
            "data": {
                "term": self.current_term,
                "granted": granted,
                "voter_id": self.config.sentinel_id,
            },
        }
        await self.send_message(req.candidate_id, resp)

    async def handle_vote_response(self, data: dict) -> None:
        resp = VoteResponse(**data)
        if self.state != RaftState.CANDIDATE:
            return
        if resp.term > self.current_term:
            self.current_term = resp.term
            self.state = RaftState.FOLLOWER
            return
        if resp.granted:
            self._votes_received.add(resp.voter_id)
            if len(self._votes_received) >= self.quorum():
                await self._become_leader()

    async def _become_leader(self) -> None:
        self.state = RaftState.LEADER
        logger.info(
            "%s became leader for term %d", self.config.sentinel_id, self.current_term
        )
        await self._send_heartbeat()

    async def _send_heartbeat(self) -> None:
        hb = {
            "type": "HEARTBEAT",
            "data": {
                "term": self.current_term,
                "leader_id": self.config.sentinel_id,
                "height": self.height,
                "last_block_hash": self.last_block_hash,
            },
        }
        for peer_id in self.peer_ids:
            asyncio.create_task(self.send_message(peer_id, hb))

    async def handle_heartbeat(self, data: dict) -> None:
        hb = Heartbeat(**data)
        if hb.term >= self.current_term:
            self.current_term = hb.term
            self.state = RaftState.FOLLOWER
            self._last_heartbeat = time.monotonic()

    # ── Block production ────────────────────────────────────────────────────

    async def _produce_block(self) -> None:
        if not self.peer_ids:
            # Solo Sentinel — commit immediately (even empty blocks as chain heartbeats)
            block = self.build_block()
            block.signature = self.sign_block(block)
            await self.on_commit(block)
            self.height = block.index
            self.last_block_hash = block.block_hash
            return

        block = self.build_block()
        block.signature = ""  # will be filled on commit
        self._pending_block = block
        self._acks_received = {}

        proposal = {
            "type": "PROPOSE_BLOCK",
            "data": {"term": self.current_term, "block": block.to_dict()},
        }
        for peer_id in self.peer_ids:
            asyncio.create_task(self.send_message(peer_id, proposal))

    async def handle_propose_block(self, data: dict) -> None:
        from .block import Block as BlockCls

        term = data["term"]
        block = BlockCls.from_dict(data["block"])

        if term < self.current_term:
            return

        self._last_heartbeat = time.monotonic()
        
        # Validate block (comprehensive checks)
        validation_errors = []
        
        # 1. Index and previous_hash (chain integrity)
        if block.index != self.height + 1:
            validation_errors.append(f"index mismatch: expected {self.height + 1}, got {block.index}")
        if block.previous_hash != self.last_block_hash:
            validation_errors.append("previous_hash mismatch")
        
        # 2. Merkle root (transaction integrity)
        tx_ids = [tx.tx_id for tx in block.transactions]
        expected_merkle = build_merkle_root(tx_ids)
        if block.merkle_root != expected_merkle:
            validation_errors.append("merkle_root mismatch")
        
        # 3. Timestamp (not in future, reasonable)
        try:
            block_ts = datetime.fromisoformat(block.timestamp.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if block_ts > now:
                validation_errors.append("timestamp in future")
        except (ValueError, AttributeError):
            validation_errors.append("invalid timestamp format")
        
        is_valid = len(validation_errors) == 0
        
        if is_valid:
            self._metrics["blocks_accepted"] += 1
            msg_type = "BLOCK_ACK"
            msg_data: dict = {
                "term": term,
                "block_hash": block.block_hash,
                "sentinel_id": self.config.sentinel_id,
            }
            canonical = json.dumps(
                {"type": "BLOCK_ACK", "term": term,
                 "block_hash": block.block_hash, "sentinel_id": self.config.sentinel_id},
                sort_keys=True, separators=(",", ":")
            )
            if self.sign_data is None:
                logger.error("sign_data not configured; cannot sign BLOCK_ACK")
                return
            signature = self.sign_data(canonical.encode())
            msg_data["signature"] = signature
        else:
            self._metrics["blocks_rejected"] += 1
            logger.warning(
                "BLOCK_PROPOSE rejected: %s",
                ", ".join(validation_errors),
            )
            msg_type = "BLOCK_NACK"
            nack_canonical = json.dumps(
                {"type": "BLOCK_NACK", "term": term,
                 "block_hash": block.block_hash,
                 "sentinel_id": self.config.sentinel_id},
                sort_keys=True, separators=(",", ":"),
            )
            nack_sig = self.sign_data(nack_canonical.encode()) if self.sign_data else ""
            msg_data = {
                "term": term,
                "block_hash": block.block_hash,
                "sentinel_id": self.config.sentinel_id,
                "reason": "invalid_block: " + ", ".join(validation_errors),
                "signature": nack_sig,
            }
        # Validate leader_id against known peer list before routing response
        # to prevent info disclosure via forged leader_id (SEC-14-H-07)
        claimed_leader_id = data.get("leader_id", "")
        if claimed_leader_id not in self.peer_ids:
            logger.warning(
                "BLOCK_PROPOSE from unknown leader_id %r — dropping ACK/NACK",
                claimed_leader_id,
            )
            return
        await self.send_message(claimed_leader_id, {
            "type": msg_type,
            "data": msg_data,
        })

    def _verify_ack_signature(self, ack: BlockAck) -> bool:
        if not ack.signature:
            return False
        if self.get_peer_pubkey is None:
            logger.warning("get_peer_pubkey not configured; cannot verify BLOCK_ACK signature")
            return False
        pubkey_bytes = self.get_peer_pubkey(ack.sentinel_id)
        if pubkey_bytes is None:
            logger.warning("No public key found for sentinel %s", ack.sentinel_id)
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.exceptions import InvalidSignature
            pub = Ed25519PublicKey.from_public_bytes(pubkey_bytes)
            canonical = json.dumps(
                {"type": "BLOCK_ACK", "term": ack.term,
                 "block_hash": ack.block_hash, "sentinel_id": ack.sentinel_id},
                sort_keys=True, separators=(",", ":")
            )
            sig_bytes = bytes.fromhex(ack.signature)
            pub.verify(sig_bytes, canonical.encode())
            return True
        except (ValueError, InvalidSignature) as e:
            logger.warning("BLOCK_ACK signature verification failed: %s", e)
            return False

    async def handle_block_ack(self, data: dict) -> None:
        ack = BlockAck(**data)
        if self.state != RaftState.LEADER or self._pending_block is None:
            return
        if ack.block_hash != self._pending_block.block_hash:
            return
        if not self._verify_ack_signature(ack):
            logger.warning("Rejected BLOCK_ACK from %s: invalid signature", ack.sentinel_id)
            return
        self._acks_received[ack.sentinel_id] = ack
        # Include self
        n_acks = len(self._acks_received) + 1
        if n_acks >= self.quorum():
            await self._commit_pending_block()

    async def handle_block_nack(self, data: dict) -> None:
        sentinel_id = data.get("sentinel_id")
        reason = data.get("reason")

        # Only accept NACKs from known peers.
        if sentinel_id not in self.peer_ids:
            logger.warning(
                "Dropping BLOCK_NACK from unknown sentinel %s", sentinel_id
            )
            return

        # Require a valid Ed25519 signature over the canonical NACK payload.
        if not self._verify_nack_signature(sentinel_id, data):
            logger.warning(
                "Dropping BLOCK_NACK from %s: invalid or missing signature", sentinel_id
            )
            return

        logger.warning("BLOCK_NACK from %s: %s", sentinel_id, reason)

        if self.state == RaftState.LEADER and self._pending_block is not None:
            logger.warning(
                "Aborting pending block proposal %s due to NACK from %s",
                self._pending_block.block_hash,
                sentinel_id,
            )
            self._pending_block = None
            self._acks_received = {}

    def _verify_nack_signature(self, sentinel_id: str, data: dict) -> bool:
        """Verify Ed25519 signature over a BLOCK_NACK payload (hex-encoded, same convention
        as BLOCK_ACK).  Returns False if no verification infrastructure is configured."""
        if self.get_peer_pubkey is None:
            # No verification infrastructure configured — reject all NACKs defensively.
            return False
        pubkey_bytes = self.get_peer_pubkey(sentinel_id)
        if pubkey_bytes is None:
            return False
        signature = data.get("signature", "")
        if not signature:
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.exceptions import InvalidSignature
            pub = Ed25519PublicKey.from_public_bytes(pubkey_bytes)
            canonical = json.dumps(
                {
                    "type": "BLOCK_NACK",
                    "term": data.get("term"),
                    "block_hash": data.get("block_hash"),
                    "sentinel_id": sentinel_id,
                },
                sort_keys=True, separators=(",", ":"),
            )
            sig_bytes = bytes.fromhex(signature)
            pub.verify(sig_bytes, canonical.encode())
            return True
        except (ValueError, InvalidSignature) as e:
            logger.warning("BLOCK_NACK signature verification failed for %s: %s", sentinel_id, e)
            return False

    async def _commit_pending_block(self) -> None:
        if self._pending_block is None:
            return
        block = self._pending_block
        block.signature = self.sign_block(block)
        self._pending_block = None
        self._acks_received = {}

        commit_msg = {
            "type": "COMMIT_BLOCK",
            "data": {"term": self.current_term, "block": block.to_dict()},
        }
        for peer_id in self.peer_ids:
            asyncio.create_task(self.send_message(peer_id, commit_msg))

        await self.on_commit(block)
        self.height = block.index
        self.last_block_hash = block.block_hash

    def _verify_block_signature(self, block) -> bool:
        """Verify the leader's Ed25519 signature on a committed block.

        The signature is over ``block.compute_hash()`` (hex digest), exactly as
        produced by ``sign_block`` on the leader side.  Returns False when
        verification infrastructure is not configured so callers can decide
        whether to accept or reject unverifiable blocks defensively.
        """
        if not block.signature:
            return False
        if self.get_peer_pubkey is None:
            logger.warning(
                "get_peer_pubkey not configured; cannot verify COMMIT_BLOCK signature"
            )
            return False
        pubkey_bytes = self.get_peer_pubkey(block.producer_id)
        if pubkey_bytes is None:
            logger.warning("No public key found for block producer %s", block.producer_id)
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.exceptions import InvalidSignature

            pub = Ed25519PublicKey.from_public_bytes(pubkey_bytes)
            sig_bytes = bytes.fromhex(block.signature)
            pub.verify(sig_bytes, block.compute_hash().encode())
            return True
        except (ValueError, InvalidSignature) as e:
            logger.warning("COMMIT_BLOCK signature verification failed: %s", e)
            return False

    async def handle_commit_block(self, data: dict) -> None:
        from .block import Block as BlockCls

        term = data["term"]
        block = BlockCls.from_dict(data["block"])
        if term >= self.current_term and block.index == self.height + 1:
            if not self._verify_block_signature(block):
                self._metrics["invalid_signatures"] += 1
                self._metrics["blocks_rejected"] += 1
                logger.warning(
                    "COMMIT_BLOCK rejected: invalid or missing signature from producer %s",
                    block.producer_id,
                )
                return
            self._metrics["blocks_accepted"] += 1
            await self.on_commit(block)
            self.height = block.index
            self.last_block_hash = block.block_hash
            self._last_heartbeat = time.monotonic()

    async def dispatch(self, msg: dict) -> None:
        """Dispatch an incoming Raft message to the appropriate handler."""
        msg_type = msg.get("type")
        data = msg.get("data", {})
        handlers = {
            "VOTE_REQUEST": self.handle_vote_request,
            "VOTE_RESPONSE": self.handle_vote_response,
            "PROPOSE_BLOCK": self.handle_propose_block,
            "BLOCK_ACK": self.handle_block_ack,
            "BLOCK_NACK": self.handle_block_nack,
            "COMMIT_BLOCK": self.handle_commit_block,
            "HEARTBEAT": self.handle_heartbeat,
        }
        handler = handlers.get(msg_type)
        if handler:
            await handler(data)
        else:
            logger.warning("Unknown Raft message type: %s", msg_type)
