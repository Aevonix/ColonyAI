"""Chain validation: block integrity, transaction signatures, state transitions.

All validation is deterministic — same state + same input = same result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.exceptions import InvalidSignature

from .block import Block, build_merkle_root
from .transactions import (
    ChainState,
    TxType,
    Transaction,
)

logger = logging.getLogger(__name__)

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]?$")


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""

    @property
    def valid(self) -> bool:
        """Alias for ok — used by nonce validator tests and external callers."""
        return self.ok

    @classmethod
    def success(cls) -> "ValidationResult":
        return cls(ok=True)

    @classmethod
    def fail(cls, reason: str) -> "ValidationResult":
        return cls(ok=False, reason=reason)


def _verify_ed25519(public_key_hex: str, data: bytes, signature_hex: str) -> bool:
    """Verify an Ed25519 signature. Returns False on any error."""
    try:
        pub_bytes = bytes.fromhex(public_key_hex)
        sig_bytes = bytes.fromhex(signature_hex)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        pub_key.verify(sig_bytes, data)
        return True
    except (InvalidSignature, ValueError, Exception):
        return False


class TransactionValidator:
    """Validates transactions against current chain state."""

    def __init__(self, state: ChainState = None, chain_store=None) -> None:
        self.state = state
        self._chain_store = chain_store

    def validate(self, tx: Transaction) -> ValidationResult:
        """Full validation: signature, nonce, type-specific preconditions."""
        # Basic structure
        if not tx.tx_id:
            return ValidationResult.fail("missing tx_id")
        if not tx.from_colony_id:
            return ValidationResult.fail("missing from_colony_id")

        # System transactions (auto-generated) skip signature check
        is_system = tx.from_colony_id == "system"

        if not is_system:
            nonce_result = self._check_nonce(tx)
            if not nonce_result.ok:
                return nonce_result

            sig_result = self._verify_signature(tx)
            if not sig_result.ok:
                return sig_result

        # Type-specific validation
        return self._validate_by_type(tx)

    def _verify_signature(self, tx: Transaction) -> ValidationResult:
        """Verify Ed25519 signature against the colony's active key."""
        colony = self.state.colony_registry.get(tx.from_colony_id)

        # For colony_register, use the public key from the payload itself
        if tx.type == TxType.COLONY_REGISTER:
            pub_key_hex = tx.payload.get("public_key_hex", "")
        elif colony is not None:
            pub_key_hex = colony.active_public_key_hex
        else:
            return ValidationResult.fail(
                f"colony {tx.from_colony_id!r} not registered"
            )

        if not pub_key_hex:
            return ValidationResult.fail("no public key for colony")

        data = tx.signing_bytes()
        if not _verify_ed25519(pub_key_hex, data, tx.signature):
            return ValidationResult.fail("invalid signature")
        return ValidationResult.success()

    def _check_nonce(self, tx: Transaction) -> ValidationResult:
        """Verify nonce is strictly greater than colony's last accepted nonce.

        The authoritative nonce check is enforced by ChainManager.submit_transaction()
        via ChainStore.get_nonce() + DB UNIQUE(colony_id, nonce). This method is a
        defence-in-depth layer for callers that use TransactionValidator directly
        (e.g. federation sync, offline verification tools).

        If no ChainStore is injected the check is skipped and a warning is logged
        so that misconfigured callers are visible in logs.
        """
        if tx.type == TxType.COLONY_REGISTER:
            # Genesis registrations have no prior nonce to compare against.
            return ValidationResult.success()

        if self._chain_store is None:
            logger.warning(
                "TransactionValidator: chain_store not injected; skipping nonce check "
                "for tx %s from %s. Inject chain_store= to enable replay protection.",
                tx.tx_id,
                tx.from_colony_id,
            )
            return ValidationResult.success()

        last_nonce = self._chain_store.get_nonce(tx.from_colony_id)
        if tx.nonce <= last_nonce:
            return ValidationResult.fail(
                f"Nonce replay: tx.nonce={tx.nonce} <= last_accepted={last_nonce}"
            )
        return ValidationResult.success()

    def _validate_by_type(self, tx: Transaction) -> ValidationResult:
        dispatch = {
            TxType.COLONY_REGISTER: self._validate_colony_register,
            TxType.COLONY_ROTATE_KEY: self._validate_colony_rotate_key,
            TxType.COLONY_REVOKE_KEY: self._validate_colony_revoke_key,
            TxType.COLONY_RELEASE_NAME: self._validate_colony_release_name,
            TxType.TRUST_ATTEST: self._validate_trust_attest,
            TxType.UNTRUST_ATTEST: self._validate_untrust_attest,
            TxType.SENTINEL_REGISTER: self._validate_sentinel_register,
            TxType.SENTINEL_DEREGISTER: self._validate_sentinel_deregister,
            TxType.COLONY_SUSPEND: self._validate_colony_suspend,
            TxType.COLONY_REINSTATE: self._validate_colony_reinstate,
            TxType.PROTOCOL_UPGRADE: self._validate_protocol_upgrade,
        }
        handler = dispatch.get(tx.type)
        if handler is None:
            return ValidationResult.fail(f"unknown transaction type: {tx.type}")
        return handler(tx)

    def _validate_colony_register(self, tx: Transaction) -> ValidationResult:
        p = tx.payload
        name = p.get("name", "").lower()
        colony_id = p.get("colony_id", "")
        public_key_hex = p.get("public_key_hex", "")

        if not name:
            return ValidationResult.fail("colony_register: missing name")
        if not _NAME_PATTERN.match(name):
            return ValidationResult.fail(
                f"colony_register: name {name!r} does not match required pattern"
            )
        if name in self.state.name_registry:
            return ValidationResult.fail(
                f"colony_register: name {name!r} already registered"
            )
        if colony_id in self.state.colony_registry:
            return ValidationResult.fail(
                f"colony_register: colony_id {colony_id!r} already registered"
            )
        # Verify colony_id = sha256(pubkey)
        try:
            expected_id = hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()
        except ValueError:
            return ValidationResult.fail("colony_register: invalid public_key_hex")
        if expected_id != colony_id:
            return ValidationResult.fail(
                "colony_register: colony_id does not match sha256(public_key)"
            )
        # Genesis admin can only appear once (in genesis block)
        if p.get("genesis_admin", False) and self.state.genesis_admin_id:
            return ValidationResult.fail(
                "colony_register: genesis_admin=true only allowed in genesis block"
            )
        return ValidationResult.success()

    def _validate_colony_rotate_key(self, tx: Transaction) -> ValidationResult:
        colony = self.state.colony_registry.get(tx.from_colony_id)
        if colony is None:
            return ValidationResult.fail("colony_rotate_key: colony not registered")
        if colony.status == "suspended":
            return ValidationResult.fail("colony_rotate_key: colony is suspended")
        p = tx.payload
        new_key = p.get("new_public_key_hex", "")
        if not new_key:
            return ValidationResult.fail("colony_rotate_key: missing new_public_key_hex")
        # New key must not be registered to another colony
        for cid, record in self.state.colony_registry.items():
            if cid != tx.from_colony_id and record.active_public_key_hex == new_key:
                return ValidationResult.fail(
                    "colony_rotate_key: new key already registered to another colony"
                )
        return ValidationResult.success()

    def _validate_colony_revoke_key(self, tx: Transaction) -> ValidationResult:
        p = tx.payload
        revoked_key = p.get("revoked_key_hex", "")
        if not revoked_key:
            return ValidationResult.fail("colony_revoke_key: missing revoked_key_hex")
        # Key must be in history of the target colony
        # from_colony_id is either the colony itself, or "system"/"genesis"
        target_id = tx.from_colony_id
        history = self.state.key_history.get(target_id, [])
        if not any(e.public_key_hex == revoked_key for e in history):
            return ValidationResult.fail(
                "colony_revoke_key: key not found in colony key history"
            )
        return ValidationResult.success()

    def _validate_colony_release_name(self, tx: Transaction) -> ValidationResult:
        p = tx.payload
        name = p.get("name", "").lower()
        owner_id = self.state.name_registry.get(name)
        if owner_id is None:
            return ValidationResult.fail(
                f"colony_release_name: name {name!r} not registered"
            )
        is_genesis = tx.from_colony_id == self.state.genesis_admin_id
        if owner_id != tx.from_colony_id and not is_genesis:
            return ValidationResult.fail(
                "colony_release_name: not authorized (not owner or genesis admin)"
            )
        return ValidationResult.success()

    def _validate_trust_attest(self, tx: Transaction) -> ValidationResult:
        p = tx.payload
        target_id = p.get("target_colony_id", "")
        if not self.state.is_active(tx.from_colony_id):
            return ValidationResult.fail("trust_attest: attesting colony not active")
        if not self.state.is_active(target_id):
            return ValidationResult.fail("trust_attest: target colony not active")
        trust_level = p.get("trust_level", -1)
        if trust_level not in range(5):
            return ValidationResult.fail("trust_attest: trust_level must be 0-4")
        return ValidationResult.success()

    def _validate_untrust_attest(self, tx: Transaction) -> ValidationResult:
        p = tx.payload
        target_id = p.get("target_colony_id", "")
        if not self.state.colony_registry.get(tx.from_colony_id):
            return ValidationResult.fail("untrust_attest: attesting colony not registered")
        if not self.state.colony_registry.get(target_id):
            return ValidationResult.fail("untrust_attest: target colony not registered")
        return ValidationResult.success()

    def _validate_sentinel_register(self, tx: Transaction) -> ValidationResult:
        colony = self.state.colony_registry.get(tx.from_colony_id)
        if colony is None:
            return ValidationResult.fail("sentinel_register: colony not registered")
        if colony.status == "suspended":
            return ValidationResult.fail("sentinel_register: colony is suspended")
        p = tx.payload
        sentinel_id = p.get("sentinel_id", "")
        if not sentinel_id:
            return ValidationResult.fail("sentinel_register: missing sentinel_id")
        if sentinel_id in self.state.sentinel_roster:
            return ValidationResult.fail(
                f"sentinel_register: sentinel_id {sentinel_id!r} already registered"
            )
        return ValidationResult.success()

    def _validate_sentinel_deregister(self, tx: Transaction) -> ValidationResult:
        p = tx.payload
        sentinel_id = p.get("sentinel_id", "")
        record = self.state.sentinel_roster.get(sentinel_id)
        if record is None:
            return ValidationResult.fail(
                f"sentinel_deregister: sentinel {sentinel_id!r} not in roster"
            )
        is_genesis = tx.from_colony_id == self.state.genesis_admin_id
        is_system = tx.from_colony_id == "system"
        if record.colony_id != tx.from_colony_id and not is_genesis and not is_system:
            return ValidationResult.fail(
                "sentinel_deregister: not authorized (not owner or genesis admin)"
            )
        return ValidationResult.success()

    def _validate_colony_suspend(self, tx: Transaction) -> ValidationResult:
        p = tx.payload
        target_id = p.get("target_colony_id", "")
        if not target_id:
            return ValidationResult.fail("colony_suspend: missing target_colony_id")
        if target_id not in self.state.colony_registry:
            return ValidationResult.fail("colony_suspend: target colony not registered")
        if self.state.is_suspended(target_id):
            return ValidationResult.fail("colony_suspend: colony already suspended")
        is_genesis = tx.from_colony_id == self.state.genesis_admin_id
        is_system = tx.from_colony_id == "system"
        if not is_genesis and not is_system:
            return ValidationResult.fail(
                "colony_suspend: only genesis admin or system may suspend"
            )
        return ValidationResult.success()

    def _validate_colony_reinstate(self, tx: Transaction) -> ValidationResult:
        p = tx.payload
        target_id = p.get("target_colony_id", "")
        if not target_id:
            return ValidationResult.fail("colony_reinstate: missing target_colony_id")
        if not self.state.is_suspended(target_id):
            return ValidationResult.fail("colony_reinstate: colony is not suspended")
        is_genesis = tx.from_colony_id == self.state.genesis_admin_id
        is_system = tx.from_colony_id == "system"
        if not is_genesis and not is_system:
            return ValidationResult.fail(
                "colony_reinstate: only genesis admin or system may reinstate"
            )
        return ValidationResult.success()

    def _validate_protocol_upgrade(self, tx: Transaction) -> ValidationResult:
        is_genesis = tx.from_colony_id == self.state.genesis_admin_id
        if not is_genesis:
            return ValidationResult.fail(
                "protocol_upgrade: only genesis admin may propose upgrades"
            )
        p = tx.payload
        if not p.get("upgrade_id"):
            return ValidationResult.fail("protocol_upgrade: missing upgrade_id")
        if not p.get("title"):
            return ValidationResult.fail("protocol_upgrade: missing title")
        activation_height = p.get("activation_height", 0)
        if activation_height <= self.state.height:
            return ValidationResult.fail(
                "protocol_upgrade: activation_height must be in the future"
            )
        return ValidationResult.success()


