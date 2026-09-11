"""Deterministic chain state machine.

Applies blocks and transactions to produce the canonical ChainState.
Full replay from genesis is always possible; checkpoints speed up recovery.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from .block import Block
from .storage import ChainStore
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
from .plugin_transactions import (
    FindingSeverity,
    PluginChainRecord,
    PluginChainStatus,
    ScanResult,
)

logger = logging.getLogger(__name__)

_CHECKPOINT_INTERVAL = 100


def _iso_to_ts(iso: str) -> float:
    """Convert ISO-8601 string to UTC unix timestamp; returns 0.0 on failure."""
    if not iso:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


class ChainStateMachine:
    """Deterministic state computation from the block sequence."""

    def __init__(self, store: ChainStore) -> None:
        self.store = store

    def apply_block(self, state: ChainState, block: Block) -> ChainState:
        """Apply all transactions in block to state. Returns new state."""
        new_state = copy.deepcopy(state)
        new_state.height = block.index
        new_state.last_block_hash = block.block_hash

        for tx in block.transactions:
            new_state = self._apply_tx(new_state, tx, block.index)

        # Check for pending protocol upgrades that activate at this height
        new_state = self._apply_pending_upgrades(new_state, block.index)

        return new_state

    def compute_state_at(self, height: int) -> ChainState:
        """Compute state at height using checkpoints to minimize replay."""
        checkpoint = self.store.get_latest_checkpoint(height)
        if checkpoint is not None:
            state, start_height = checkpoint
            start_index = start_height + 1
        else:
            state = ChainState()
            start_index = 0

        for idx in range(start_index, height + 1):
            block = self.store.get_block(idx)
            if block is None:
                break
            state = self.apply_block(state, block)

        return state

    def get_current_state(self) -> ChainState:
        """Return state at the latest committed height."""
        height = self.store.get_height()
        if height < 0:
            return ChainState()
        return self.compute_state_at(height)

    def rebuild_and_checkpoint(self) -> ChainState:
        """Full replay from genesis, saving checkpoints every 100 blocks."""
        state = ChainState()
        height = self.store.get_height()
        for idx in range(height + 1):
            block = self.store.get_block(idx)
            if block is None:
                break
            state = self.apply_block(state, block)
            if idx > 0 and idx % _CHECKPOINT_INTERVAL == 0:
                self.store.save_checkpoint(state, idx)
        return state

    # ── Transaction dispatch ────────────────────────────────────────────────

    def _apply_tx(
        self, state: ChainState, tx: Transaction, block_index: int
    ) -> ChainState:
        dispatch = {
            TxType.COLONY_REGISTER: self._apply_colony_register,
            TxType.COLONY_ROTATE_KEY: self._apply_colony_rotate_key,
            TxType.COLONY_REVOKE_KEY: self._apply_colony_revoke_key,
            TxType.COLONY_RELEASE_NAME: self._apply_colony_release_name,
            TxType.TRUST_ATTEST: self._apply_trust_attest,
            TxType.UNTRUST_ATTEST: self._apply_untrust_attest,
            TxType.SENTINEL_REGISTER: self._apply_sentinel_register,
            TxType.SENTINEL_DEREGISTER: self._apply_sentinel_deregister,
            TxType.COLONY_SUSPEND: self._apply_colony_suspend,
            TxType.COLONY_REINSTATE: self._apply_colony_reinstate,
            TxType.PROTOCOL_UPGRADE: self._apply_protocol_upgrade,
            TxType.PLUGIN_PUBLISH: self._apply_plugin_publish,
            TxType.PLUGIN_ATTESTATION: self._apply_plugin_attestation,
            TxType.PLUGIN_FLAG: self._apply_plugin_flag,
            TxType.PLUGIN_QUARANTINE: self._apply_plugin_quarantine,
        }
        handler = dispatch.get(tx.type)
        if handler is None:
            logger.warning("Unknown tx type %s — skipping", tx.type)
            return state
        # Handler exceptions must propagate: silently returning the unmodified
        # state on a handler bug would mean different replicas could disagree
        # on which transactions actually applied, breaking consensus
        # invariants. If a handler raises, fail the whole apply_block so the
        # operator sees the corruption and can stop the node.
        return handler(state, tx, block_index)

    def _apply_colony_register(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        p = tx.payload
        colony_id = p["colony_id"]
        name = p["name"].lower()
        is_genesis_admin = p.get("genesis_admin", False)

        record = ColonyRecord(
            colony_id=colony_id,
            name=name,
            active_public_key_hex=p["public_key_hex"],
            endpoint=p.get("endpoint", ""),
            description=p.get("description", ""),
            capabilities=p.get("capabilities", []),
            protocol_version=p.get("protocol_version", "1.0.0"),
            registered_at_height=height,
            registered_at_tx=tx.tx_id,
            is_genesis_admin=is_genesis_admin,
            status="active",
            metadata=p.get("metadata", {}),
        )
        state.colony_registry[colony_id] = record
        state.name_registry[name] = colony_id
        state.key_history[colony_id] = [
            KeyHistoryEntry(
                public_key_hex=p["public_key_hex"],
                active_from_height=height,
            )
        ]
        if is_genesis_admin and not state.genesis_admin_id:
            state.genesis_admin_id = colony_id
        return state

    def _apply_colony_rotate_key(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        p = tx.payload
        colony_id = tx.from_colony_id
        new_key = p["new_public_key_hex"]

        record = state.colony_registry.get(colony_id)
        if record is None:
            return state

        # Mark old key as rotated
        history = state.key_history.get(colony_id, [])
        for entry in history:
            if entry.rotated_at_height is None and entry.revoked_at_height is None:
                entry.rotated_at_height = height

        # Add new key
        history.append(KeyHistoryEntry(public_key_hex=new_key, active_from_height=height))
        state.key_history[colony_id] = history
        record.active_public_key_hex = new_key
        return state

    def _apply_colony_revoke_key(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        p = tx.payload
        revoked_key = p["revoked_key_hex"]
        cascade = p.get("cascade", False)
        colony_id = tx.from_colony_id

        history = state.key_history.get(colony_id, [])
        for entry in history:
            if entry.public_key_hex == revoked_key:
                entry.revoked_at_height = height
                entry.revocation_tx = tx.tx_id

        if cascade:
            # Downgrade trust attestations signed by revoked key to DISCOVERY
            # (We don't track which key signed each attestation, so we downgrade
            # all attestations FROM this colony)
            for edge_key, edge in list(state.trust_graph.items()):
                if edge.from_colony_id == colony_id:
                    edge.trust_level = 0  # downgrade to DISCOVERY

        return state

    def _apply_colony_release_name(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        p = tx.payload
        name = p["name"].lower()
        state.name_registry.pop(name, None)
        return state

    def _apply_trust_attest(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        p = tx.payload
        target_id = p["target_colony_id"]
        edge_key = (tx.from_colony_id, target_id)
        state.trust_graph[edge_key] = TrustEdge(
            from_colony_id=tx.from_colony_id,
            to_colony_id=target_id,
            trust_level=p["trust_level"],
            attested_at_height=height,
            attested_at_tx=tx.tx_id,
            valid_until=p.get("valid_until"),
        )
        return state

    def _apply_untrust_attest(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        p = tx.payload
        target_id = p["target_colony_id"]
        edge_key = (tx.from_colony_id, target_id)

        # Remove trust edge
        state.trust_graph.pop(edge_key, None)

        # Track untrust event
        if target_id not in state.untrust_counters:
            state.untrust_counters[target_id] = []

        # Check for duplicate from same colony
        existing = [
            e for e in state.untrust_counters[target_id]
            if e.from_colony_id == tx.from_colony_id
        ]
        if not existing:
            event = UntrustEvent(
                from_colony_id=tx.from_colony_id,
                target_colony_id=target_id,
                at_height=height,
                tx_id=tx.tx_id,
                report_abuse=p.get("report_abuse", False),
                timestamp=tx.timestamp,
            )
            state.untrust_counters[target_id].append(event)

        # Auto-suspend if threshold reached
        threshold = state.protocol_config.untrust_threshold
        abuse_count = sum(
            1 for e in state.untrust_counters[target_id] if e.report_abuse
        )
        total_count = len(state.untrust_counters[target_id])

        if total_count >= threshold and not state.is_suspended(target_id):
            # Emit implicit suspend
            record = state.colony_registry.get(target_id)
            if record and record.status == "active":
                record.status = "suspended"
                state.suspended_colonies.add(target_id)
                logger.info(
                    "Auto-suspended colony %s: untrust threshold %d reached",
                    target_id,
                    threshold,
                )

        return state

    def _apply_sentinel_register(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        p = tx.payload
        sentinel_id = p["sentinel_id"]
        state.sentinel_roster[sentinel_id] = SentinelRecord(
            sentinel_id=sentinel_id,
            colony_id=p.get("colony_id", tx.from_colony_id),
            host=p.get("host", ""),
            port=p.get("port", 7744),
            public_key_hex=p.get("public_key_hex", ""),
            registered_at_height=height,
            status="active",
            uptime_percent=p.get("uptime_proof", {}).get("uptime_percent", 100.0),
        )
        return state

    def _apply_sentinel_deregister(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        p = tx.payload
        sentinel_id = p["sentinel_id"]
        record = state.sentinel_roster.get(sentinel_id)
        if record:
            record.status = "deregistered"
        return state

    def _apply_colony_suspend(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        p = tx.payload
        target_id = p["target_colony_id"]
        record = state.colony_registry.get(target_id)
        if record:
            record.status = "suspended"
        state.suspended_colonies.add(target_id)
        return state

    def _apply_colony_reinstate(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        p = tx.payload
        target_id = p["target_colony_id"]
        record = state.colony_registry.get(target_id)
        if record:
            record.status = "active"
        state.suspended_colonies.discard(target_id)
        # Reset untrust counter
        state.untrust_counters.pop(target_id, None)
        return state

    def _apply_protocol_upgrade(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        p = tx.payload
        title = p.get("title", "")
        changes = p.get("changes", {})

        # ── Special admin actions embedded as protocol_upgrade titles ────────

        # Genesis admin transfer: title == "transfer_genesis_admin"
        if title == "transfer_genesis_admin":
            new_admin_id = changes.get("new_genesis_admin_id", "")
            if new_admin_id and tx.from_colony_id == state.genesis_admin_id:
                old_admin_id = state.genesis_admin_id
                state.genesis_admin_id = new_admin_id
                # Update ColonyRecord flags
                if old_admin_id in state.colony_registry:
                    state.colony_registry[old_admin_id].is_genesis_admin = False
                if new_admin_id in state.colony_registry:
                    state.colony_registry[new_admin_id].is_genesis_admin = True
                logger.info(
                    "Genesis admin transferred: %s → %s",
                    old_admin_id[:16], new_admin_id[:16],
                )
            else:
                logger.warning(
                    "transfer_genesis_admin rejected: sender %s is not genesis admin %s",
                    tx.from_colony_id[:16], state.genesis_admin_id[:16] if state.genesis_admin_id else "(none)",
                )
            # Record but do not queue for future activation
            state.upgrade_history.append(ProtocolUpgradeRecord(
                upgrade_id=p.get("upgrade_id", ""),
                title=title,
                proposed_at_height=height,
                activation_height=height,
                ratified=True,
                changes=changes,
            ))
            return state

        # Unanimous Sentinel strip of Genesis admin:
        # title == "strip_genesis_admin" with co-signatures from ALL active Sentinels
        if title == "strip_genesis_admin":
            active_sentinels = state.active_sentinels()
            n_sentinels = len(active_sentinels)
            votes = p.get("votes", [])
            sentinel_ids = {s.sentinel_id for s in active_sentinels}
            yes_sentinel_ids = {
                v.get("sentinel_id")
                for v in votes
                if v.get("vote") == "yes" and v.get("sentinel_id") in sentinel_ids
            }
            # Unanimous: ALL active Sentinels must have voted yes
            unanimous = (n_sentinels > 0 and yes_sentinel_ids == sentinel_ids)
            if unanimous:
                old_admin = state.genesis_admin_id
                if old_admin and old_admin in state.colony_registry:
                    state.colony_registry[old_admin].is_genesis_admin = False
                state.genesis_admin_id = ""
                logger.warning(
                    "Genesis admin STRIPPED by unanimous Sentinel vote (was %s). "
                    "Network enters no-admin mode.",
                    old_admin[:16] if old_admin else "(none)",
                )
            else:
                logger.warning(
                    "strip_genesis_admin rejected: only %d/%d Sentinels voted yes",
                    len(yes_sentinel_ids), n_sentinels,
                )
            state.upgrade_history.append(ProtocolUpgradeRecord(
                upgrade_id=p.get("upgrade_id", ""),
                title=title,
                proposed_at_height=height,
                activation_height=height,
                ratified=unanimous,
                changes=changes,
            ))
            return state

        # ── Standard protocol upgrade ────────────────────────────────────────

        votes = p.get("votes", [])
        active_sentinels = state.active_sentinels()
        n_sentinels = len(active_sentinels)
        required = max(1, (n_sentinels * 2 // 3) + 1)
        yes_votes = [v for v in votes if v.get("vote") == "yes"]
        ratified = len(yes_votes) >= required or n_sentinels == 0

        upgrade = ProtocolUpgradeRecord(
            upgrade_id=p["upgrade_id"],
            title=title,
            proposed_at_height=height,
            activation_height=p.get("activation_height", height + 1),
            ratified=ratified,
            changes=changes,
        )
        state.upgrade_history.append(upgrade)
        return state

    # ── Plugin transaction handlers ─────────────────────────────────────────

    # Default rate limits (may be overridden by protocol_upgrade)
    _MAX_PUBLISHES_PER_DAY = 20
    _MAX_PUBLISHES_PER_HOUR = 5
    _AUTO_QUARANTINE_THRESHOLD = 3
    _AUTO_QUARANTINE_WINDOW_HOURS = 72
    _SAFE_CONSENSUS_THRESHOLD = 0.8
    _FLAG_SUSPENSION_THRESHOLD = 3

    def _apply_plugin_publish(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        p = tx.payload
        plugin_hash = p.get("plugin_hash", "")
        name = p.get("name", "")
        version = p.get("version", "")
        publisher_id = tx.from_colony_id

        # Reject if publisher is suspended
        if publisher_id in state.suspended_colonies:
            logger.warning("plugin_publish rejected: publisher %s is suspended", publisher_id)
            return state

        # Check idempotency: same (name, version, hash) → no-op
        for existing in state.plugin_registry.values():
            if existing.name == name and existing.version == version:
                if existing.plugin_hash == plugin_hash:
                    # Idempotent re-publish — no-op
                    return state
                else:
                    # Different hash for same (name, version) → reject
                    logger.warning(
                        "plugin_publish rejected: version %s@%s already exists with different hash",
                        name, version,
                    )
                    return state

        # Rate limiting: track publish timestamps per colony
        now_iso = datetime.now(timezone.utc).isoformat()
        timestamps = state.plugin_publish_counts.get(publisher_id, [])
        # Keep only timestamps in the last 24 hours
        cutoff_24h = datetime.now(timezone.utc).timestamp() - 86400
        cutoff_1h = datetime.now(timezone.utc).timestamp() - 3600
        recent_24h = [t for t in timestamps if _iso_to_ts(t) > cutoff_24h]
        recent_1h = [t for t in timestamps if _iso_to_ts(t) > cutoff_1h]

        if len(recent_24h) >= self._MAX_PUBLISHES_PER_DAY:
            logger.warning("plugin_publish rejected: %s exceeds daily rate limit", publisher_id)
            return state
        if len(recent_1h) >= self._MAX_PUBLISHES_PER_HOUR:
            logger.warning("plugin_publish rejected: %s exceeds hourly rate limit", publisher_id)
            return state

        # Add to registry
        record = PluginChainRecord(
            plugin_hash=plugin_hash,
            name=name,
            version=version,
            publisher_id=publisher_id,
            published_at_height=height,
            status=PluginChainStatus.PENDING_SCAN,
            source_url=p.get("source_url"),
            capabilities=p.get("capabilities", []),
        )
        state.plugin_registry[plugin_hash] = record

        # Update rate limit counters
        recent_24h.append(now_iso)
        state.plugin_publish_counts[publisher_id] = recent_24h

        logger.info("plugin_publish: registered %s@%s hash=%s", name, version, plugin_hash[:16])
        return state

    def _apply_plugin_attestation(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        p = tx.payload
        plugin_hash = p.get("plugin_hash", "")
        scanner_id = tx.from_colony_id
        scan_result = p.get("scan_result", ScanResult.ERROR.value)

        record = state.plugin_registry.get(plugin_hash)
        if record is None:
            logger.warning("plugin_attestation for unknown hash %s", plugin_hash[:16])
            return state

        # Deduplicate: one attestation per (plugin_hash, scanner_colony_id) — supersede old
        record.attestations = [
            a for a in record.attestations
            if a.get("scanner_colony_id") != scanner_id
        ]
        record.attestations.append({
            "scanner_colony_id": scanner_id,
            "scan_result": scan_result,
            "scanned_at": p.get("scanned_at", ""),
            "stages_completed": p.get("stages_completed", []),
        })

        # Recompute consensus using sentinel weights
        sentinel_ids = {s.colony_id for s in state.sentinel_roster.values() if s.status == "active"}
        scanner_weights = {sid: 2.0 for sid in sentinel_ids}
        # Trusted non-sentinels (trust_level >= 3) get weight 1.0, others 0.25 (default)
        record.recompute_consensus(scanner_weights)

        # State transitions
        if scan_result == ScanResult.FLAGGED.value:
            # Any FLAGGED from trusted scanner (sentinel) → FLAGGED status
            if scanner_id in sentinel_ids:
                record.status = PluginChainStatus.FLAGGED
                logger.info("plugin %s flagged by sentinel %s", plugin_hash[:16], scanner_id)
            elif record.status == PluginChainStatus.PENDING_SCAN:
                record.status = PluginChainStatus.FLAGGED
        elif scan_result == ScanResult.SAFE.value:
            # Check consensus threshold for SAFE transition
            if (
                record.status == PluginChainStatus.PENDING_SCAN
                and record.safe_consensus >= self._SAFE_CONSENSUS_THRESHOLD
                and record.has_sentinel_safe(frozenset(sentinel_ids))
            ):
                record.status = PluginChainStatus.SAFE
                logger.info(
                    "plugin %s reached SAFE consensus (%.2f)",
                    plugin_hash[:16], record.safe_consensus,
                )

        return state

    def _apply_plugin_flag(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        p = tx.payload
        plugin_hash = p.get("plugin_hash", "")
        reporter_id = tx.from_colony_id
        severity = p.get("severity", FindingSeverity.INFO.value)

        record = state.plugin_registry.get(plugin_hash)
        if record is None:
            logger.warning("plugin_flag for unknown hash %s", plugin_hash[:16])
            return state

        # Deduplicate: same (reporter, vuln_description) → idempotent
        vuln_desc = p.get("vulnerability_description", "")
        for existing_flag in record.flags:
            if (
                existing_flag.get("reporter_colony_id") == reporter_id
                and existing_flag.get("vulnerability_description") == vuln_desc
            ):
                return state  # duplicate

        record.flags.append({
            "reporter_colony_id": reporter_id,
            "severity": severity,
            "vulnerability_description": vuln_desc,
            "flagged_at": datetime.now(timezone.utc).isoformat(),
            "tx_id": tx.tx_id,
        })

        # Count trusted flags for auto-quarantine
        sentinel_ids = {s.colony_id for s in state.sentinel_roster.values() if s.status == "active"}
        high_severity = {FindingSeverity.HIGH.value, FindingSeverity.CRITICAL.value}
        mid_and_above = {FindingSeverity.MEDIUM.value, FindingSeverity.HIGH.value, FindingSeverity.CRITICAL.value}

        # Single Sentinel flag at medium+ → immediate quarantine
        if reporter_id in sentinel_ids and severity in mid_and_above:
            record.status = PluginChainStatus.QUARANTINED
            logger.info("plugin %s quarantined by sentinel flag from %s", plugin_hash[:16], reporter_id)
            return state

        # Count distinct trusted reporters with high/critical severity in last 72h
        window_ts = datetime.now(timezone.utc).timestamp() - self._AUTO_QUARANTINE_WINDOW_HOURS * 3600
        trusted_high_reporters = set()
        for flag in record.flags:
            if (
                flag["severity"] in high_severity
                and _iso_to_ts(flag.get("flagged_at", "")) > window_ts
            ):
                trusted_high_reporters.add(flag["reporter_colony_id"])

        record.flag_count_trusted = len(trusted_high_reporters)

        if len(trusted_high_reporters) >= self._AUTO_QUARANTINE_THRESHOLD:
            record.status = PluginChainStatus.QUARANTINED
            logger.info(
                "plugin %s auto-quarantined: %d trusted flags",
                plugin_hash[:16], len(trusted_high_reporters),
            )

        return state

    def _apply_plugin_quarantine(
        self, state: ChainState, tx: Transaction, height: int
    ) -> ChainState:
        """Apply a system-generated plugin_quarantine transaction."""
        p = tx.payload
        plugin_hash = p.get("plugin_hash", "")

        record = state.plugin_registry.get(plugin_hash)
        if record is None:
            logger.warning("plugin_quarantine for unknown hash %s", plugin_hash[:16])
            return state

        record.status = PluginChainStatus.QUARANTINED
        logger.info(
            "plugin %s quarantined via system tx (reason: %s)",
            plugin_hash[:16], p.get("quarantine_reason", "unknown"),
        )
        return state

    def _apply_pending_upgrades(self, state: ChainState, height: int) -> ChainState:
        """Apply any ratified protocol upgrades whose activation height has arrived."""
        for upgrade in state.upgrade_history:
            if upgrade.ratified and upgrade.activation_height == height:
                changes = upgrade.changes
                pc = state.protocol_config
                if "block_interval_secs" in changes:
                    pc.block_interval_secs = changes["block_interval_secs"]
                if "untrust_threshold" in changes:
                    pc.untrust_threshold = changes["untrust_threshold"]
                if "untrust_window_days" in changes:
                    pc.untrust_window_days = changes["untrust_window_days"]
                if "uptime_requirement_percent" in changes:
                    pc.uptime_requirement_percent = changes["uptime_requirement_percent"]
                if "min_protocol_version" in changes:
                    pc.min_protocol_version = changes["min_protocol_version"]
                logger.info("Applied protocol upgrade %s at height %d", upgrade.upgrade_id, height)
        return state
