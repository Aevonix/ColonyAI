"""Genesis block creation, Genesis admin capabilities, and single-chain enforcement.

The Genesis admin is a runtime claim — whoever submits the genesis block
becomes admin. Admin identity is tracked on-chain and transferable.
All admin actions are recorded on-chain and fully auditable.

Single-chain enforcement:
- Every block carries a network_id derived from the genesis block.
- Blocks from other chains (different network_id) are rejected.
- Genesis block creation is only offered when no existing network is found.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .block import Block, build_merkle_root
from .transactions import Transaction, TxType

logger = logging.getLogger(__name__)


@dataclass
class GenesisConfig:
    """Configuration for genesis block creation."""

    network_name: str = "colony-federation"
    genesis_colony_name: str = "genesis"
    genesis_colony_id: str = ""         # sha256(genesis_pubkey)
    genesis_pubkey_hex: str = ""        # 32-byte Ed25519 pubkey, hex
    genesis_endpoint: str = ""
    genesis_description: str = "Genesis colony — network founder"
    block_interval_secs: int = 30
    untrust_threshold: int = 3
    untrust_window_days: int = 30
    uptime_requirement_percent: float = 99.0
    min_sentinels: int = 1
    min_protocol_version: str = "1.0.0"


def _derive_colony_id(public_key_hex: str) -> str:
    """Compute colony_id = sha256(public_key_bytes)."""
    return hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()


def create_genesis_block(
    config: GenesisConfig,
    sign_fn: Callable[[bytes], str],
    producer_id: str = "genesis-sentinel",
) -> Block:
    """Create the genesis block with the founding colony's registration.

    Args:
        config: Genesis configuration including the admin's public key.
        sign_fn: Callable that signs bytes and returns hex signature.
        producer_id: Sentinel ID producing the genesis block.

    Returns:
        Fully constructed and signed genesis Block.
    """
    ts = datetime.now(timezone.utc).isoformat()

    # Derive colony_id if not provided
    if not config.genesis_colony_id and config.genesis_pubkey_hex:
        config.genesis_colony_id = _derive_colony_id(config.genesis_pubkey_hex)

    # Build the genesis colony_register transaction
    register_payload = {
        "name": config.genesis_colony_name,
        "public_key_hex": config.genesis_pubkey_hex,
        "colony_id": config.genesis_colony_id,
        "endpoint": config.genesis_endpoint,
        "description": config.genesis_description,
        "protocol_version": config.min_protocol_version,
        "capabilities": ["task_delegation", "memory_sharing", "sentinel_relay"],
        "genesis_admin": True,
        "metadata": {},
    }

    register_tx = Transaction.create(
        tx_type=TxType.COLONY_REGISTER,
        from_colony_id=config.genesis_colony_id,
        nonce=1,
        payload=register_payload,
        sign_fn=sign_fn,
    )

    transactions = [register_tx]
    tx_ids = [t.tx_id for t in transactions]
    merkle_root = build_merkle_root(tx_ids)

    # Compute network_id from genesis pubkey + name + timestamp
    network_id_input = (
        config.genesis_pubkey_hex + config.network_name + ts
    ).encode()
    network_id = hashlib.sha256(network_id_input).hexdigest()

    metadata: dict[str, Any] = {
        "network_name": config.network_name,
        "network_id": network_id,
        "genesis_colony_id": config.genesis_colony_id,
        "genesis_pubkey_hex": config.genesis_pubkey_hex,
        "initial_config": {
            "block_interval_secs": config.block_interval_secs,
            "untrust_threshold": config.untrust_threshold,
            "untrust_window_days": config.untrust_window_days,
            "uptime_requirement_percent": config.uptime_requirement_percent,
            "min_sentinels": config.min_sentinels,
            "min_protocol_version": config.min_protocol_version,
        },
        "created_at": ts,
    }

    block = Block(
        index=0,
        previous_hash="0" * 64,
        timestamp=ts,
        transactions=transactions,
        merkle_root=merkle_root,
        producer_id=producer_id,
        signature="",
        metadata=metadata,
    )

    # Sign the block (excluding the signature field)
    block_dict = block.to_dict()
    block_dict.pop("signature", None)
    canonical = json.dumps(block_dict, sort_keys=True, separators=(",", ":"))
    block.signature = sign_fn(canonical.encode())
    block._hash = ""  # reset cached hash

    return block


class GenesisAdmin:
    """Genesis admin capability layer.

    Provides helper methods for Genesis admin actions. Each action creates
    and returns a Transaction that must be submitted to the chain.
    All actions are recorded on-chain and fully auditable.
    """

    def __init__(
        self,
        genesis_colony_id: str,
        sign_fn: Callable[[bytes], str],
        get_nonce: Callable[[str], int],
    ) -> None:
        self.genesis_colony_id = genesis_colony_id
        self.sign_fn = sign_fn
        self.get_nonce = get_nonce

    def _next_nonce(self) -> int:
        return self.get_nonce(self.genesis_colony_id) + 1

    def suspend_colony(
        self, target_colony_id: str, reason: str = "admin_action"
    ) -> Transaction:
        """Create a colony_suspend transaction as Genesis admin."""
        return Transaction.create(
            tx_type=TxType.COLONY_SUSPEND,
            from_colony_id=self.genesis_colony_id,
            nonce=self._next_nonce(),
            payload={
                "target_colony_id": target_colony_id,
                "reason": reason,
                "untrust_evidence_tx_ids": [],
                "effective_height": 0,
                "review_at_height": 0,
            },
            sign_fn=self.sign_fn,
        )

    def reinstate_colony(
        self,
        target_colony_id: str,
        conditions: str = "",
        colony_acknowledgment: str = "",
    ) -> Transaction:
        """Create a colony_reinstate transaction as Genesis admin."""
        return Transaction.create(
            tx_type=TxType.COLONY_REINSTATE,
            from_colony_id=self.genesis_colony_id,
            nonce=self._next_nonce(),
            payload={
                "target_colony_id": target_colony_id,
                "reinstated_by": "genesis",
                "conditions": conditions,
                "colony_acknowledgment": colony_acknowledgment,
            },
            sign_fn=self.sign_fn,
        )

    def appoint_sentinel(
        self,
        sentinel_id: str,
        colony_id: str,
        host: str,
        port: int,
        public_key_hex: str,
    ) -> Transaction:
        """Appoint a Sentinel directly (genesis bootstrap, bypasses vote)."""
        return Transaction.create(
            tx_type=TxType.SENTINEL_REGISTER,
            from_colony_id=self.genesis_colony_id,
            nonce=self._next_nonce(),
            payload={
                "sentinel_id": sentinel_id,
                "colony_id": colony_id,
                "host": host,
                "port": port,
                "public_key_hex": public_key_hex,
                "uptime_proof": {
                    "period_days": 30,
                    "uptime_percent": 100.0,
                    "attestation_tx_ids": [],
                },
                "approver_signatures": [],
                "genesis_approved": True,
            },
            sign_fn=self.sign_fn,
        )

    def remove_sentinel(
        self, sentinel_id: str, reason: str = "admin_action"
    ) -> Transaction:
        """Forcibly remove a Sentinel (policy violation)."""
        return Transaction.create(
            tx_type=TxType.SENTINEL_DEREGISTER,
            from_colony_id=self.genesis_colony_id,
            nonce=self._next_nonce(),
            payload={
                "sentinel_id": sentinel_id,
                "reason": reason,
                "successor_sentinel_id": None,
            },
            sign_fn=self.sign_fn,
        )

    def force_release_name(self, name: str) -> Transaction:
        """Force-release a squatted name."""
        return Transaction.create(
            tx_type=TxType.COLONY_RELEASE_NAME,
            from_colony_id=self.genesis_colony_id,
            nonce=self._next_nonce(),
            payload={"name": name, "reason": "admin_force_release"},
            sign_fn=self.sign_fn,
        )

    def propose_protocol_upgrade(
        self,
        title: str,
        description: str,
        changes: dict[str, Any],
        activation_height: int,
        sentinel_votes: list[dict] | None = None,
    ) -> Transaction:
        """Propose a protocol upgrade. Sentinels vote via signed payloads."""
        return Transaction.create(
            tx_type=TxType.PROTOCOL_UPGRADE,
            from_colony_id=self.genesis_colony_id,
            nonce=self._next_nonce(),
            payload={
                "upgrade_id": str(uuid.uuid4()),
                "title": title[:128],
                "description": description[:4096],
                "spec_url": "",
                "changes": changes,
                "activation_height": activation_height,
                "votes": sentinel_votes or [],
            },
            sign_fn=self.sign_fn,
        )

    def broadcast_announcement(self, message: str) -> Transaction:
        """Submit a network-wide broadcast announcement via a no-op protocol_upgrade."""
        return Transaction.create(
            tx_type=TxType.PROTOCOL_UPGRADE,
            from_colony_id=self.genesis_colony_id,
            nonce=self._next_nonce(),
            payload={
                "upgrade_id": str(uuid.uuid4()),
                "title": "broadcast",
                "description": message[:4096],
                "spec_url": "",
                "changes": {},
                "activation_height": 999_999_999,  # never activates
                "votes": [],
                "broadcast": True,
            },
            sign_fn=self.sign_fn,
        )

    def vote_strip_genesis_admin(self, sentinel_id: str) -> dict[str, Any]:
        """Return a signed vote dict for unanimous Sentinel strip of Genesis admin.

        This is NOT a standalone transaction — it produces a vote entry that must
        be aggregated with all other Sentinel votes before submitting the
        strip_genesis_admin protocol_upgrade transaction.

        The caller submits:
            GenesisAdmin.build_strip_genesis_admin(votes)
        once all Sentinels have voted.
        """
        payload = {
            "sentinel_id": sentinel_id,
            "action": "strip_genesis_admin",
            "vote": "yes",
        }
        import json as _json
        canonical = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
        signature = self.sign_fn(canonical.encode())
        return {"sentinel_id": sentinel_id, "vote": "yes", "signature": signature}

    def build_strip_genesis_admin(self, sentinel_votes: list[dict]) -> Transaction:
        """Build the co-signed strip_genesis_admin protocol_upgrade transaction.

        Args:
            sentinel_votes: List of vote dicts from vote_strip_genesis_admin(),
                one per active Sentinel. All must be "yes" for the state machine
                to accept the strip.

        Returns:
            A PROTOCOL_UPGRADE transaction with title="strip_genesis_admin".
        """
        return Transaction.create(
            tx_type=TxType.PROTOCOL_UPGRADE,
            from_colony_id=self.genesis_colony_id,
            nonce=self._next_nonce(),
            payload={
                "upgrade_id": str(uuid.uuid4()),
                "title": "strip_genesis_admin",
                "description": "Unanimous Sentinel vote to strip Genesis admin",
                "spec_url": "",
                "changes": {},
                "activation_height": 0,
                "votes": sentinel_votes,
            },
            sign_fn=self.sign_fn,
        )

    def transfer_admin(
        self, new_admin_colony_id: str, new_admin_pubkey_hex: str
    ) -> Transaction:
        """Transfer Genesis admin role to another colony.

        Records a protocol_upgrade that notes the new admin.
        The actual transfer is enforced by chain state machine when processed.
        """
        return Transaction.create(
            tx_type=TxType.PROTOCOL_UPGRADE,
            from_colony_id=self.genesis_colony_id,
            nonce=self._next_nonce(),
            payload={
                "upgrade_id": str(uuid.uuid4()),
                "title": "transfer_genesis_admin",
                "description": f"Transfer genesis admin to {new_admin_colony_id}",
                "spec_url": "",
                "changes": {
                    "new_genesis_admin_id": new_admin_colony_id,
                    "new_genesis_admin_pubkey": new_admin_pubkey_hex,
                },
                "activation_height": 0,
                "votes": [],
            },
            sign_fn=self.sign_fn,
        )


# ---------------------------------------------------------------------------
# Single-chain enforcement
# ---------------------------------------------------------------------------


class ChainForkError(Exception):
    """Raised when a block from a different chain (different network_id) is received."""


def validate_chain_origin(
    local_network_id: str,
    received_block: Block,
    sender_node_id: Optional[str] = None,
) -> None:
    """Validate that a received block belongs to the local network.

    Requirements:
    - MUST reject blocks with network_id != local_network_id.
    - MUST reject genesis blocks with index=0 if a genesis block already exists
      (caller must check this condition before calling).
    - MUST log rejected blocks with sender_node_id for audit.

    Raises:
        ChainForkError: If the block belongs to a different network.
    """
    received_network_id = received_block.metadata.get("network_id")
    if received_network_id != local_network_id:
        logger.warning(
            "CHAIN FORK REJECTED: block network_id=%r does not match local=%r sender=%s",
            received_network_id,
            local_network_id,
            sender_node_id or "unknown",
        )
        raise ChainForkError(
            f"Block network_id {received_network_id!r} does not match "
            f"local network_id {local_network_id!r}. "
            "This block is from a different chain and has been rejected."
        )


def should_offer_genesis_creation(sentinels_found: list) -> bool:
    """Return True only when zero Sentinels were found during discovery.

    This is the first-install detection gate. Genesis block creation is ONLY
    offered once, ever, when no existing network is present.

    Args:
        sentinels_found: List of Sentinel addresses discovered during the
            mDNS / SWIM / relay discovery phase.

    Returns:
        True if no existing network was found and genesis creation is appropriate.
    """
    return len(sentinels_found) == 0


def extract_network_id(genesis_block: Block) -> str:
    """Extract and validate the network_id from a genesis block.

    Raises:
        ValueError: If the genesis block has no network_id in its metadata.
    """
    network_id = genesis_block.metadata.get("network_id")
    if not network_id:
        raise ValueError(
            "Genesis block has no network_id in metadata — cannot join this chain."
        )
    return network_id
