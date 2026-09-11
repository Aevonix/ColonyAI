"""Block model for ColonyChain.

SHA-256 hash chain with Ed25519 validator signatures and Merkle root
computation over transactions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .transactions import Transaction


def build_merkle_root(tx_ids: list[str]) -> str:
    """Compute SHA-256 Merkle root over a list of transaction IDs."""
    if not tx_ids:
        return hashlib.sha256(b"").hexdigest()
    level: list[str] = [hashlib.sha256(tid.encode()).hexdigest() for tid in tx_ids]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [
            hashlib.sha256(
                bytes.fromhex(level[i]) + bytes.fromhex(level[i + 1])
            ).hexdigest()
            for i in range(0, len(level), 2)
        ]
    return level[0]


def compute_block_hash(block_dict: dict[str, Any]) -> str:
    """Compute SHA-256 hash of a block dict (excluding 'signature' field)."""
    fields = {k: v for k, v in block_dict.items() if k != "signature"}
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class Block:
    """A single block in the ColonyChain."""

    index: int
    previous_hash: str
    timestamp: str
    transactions: list["Transaction"]
    merkle_root: str
    producer_id: str
    signature: str
    metadata: dict[str, Any] = field(default_factory=dict)

    # Computed and cached after creation
    _hash: str = field(default="", repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "previous_hash": self.previous_hash,
            "timestamp": self.timestamp,
            "transactions": [t.to_dict() for t in self.transactions],
            "merkle_root": self.merkle_root,
            "producer_id": self.producer_id,
            "signature": self.signature,
            "metadata": self.metadata,
        }

    def compute_hash(self) -> str:
        """Compute SHA-256 hash of this block (excluding signature)."""
        d = self.to_dict()
        return compute_block_hash(d)

    @property
    def block_hash(self) -> str:
        if not self._hash:
            self._hash = self.compute_hash()
        return self._hash

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Block":
        from .transactions import Transaction  # avoid circular at module load

        txs = [Transaction.from_dict(t) for t in d.get("transactions", [])]
        block = cls(
            index=d["index"],
            previous_hash=d["previous_hash"],
            timestamp=d["timestamp"],
            transactions=txs,
            merkle_root=d["merkle_root"],
            producer_id=d["producer_id"],
            signature=d.get("signature", ""),
            metadata=d.get("metadata", {}),
        )
        return block

    def __post_init__(self) -> None:
        # Reset cached hash if fields change
        self._hash = ""