class BlockValidator:
    """Validates block structure, hash chain integrity, and merkle root."""

    def validate_block(
        self, block: Block, previous_block: Block | None
    ) -> ValidationResult:
        # Hash chain
        if previous_block is None:
            if block.index != 0:
                return ValidationResult.fail("first block must have index 0")
            if block.previous_hash != "0" * 64:
                return ValidationResult.fail(
                    "genesis block previous_hash must be 64 zeros"
                )
        else:
            if block.index != previous_block.index + 1:
                return ValidationResult.fail(
                    f"expected index {previous_block.index + 1}, got {block.index}"
                )
            if block.previous_hash != previous_block.block_hash:
                return ValidationResult.fail("previous_hash mismatch")

        # Merkle root
        tx_ids = [tx.tx_id for tx in block.transactions]
        expected_merkle = build_merkle_root(tx_ids)
        if block.merkle_root != expected_merkle:
            return ValidationResult.fail(
                f"merkle_root mismatch: {block.merkle_root!r} != {expected_merkle!r}"
            )

        return ValidationResult.success()

    def validate_sentinel_signature(
        self,
        block: Block,
        sentinel_public_key_hex: str,
    ) -> ValidationResult:
        """Verify the block's Ed25519 signature from the producing Sentinel."""
        block_dict = block.to_dict()
        block_dict.pop("signature", None)
        canonical = json.dumps(block_dict, sort_keys=True, separators=(",", ":"))
        data = canonical.encode()
        if not _verify_ed25519(sentinel_public_key_hex, data, block.signature):
            return ValidationResult.fail("block signature invalid")
        return ValidationResult.success()
