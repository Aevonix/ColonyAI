"""Protocol message types for ColonyChain inter-node communication.

Defines the wire format for chain sync, block announcement,
admin commands, and network broadcasts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ChainMsgType(str, Enum):
    """Chain protocol message types."""
    CHAIN_SYNC = "CHAIN_SYNC"               # Request blocks from a range
    CHAIN_SYNC_RESPONSE = "CHAIN_SYNC_RESPONSE"  # Response carrying blocks
    CHAIN_ANNOUNCE = "CHAIN_ANNOUNCE"       # Broadcast new block
    ADMIN_COMMAND = "ADMIN_COMMAND"         # Genesis admin messages
    BROADCAST = "BROADCAST"                 # Network-wide announcements
    CHAIN_STATE_REQUEST = "CHAIN_STATE_REQUEST"   # Request current chain state
    CHAIN_STATE_RESPONSE = "CHAIN_STATE_RESPONSE" # Response with chain state summary
    TX_SUBMIT = "TX_SUBMIT"                 # Submit transaction to mempool
    TX_SUBMIT_RESPONSE = "TX_SUBMIT_RESPONSE"    # Ack/nack for tx submission


@dataclass
class ChainSyncRequest:
    """Request blocks from a range [from_index, to_index]."""
    from_index: int
    to_index: int
    requester_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": ChainMsgType.CHAIN_SYNC.value,
            "from_index": self.from_index,
            "to_index": self.to_index,
            "requester_id": self.requester_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChainSyncRequest":
        return cls(
            from_index=d["from_index"],
            to_index=d["to_index"],
            requester_id=d.get("requester_id", ""),
        )


@dataclass
class ChainSyncResponse:
    """Response carrying a list of serialized blocks."""
    blocks: list[dict[str, Any]]
    from_index: int
    to_index: int
    responder_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": ChainMsgType.CHAIN_SYNC_RESPONSE.value,
            "blocks": self.blocks,
            "from_index": self.from_index,
            "to_index": self.to_index,
            "responder_id": self.responder_id,
        }


@dataclass
class ChainAnnouncement:
    """Broadcast that a new block has been committed."""
    block_index: int
    block_hash: str
    producer_id: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    tx_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": ChainMsgType.CHAIN_ANNOUNCE.value,
            "block_index": self.block_index,
            "block_hash": self.block_hash,
            "producer_id": self.producer_id,
            "timestamp": self.timestamp,
            "tx_count": self.tx_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChainAnnouncement":
        return cls(
            block_index=d["block_index"],
            block_hash=d["block_hash"],
            producer_id=d["producer_id"],
            timestamp=d.get("timestamp", ""),
            tx_count=d.get("tx_count", 0),
        )


@dataclass
class AdminCommand:
    """Genesis admin command message (signed)."""
    command_type: str          # "suspend" | "reinstate" | "sentinel_appoint" | etc.
    target_id: str
    genesis_colony_id: str
    signed_tx_json: str        # JSON of the signed Transaction
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": ChainMsgType.ADMIN_COMMAND.value,
            "command_type": self.command_type,
            "target_id": self.target_id,
            "genesis_colony_id": self.genesis_colony_id,
            "signed_tx_json": self.signed_tx_json,
            "timestamp": self.timestamp,
        }


@dataclass
class BroadcastMessage:
    """Network-wide announcement from Genesis admin."""
    sender_id: str
    message: str
    signed_tx_json: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": ChainMsgType.BROADCAST.value,
            "sender_id": self.sender_id,
            "message": self.message,
            "signed_tx_json": self.signed_tx_json,
            "timestamp": self.timestamp,
        }


@dataclass
class ChainStateRequest:
    """Request a summary of the current chain state from a Sentinel."""
    requester_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": ChainMsgType.CHAIN_STATE_REQUEST.value,
            "requester_id": self.requester_id,
        }


@dataclass
class ChainStateResponse:
    """Summary of current chain state."""
    height: int
    last_block_hash: str
    n_colonies: int
    n_sentinels: int
    n_suspended: int
    network_id: str
    responder_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": ChainMsgType.CHAIN_STATE_RESPONSE.value,
            "height": self.height,
            "last_block_hash": self.last_block_hash,
            "n_colonies": self.n_colonies,
            "n_sentinels": self.n_sentinels,
            "n_suspended": self.n_suspended,
            "network_id": self.network_id,
            "responder_id": self.responder_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChainStateResponse":
        return cls(
            height=d["height"],
            last_block_hash=d["last_block_hash"],
            n_colonies=d["n_colonies"],
            n_sentinels=d["n_sentinels"],
            n_suspended=d["n_suspended"],
            network_id=d.get("network_id", ""),
            responder_id=d.get("responder_id", ""),
        )


@dataclass
class TxSubmitRequest:
    """Submit a signed transaction to a Sentinel's mempool."""
    tx_json: str
    submitter_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": ChainMsgType.TX_SUBMIT.value,
            "tx_json": self.tx_json,
            "submitter_id": self.submitter_id,
        }


@dataclass
class TxSubmitResponse:
    """Acknowledgment or rejection of a submitted transaction."""
    tx_id: str
    accepted: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": ChainMsgType.TX_SUBMIT_RESPONSE.value,
            "tx_id": self.tx_id,
            "accepted": self.accepted,
            "reason": self.reason,
        }
