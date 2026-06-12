"""Transaction types and chain state dataclasses for ColonyChain.

Each transaction type is fully typed. The ChainState dataclass holds the
complete materialized state derived from replaying all committed blocks.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Transaction type enum
# ---------------------------------------------------------------------------

class TxType(str, Enum):
    COLONY_REGISTER = "colony_register"
    COLONY_ROTATE_KEY = "colony_rotate_key"
    COLONY_REVOKE_KEY = "colony_revoke_key"
    COLONY_RELEASE_NAME = "colony_release_name"
    TRUST_ATTEST = "trust_attest"
    UNTRUST_ATTEST = "untrust_attest"
    SENTINEL_REGISTER = "sentinel_register"
    SENTINEL_DEREGISTER = "sentinel_deregister"
    COLONY_SUSPEND = "colony_suspend"
    COLONY_REINSTATE = "colony_reinstate"
    PROTOCOL_UPGRADE = "protocol_upgrade"

    # Plugin security extensions
    PLUGIN_PUBLISH = "plugin_publish"
    PLUGIN_ATTESTATION = "plugin_attestation"
    PLUGIN_FLAG = "plugin_flag"
    PLUGIN_QUARANTINE = "plugin_quarantine"  # system-generated only


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------

@dataclass
class Transaction:
    """A single chain transaction with Ed25519 signature."""

    tx_id: str
    type: TxType
    from_colony_id: str
    timestamp: str
    nonce: int
    payload: dict[str, Any]
    signature: str  # Ed25519 hex

    @classmethod
    def create(
        cls,
        tx_type: TxType,
        from_colony_id: str,
        nonce: int,
        payload: dict[str, Any],
        sign_fn: Callable[[bytes], str],
    ) -> "Transaction":
        """Create and sign a transaction."""
        tx_id = str(uuid.uuid4())
        ts = datetime.now(timezone.utc).isoformat()
        signing_payload = {
            "tx_id": tx_id,
            "type": tx_type.value,
            "from_colony_id": from_colony_id,
            "timestamp": ts,
            "nonce": nonce,
            "payload": payload,
        }
        canonical = json.dumps(signing_payload, sort_keys=True, separators=(",", ":"))
        sig = sign_fn(canonical.encode())
        return cls(
            tx_id=tx_id,
            type=tx_type,
            from_colony_id=from_colony_id,
            timestamp=ts,
            nonce=nonce,
            payload=payload,
            signature=sig,
        )

    def signing_bytes(self) -> bytes:
        """Return the canonical bytes that were/should be signed."""
        payload = {
            "tx_id": self.tx_id,
            "type": self.type.value if isinstance(self.type, TxType) else self.type,
            "from_colony_id": self.from_colony_id,
            "timestamp": self.timestamp,
            "nonce": self.nonce,
            "payload": self.payload,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def to_dict(self) -> dict[str, Any]:
        return {
            "tx_id": self.tx_id,
            "type": self.type.value if isinstance(self.type, TxType) else self.type,
            "from_colony_id": self.from_colony_id,
            "timestamp": self.timestamp,
            "nonce": self.nonce,
            "payload": self.payload,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Transaction":
        try:
            tx_type: Any = TxType(d["type"])
        except (ValueError, KeyError):
            tx_type = d.get("type", "unknown")
        return cls(
            tx_id=d["tx_id"],
            type=tx_type,
            from_colony_id=d["from_colony_id"],
            timestamp=d["timestamp"],
            nonce=d["nonce"],
            payload=d["payload"],
            signature=d.get("signature", ""),
        )


# ---------------------------------------------------------------------------
# Chain state dataclasses
# ---------------------------------------------------------------------------

@dataclass
class KeyHistoryEntry:
    public_key_hex: str
    active_from_height: int
    rotated_at_height: int | None = None   # None = still active
    revoked_at_height: int | None = None   # None = not revoked
    revocation_tx: str | None = None


@dataclass
class ColonyRecord:
    colony_id: str
    name: str
    active_public_key_hex: str
    endpoint: str
    description: str
    capabilities: list[str]
    protocol_version: str
    registered_at_height: int
    registered_at_tx: str
    is_genesis_admin: bool
    status: str  # "active" | "suspended" | "deregistered"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrustEdge:
    from_colony_id: str
    to_colony_id: str
    trust_level: int  # 0-4
    attested_at_height: int
    attested_at_tx: str
    valid_until: str | None = None  # ISO-8601 or None


@dataclass
class UntrustEvent:
    from_colony_id: str
    target_colony_id: str
    at_height: int
    tx_id: str
    report_abuse: bool
    timestamp: str


@dataclass
class SentinelRecord:
    sentinel_id: str
    colony_id: str
    host: str
    port: int
    public_key_hex: str
    registered_at_height: int
    status: str  # "active" | "demoted" | "deregistered"
    uptime_percent: float


@dataclass
class ProtocolConfig:
    block_interval_secs: int = 30
    untrust_threshold: int = 3
    untrust_window_days: int = 30
    uptime_requirement_percent: float = 99.0
    min_sentinels: int = 1
    min_protocol_version: str = "1.0.0"


@dataclass
class ProtocolUpgradeRecord:
    upgrade_id: str
    title: str
    proposed_at_height: int
    activation_height: int
    ratified: bool
    changes: dict[str, Any]


@dataclass
class ChainState:
    """Complete materialized state of the chain at a given height."""

    height: int = 0
    last_block_hash: str = "0" * 64
    colony_registry: dict[str, ColonyRecord] = field(default_factory=dict)
    name_registry: dict[str, str] = field(default_factory=dict)  # name -> colony_id
    key_history: dict[str, list[KeyHistoryEntry]] = field(default_factory=dict)
    trust_graph: dict[tuple[str, str], TrustEdge] = field(default_factory=dict)
    untrust_counters: dict[str, list[UntrustEvent]] = field(default_factory=dict)
    sentinel_roster: dict[str, SentinelRecord] = field(default_factory=dict)
    suspended_colonies: set[str] = field(default_factory=set)
    protocol_config: ProtocolConfig = field(default_factory=ProtocolConfig)
    upgrade_history: list[ProtocolUpgradeRecord] = field(default_factory=list)
    genesis_admin_id: str = ""
    network_id: str = ""
    # Plugin security registry
    plugin_registry: dict[str, Any] = field(default_factory=dict)  # plugin_hash -> PluginChainRecord
    # Track publish counts per colony for rate limiting
    plugin_publish_counts: dict[str, list[str]] = field(default_factory=dict)  # colony_id -> list of timestamps

    def colony_id_for_name(self, name: str) -> str | None:
        return self.name_registry.get(name.lower())

    def active_key_for_colony(self, colony_id: str) -> str | None:
        record = self.colony_registry.get(colony_id)
        if record:
            return record.active_public_key_hex
        return None

    def is_active(self, colony_id: str) -> bool:
        record = self.colony_registry.get(colony_id)
        return record is not None and record.status == "active"

    def is_suspended(self, colony_id: str) -> bool:
        return colony_id in self.suspended_colonies

    def active_sentinels(self) -> list[SentinelRecord]:
        return [s for s in self.sentinel_roster.values() if s.status == "active"]
