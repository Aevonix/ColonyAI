"""ColonyChain — lightweight append-only identity and trust ledger.

Provides:
- Block model with SHA-256 hash chain and Ed25519 signatures
- Transaction types for colony lifecycle and governance
- SQLite-backed persistent chain storage
- Raft-lite consensus scaffolding (consensus.py — NOT wired; see docs/KNOWN-GAPS.md)
- Genesis admin capabilities
- Chain sync protocol messages
"""

from .block import Block, build_merkle_root, compute_block_hash
from .transactions import (
    TxType,
    Transaction,
    ChainState,
    ColonyRecord,
    KeyHistoryEntry,
    TrustEdge,
    SentinelRecord,
    ProtocolConfig,
    UntrustEvent,
)
from .storage import ChainStore
from .validation import TransactionValidator, BlockValidator, ValidationResult
from .genesis import create_genesis_block, GenesisConfig
from .manager import ChainManager

__all__ = [
    "Block",
    "build_merkle_root",
    "compute_block_hash",
    "TxType",
    "Transaction",
    "ChainState",
    "ColonyRecord",
    "KeyHistoryEntry",
    "TrustEdge",
    "SentinelRecord",
    "ProtocolConfig",
    "UntrustEvent",
    "ChainStore",
    "TransactionValidator",
    "BlockValidator",
    "ValidationResult",
    "create_genesis_block",
    "GenesisConfig",
    "ChainManager",
]
